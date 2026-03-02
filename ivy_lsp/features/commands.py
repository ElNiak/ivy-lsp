"""Custom Ivy tool commands: verify, compile, show model."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Union

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.async_subprocess import run_ivy_subprocess
from ivy_lsp.utils.validation import validate_ivy_param as _validate_ivy_param

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


DEFAULT_VERIFY_TIMEOUT = 120.0
DEFAULT_COMPILE_TIMEOUT = 300.0
DEFAULT_SHOW_MODEL_TIMEOUT = 30.0


def _find_tool(name: str) -> Optional[str]:
    """Check if an Ivy CLI tool is available on PATH."""
    return shutil.which(name)


def _resolve_via_staging(server: Any, filepath: str) -> str:
    """Return the staging-directory path for *filepath* if available.

    Ivy's ``import_module()`` resolves ``include`` directives relative
    to the **process CWD** (via bare ``open(fname, 'r')``), *not* the
    input file's directory.  Callers must therefore also pass
    ``cwd=os.path.dirname(staged_path)`` to ``_run_tool()`` so the
    subprocess CWD points into the staging directory where every
    workspace ``.ivy`` file has a flat symlink.

    Falls back to the original path when the server has no active
    staging directory or the file's basename is not in the staging map.
    """
    try:
        resolver = server._indexer._resolver
    except AttributeError:
        return filepath

    staged = resolver.get_staged_path(filepath)
    if staged is not None:
        return staged
    return filepath


def _detect_isolate_at_position(
    server: Any,
    uri: str,
    position: Optional[lsp.Position],
) -> Optional[str]:
    """Detect which isolate the cursor is inside using document symbols."""
    if position is None:
        return None

    filepath = uri_to_path(uri)
    doc = server.workspace.get_text_document(uri)
    source = doc.source or ""

    from ivy_lsp.features.document_symbols import compute_document_symbols

    symbols = compute_document_symbols(
        server._parser, server._indexer, source, filepath
    )

    def _find_containing(
        syms: Sequence[lsp.DocumentSymbol], line: int
    ) -> Optional[str]:
        for sym in syms:
            if sym.kind == lsp.SymbolKind.Namespace:  # isolate
                if sym.range.start.line <= line <= sym.range.end.line:
                    return sym.name
            if sym.children:
                found = _find_containing(sym.children, line)
                if found:
                    return found
        return None

    return _find_containing(symbols, position.line)


def _detect_isolate_from_params(
    server: Any, uri: str, params: Any
) -> tuple:
    """Extract explicit isolate from params, or detect from cursor position.

    Returns (isolate, position) where either may be None.
    Used by ivy/verify and ivy/showModel which share this detection pattern.
    """
    position = None
    raw_pos = getattr(params, "position", None)
    if raw_pos is not None:
        position = lsp.Position(line=raw_pos.line, character=raw_pos.character)

    isolate = getattr(params, "isolate", None)
    if isolate is None and position is not None:
        isolate = _detect_isolate_at_position(server, uri, position)

    return isolate, position


def _collect_all_isolates(
    server: Any,
    filepath: str,
    source: str,
) -> List[str]:
    """Collect all isolate names from the current file and transitive includes.

    Checks the current file's document symbols first, then walks the include
    graph to find isolates defined in transitively included modules.
    """
    from ivy_lsp.features.document_symbols import compute_document_symbols

    def _extract_isolate_names(
        syms: Sequence[lsp.DocumentSymbol],
    ) -> List[str]:
        names: List[str] = []
        for sym in syms:
            if sym.kind == lsp.SymbolKind.Namespace:
                names.append(sym.name)
            if sym.children:
                names.extend(_extract_isolate_names(sym.children))
        return names

    # Check current file
    symbols = compute_document_symbols(
        server._parser, server._indexer, source, filepath
    )
    isolates = _extract_isolate_names(symbols)

    # If none found locally, walk transitive includes
    if not isolates:
        try:
            include_graph = server._indexer._include_graph
            cache = server._indexer._cache
        except AttributeError:
            return isolates

        for included_file in include_graph.get_transitive_includes(filepath):
            cached = cache.get(included_file)
            if cached is None:
                continue
            for sym in cached.symbols:
                if getattr(sym, "kind", None) == lsp.SymbolKind.Namespace:
                    isolates.append(sym.name)

    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for name in isolates:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _find_enclosing_test(
    server: Any,
    filepath: str,
) -> Optional[str]:
    """Find a test file whose include closure contains *filepath*.

    Returns the active test if it includes the file, otherwise
    picks the first available test scope.  Returns ``None`` if no
    test scope covers the file or if *filepath* is itself a test.
    """
    try:
        graph = server._indexer._requirement_graph
    except AttributeError:
        return None

    if not isinstance(graph, ScopedRequirementModel):
        return None

    # If the file IS a test file, no redirection needed
    if filepath in graph._test_scopes:
        return None

    # Prefer the active test scope if it covers this file
    active = graph.get_active_scope()
    if active and active.is_file_in_scope(filepath):
        return active.test_file

    # Find any test that includes this file
    tests = graph.get_tests_for_file(filepath)
    if tests:
        return sorted(tests)[0]  # deterministic pick

    return None


def _get_compile_env() -> Optional[Dict[str, str]]:
    """Build environment with system include/lib paths for C++ compilation.

    On macOS, Homebrew installs headers/libs outside the default compiler
    search paths. This adds them via CPLUS_INCLUDE_PATH and LIBRARY_PATH
    so that g++ (invoked by ivyc) can find Z3, OpenSSL, etc.
    """
    if sys.platform != "darwin":
        return None

    env = dict(os.environ)
    extra_includes: List[str] = []
    extra_libs: List[str] = []

    for prefix in ("/opt/homebrew", "/usr/local"):
        inc = os.path.join(prefix, "include")
        lib = os.path.join(prefix, "lib")
        if os.path.isdir(inc):
            extra_includes.append(inc)
        if os.path.isdir(lib):
            extra_libs.append(lib)

    if not extra_includes and not extra_libs:
        return None

    if extra_includes:
        existing = env.get("CPLUS_INCLUDE_PATH", "")
        env["CPLUS_INCLUDE_PATH"] = os.pathsep.join(
            filter(None, [existing] + extra_includes)
        )
    if extra_libs:
        existing = env.get("LIBRARY_PATH", "")
        env["LIBRARY_PATH"] = os.pathsep.join(
            filter(None, [existing] + extra_libs)
        )

    return env


async def _run_tool(
    cmd: List[str],
    timeout: float,
    server: Any,
    token: Optional[Union[str, int]] = None,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run an Ivy CLI tool as async subprocess with progress reporting.

    Delegates to :func:`run_ivy_subprocess` for bounded-concurrency
    execution and wraps the result with LSP work-done progress
    notifications.
    """
    if token is not None:
        try:
            await server.work_done_progress.create_async(token)
            server.work_done_progress.begin(
                token,
                lsp.WorkDoneProgressBegin(
                    title="Ivy",
                    message=f"Running {cmd[0]}...",
                    cancellable=True,
                ),
            )
        except Exception:
            logger.debug("Could not create progress token", exc_info=True)
            token = None  # fall back to no progress

    try:
        result = await run_ivy_subprocess(
            cmd, timeout=timeout, cwd=cwd, env=env,
        )

        return {
            "success": result.success,
            "message": result.message,
            "output": result.output_lines,
            "duration": result.duration,
        }
    finally:
        if token is not None:
            try:
                server.work_done_progress.end(
                    token, lsp.WorkDoneProgressEnd(message="Done")
                )
            except Exception:
                logger.warning(
                    "Failed to end progress token %s",
                    token,
                    exc_info=True,
                )



def _redirect_to_enclosing_test(
    server: Any,
    uri: str,
    filepath: str,
    isolate: Optional[str],
    op_name: str,
) -> tuple:
    """Redirect a module file to its enclosing test if applicable.

    When the user invokes verify/showModel on a non-test module,
    we redirect to the enclosing test file and optionally infer the
    isolate from the module basename.

    Returns:
        ``(filepath, isolate, redirected)`` — *filepath* is the test
        file if redirection happened, original otherwise.
    """
    enclosing_test = _find_enclosing_test(server, filepath)
    if enclosing_test is None:
        return filepath, isolate, False

    slog.info(
        "Redirecting %s from module %s to test %s",
        op_name,
        filepath,
        enclosing_test,
        extra={"event": LogEvent(
            LogCategory.ACTIVITY, op_name,
            {"module": filepath, "test": enclosing_test},
        )},
    )

    if isolate is None:
        module_basename = os.path.basename(
            uri_to_path(uri)
        ).replace(".ivy", "")
        # Read actual source for isolate detection
        enc_source = ""
        try:
            with open(enclosing_test, "r", encoding="utf-8", errors="replace") as f:
                enc_source = f.read()
        except OSError:
            logger.debug("Could not read %s for isolate detection", enclosing_test)
        all_isolates = _collect_all_isolates(server, enclosing_test, enc_source)
        if module_basename in all_isolates:
            isolate = module_basename

    return enclosing_test, isolate, True


def _extract_param(params: Any, dict_key: str) -> Optional[str]:
    """Extract first argument from LSP command params.

    Handles three param shapes:
    - list: unwraps one nesting level, returns first element.
      Both ``["quic_types"]`` and ``[["quic_types", "file://..."]]``
      yield ``"quic_types"``.
    - dict: returns ``params[dict_key]``.
    - object with ``.arguments``: treats ``.arguments`` as a list and
      applies the same unwrapping as the list path.

    Returns None if params is None, empty, or the extracted value is
    not a string.
    """
    raw: Any = None
    if isinstance(params, list) and params:
        first = params[0]
        # Unwrap one nesting level (e.g. workspace/executeCommand wrapping)
        if isinstance(first, list) and first:
            raw = first[0]
        else:
            raw = first
    elif isinstance(params, dict):
        raw = params.get(dict_key)
    elif params is not None:
        args = getattr(params, "arguments", None)
        if args:
            first = args[0]
            if isinstance(first, list) and first:
                raw = first[0]
            else:
                raw = first
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning(
            "Expected string parameter for %r, got %s: %r",
            dict_key,
            type(raw).__name__,
            raw,
        )
        return None
    return raw


def register(server: Any) -> None:
    """Register custom Ivy command handlers."""
    from dataclasses import dataclass

    @dataclass
    class _ServerProxy:
        _indexer: Any

    def _track_start(srv: Any, op_type: str, filepath: str) -> Optional[str]:
        tracker = getattr(srv, "state_tracker", None)
        if tracker is None:
            return None
        return tracker.operation_tracker.record_start(op_type, filepath)

    def _track_end(srv: Any, op_id: Optional[str], result: Dict[str, Any]) -> None:
        if op_id is None:
            return
        tracker = getattr(srv, "state_tracker", None)
        if tracker is None:
            return
        tracker.operation_tracker.record_end(
            op_id,
            success=result.get("success", False),
            message=result.get("message", ""),
            duration=result.get("duration", 0.0),
        )

    def _track_error(srv: Any, op_id: Optional[str], exc: Exception) -> None:
        if op_id is None:
            return
        tracker = getattr(srv, "state_tracker", None)
        if tracker is None:
            return
        tracker.operation_tracker.record_end(
            op_id, success=False, message=str(exc), duration=0.0
        )

    @server.feature("ivy/verify")
    async def ivy_verify(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)

        isolate, _ = _detect_isolate_from_params(server, uri, params)

        # Redirect module files to their enclosing test
        filepath, isolate, redirected = _redirect_to_enclosing_test(
            server, uri, filepath, isolate, "verify"
        )

        op_id = _track_start(server, "verify", filepath)
        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={_validate_ivy_param(isolate)}")
        elif redirected:
            cmd.append("coi=false")
        cmd.append(staged_filepath)

        try:
            result = await _run_tool(
                cmd,
                DEFAULT_VERIFY_TIMEOUT,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
            )
            result["isolate"] = isolate

            # Parse output into diagnostics
            from ivy_lsp.features.diagnostics import parse_ivy_check_output

            combined = "\n".join(result["output"])
            deep_diags = parse_ivy_check_output(combined)
            result["diagnosticCount"] = len(deep_diags)

            # Publish merged diagnostics
            from ivy_lsp.features.diagnostics import compute_diagnostics

            doc = server.workspace.get_text_document(uri)
            base_diags = compute_diagnostics(
                server._parser, doc.source or "", filepath, server._indexer
            )
            server.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(
                    uri=uri, version=doc.version, diagnostics=base_diags + deep_diags
                )
            )

            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/compile")
    async def ivy_compile(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)
        target = getattr(params, "target", "test")

        # Redirect module files to their enclosing test
        enclosing_test = _find_enclosing_test(server, filepath)
        if enclosing_test is not None:
            slog.info(
                "Redirecting ivyc from module %s to test %s",
                filepath,
                enclosing_test,
                extra={"event": LogEvent(
                    LogCategory.ACTIVITY, "compile",
                    {"module": filepath, "test": enclosing_test},
                )},
            )
            filepath = enclosing_test

        op_id = _track_start(server, "compile", filepath)

        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivyc", f"target={_validate_ivy_param(target)}", os.path.basename(staged_filepath)]
        try:
            result = await _run_tool(
                cmd,
                DEFAULT_COMPILE_TIMEOUT,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
                env=_get_compile_env(),
            )
            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/showModel")
    async def ivy_show_model(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)

        isolate, _ = _detect_isolate_from_params(server, uri, params)

        # If still no isolate, collect from file + transitive includes
        if isolate is None:
            doc = server.workspace.get_text_document(uri)
            source = doc.source or ""
            all_isolates = _collect_all_isolates(
                server, filepath, source
            )
            if len(all_isolates) == 1:
                isolate = all_isolates[0]
            elif len(all_isolates) > 1:
                return {
                    "success": False,
                    "message": "Multiple isolates found — please select one",
                    "output": [],
                    "duration": 0.0,
                    "availableIsolates": all_isolates,
                }

        # Redirect module files to their enclosing test to avoid
        # circular-include "redefining" errors (mirrors ivy_to_cpp.py).
        filepath, isolate, redirected = _redirect_to_enclosing_test(
            server, uri, filepath, isolate, "show_model"
        )

        op_id = _track_start(server, "showModel", filepath)
        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_show"]
        if isolate:
            cmd.append(f"isolate={_validate_ivy_param(isolate)}")
        elif redirected:
            cmd.append("coi=false")
        cmd.append(staged_filepath)
        try:
            result = await _run_tool(
                cmd,
                DEFAULT_SHOW_MODEL_TIMEOUT,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
            )
            result["isolate"] = isolate
            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/capabilities")
    async def ivy_capabilities(params: Any = None) -> Dict[str, Any]:
        return {
            "fullMode": getattr(server, "_full_mode", False),
            "ivyCheckAvailable": _find_tool("ivy_check") is not None,
            "ivycAvailable": _find_tool("ivyc") is not None,
            "ivyShowAvailable": _find_tool("ivy_show") is not None,
            "compiledModelAvailable": getattr(server, "_compiler_manager", None)
            is not None,
        }

    def _refresh_open_diagnostics_sync(srv) -> None:
        """Re-publish diagnostics for all open documents (sequential).

        Called from non-async contexts. For async callers prefer
        :func:`_refresh_open_diagnostics_async` which processes
        documents in parallel.
        """
        from ivy_lsp.features.diagnostics import compute_diagnostics

        try:
            items = list(srv.workspace.text_documents.items())
        except (AttributeError, TypeError):
            return

        for uri, doc in items:
            if not uri.startswith("file://"):
                continue
            filepath = uri_to_path(uri)
            try:
                diags = compute_diagnostics(
                    srv._parser, doc.source or "", filepath, srv._indexer
                )
                srv.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(uri=uri, version=doc.version, diagnostics=diags)
                )
            except Exception:
                logger.warning(
                    "Failed to refresh diagnostics for %s", uri, exc_info=True
                )

    async def _refresh_open_diagnostics_async(srv) -> None:
        """Re-publish diagnostics for all open documents in parallel.

        Uses ``asyncio.gather`` to compute diagnostics for each open
        document concurrently via the thread-pool executor.
        """
        from ivy_lsp.features.diagnostics import compute_diagnostics

        try:
            items = list(srv.workspace.text_documents.items())
        except (AttributeError, TypeError):
            return

        loop = asyncio.get_running_loop()

        async def _process_one(uri: str, doc: Any) -> None:
            if not uri.startswith("file://"):
                return
            filepath = uri_to_path(uri)
            try:
                diags = await loop.run_in_executor(
                    None,
                    compute_diagnostics,
                    srv._parser,
                    doc.source or "",
                    filepath,
                    srv._indexer,
                )
                srv.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(uri=uri, version=doc.version, diagnostics=diags)
                )
            except Exception:
                logger.warning(
                    "Failed to refresh diagnostics for %s", uri, exc_info=True
                )

        await asyncio.gather(
            *(_process_one(uri, doc) for uri, doc in items)
        )

    @server.feature("ivy/setActiveTest")
    async def ivy_set_active_test(params: Any = None) -> Dict[str, Any]:
        """Set the active test scope for diagnostics and code lenses.

        On success, re-publishes diagnostics for all open documents
        so scoped filtering takes effect immediately.
        """
        if params is None:
            return {"success": False, "error": "No params provided"}

        test_file = getattr(params, "testFile", None)

        try:
            graph = server._indexer._requirement_graph
        except AttributeError:
            return {"success": False, "error": "Indexer not available"}

        if not isinstance(graph, ScopedRequirementModel):
            return {"success": False, "error": "Scoped model not available"}

        if test_file is not None and test_file not in graph._test_scopes:
            active = graph.get_active_scope()
            return {
                "success": False,
                "error": f"Unknown test: {test_file}",
                "activeTest": active.test_file if active else None,
            }

        graph.set_active_test(test_file)
        active = graph.get_active_scope()

        await _refresh_open_diagnostics_async(server)

        return {
            "success": True,
            "activeTest": active.test_file if active else None,
        }

    @server.feature("ivy/listTests")
    async def ivy_list_tests(params: Any = None) -> Dict[str, Any]:
        """List all discovered test scopes with metadata."""
        try:
            graph = server._indexer._requirement_graph
        except AttributeError:
            return {"tests": [], "activeTest": None}

        if not isinstance(graph, ScopedRequirementModel):
            return {"tests": [], "activeTest": None}

        tests = []
        for _test_file, scope in sorted(graph._test_scopes.items()):
            tests.append({
                "testFile": scope.test_file,
                "testerRole": scope.tester_role,
                "exportCount": len(scope.exported_actions),
                "importCount": len(scope.imported_actions),
                "includeCount": len(scope.include_closure),
            })

        active = graph.get_active_scope()
        return {
            "tests": tests,
            "activeTest": active.test_file if active else None,
        }

    @server.feature("ivy/compileTest")
    async def ivy_compile_test(params: Any = None) -> Dict[str, Any]:
        """Compile a specific test file with ivyc target=test."""
        if params is None:
            return {
                "success": False,
                "message": "No params provided",
                "output": [],
                "duration": 0.0,
            }

        test_file = getattr(params, "testFile", None)
        if not test_file:
            return {
                "success": False,
                "message": "No testFile specified",
                "output": [],
                "duration": 0.0,
            }

        op_id = _track_start(server, "compileTest", test_file)
        token = getattr(params, "workDoneToken", None)
        staged = _resolve_via_staging(server, test_file)
        cmd = ["ivyc", "target=test", os.path.basename(staged)]
        try:
            result = await _run_tool(
                cmd,
                DEFAULT_COMPILE_TIMEOUT,
                server,
                token,
                env=_get_compile_env(),
                cwd=os.path.dirname(staged),
            )

            # Store compilation result in scoped model if available
            try:
                graph = server._indexer._requirement_graph
                if isinstance(graph, ScopedRequirementModel):
                    graph._compilation_results[test_file] = result
            except AttributeError:
                logger.warning(
                    "Failed to store compilation result for %s",
                    test_file,
                    exc_info=True,
                )

            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/activeDocumentChanged")
    async def ivy_active_document_changed(params: Any = None) -> None:
        """Auto-detect active test when user switches documents.

        If the newly focused document is a registered test file,
        set it as the active test and refresh diagnostics.
        Non-test files are ignored (sticky behavior).
        """
        uri = getattr(params, "uri", None)
        if not uri or not uri.startswith("file://"):
            return

        filepath = uri_to_path(uri)

        try:
            graph = server._indexer._requirement_graph
        except AttributeError:
            return

        if not isinstance(graph, ScopedRequirementModel):
            return

        if filepath not in graph._test_scopes:
            return  # Non-test file: keep current scope (sticky)

        # Check if already the active test (avoid redundant refresh)
        current = graph.get_active_scope()
        if current and current.test_file == filepath:
            return

        graph.set_active_test(filepath)
        await _refresh_open_diagnostics_async(server)

    @server.feature("ivy/compiledModel")
    async def ivy_compiled_model(params: Any = None) -> Dict[str, Any]:
        """Return the cached CompiledModuleIR for a file as JSON."""
        if params is None:
            return {"success": False, "error": "No params provided"}

        uri = getattr(params, "textDocument", None)
        if uri is not None:
            uri = getattr(uri, "uri", uri)
        if not uri:
            uri = getattr(params, "uri", None)
        if not uri:
            return {"success": False, "error": "No file URI provided"}

        filepath = uri_to_path(uri)

        manager = getattr(server, "_compiler_manager", None)
        if manager is None:
            return {"success": False, "error": "CompilerManager not available"}

        ir = manager.get_cached(filepath)
        if ir is None:
            return {
                "success": False,
                "error": (
                    f"No cached compilation for {os.path.basename(filepath)}"
                ),
                "hint": "Run ivy/compile or wait for background compilation",
            }

        # Flatten mixins from Dict[str, List[MixinIR]] to a single list
        flat_mixins = []
        for mixin_list in ir.mixins.values():
            for m in mixin_list:
                flat_mixins.append(
                    {"mixer": m.mixer, "mixee": m.mixee, "kind": m.kind}
                )

        return {
            "success": True,
            "filepath": filepath,
            "compileDuration": ir.compile_duration,
            "sorts": {
                name: {
                    "name": s.name,
                    "arity": s.arity,
                    "isUninterpreted": s.is_uninterpreted,
                    "isEnumerated": s.is_enumerated,
                    "constructors": list(s.constructors),
                }
                for name, s in ir.sorts.items()
            },
            "symbols": {
                name: {
                    "name": s.name,
                    "sortStr": s.sort_str,
                    "domainSorts": list(s.domain_sorts),
                    "rangeSort": s.range_sort,
                    "isRelation": s.is_relation,
                    "isDestructor": s.is_destructor,
                    "isConstructor": s.is_constructor,
                }
                for name, s in ir.symbols.items()
            },
            "actions": {
                name: {
                    "name": a.name,
                    "formalParams": list(a.formal_params),
                    "formalReturns": list(a.formal_returns),
                    "isExported": a.is_exported,
                    "isImported": a.is_imported,
                }
                for name, a in ir.actions.items()
            },
            "mixins": flat_mixins,
            "isolates": {
                name: {
                    "name": iso.name,
                    "verifiedComponents": list(iso.verified_components),
                    "presentComponents": list(iso.present_components),
                }
                for name, iso in ir.isolates.items()
            },
            "axiomCount": len(ir.labeled_axioms),
            "conjectureCount": len(ir.labeled_conjectures),
            "requirementCount": len(ir.requirements),
            "errors": list(ir.errors),
        }

    # ------------------------------------------------------------------
    # Code-lens command handlers
    #
    # These are registered both as custom protocol methods (ivy.xxx) for
    # direct JSON-RPC calls AND via workspace/executeCommand so that
    # CodeLens clicks work.  The dispatch table is built at the end of
    # this block.
    # ------------------------------------------------------------------

    @server.feature("ivy.showActionRequirements")
    async def ivy_show_action_requirements(params: Any = None) -> Dict[str, Any]:
        """Handle clicks on monitor/state-var code lenses."""
        from ivy_lsp.features.visualization import handle_action_requirements

        action_name = _extract_param(params, "actionName")

        viz_params: Dict[str, Any] = {}
        if action_name:
            viz_params["actionName"] = action_name

        indexer = getattr(server, "_indexer", None)
        if indexer is None:
            return {"error": "No indexer available"}

        proxy = _ServerProxy(_indexer=indexer)
        return handle_action_requirements(proxy, viz_params)

    @server.feature("ivy.showPropertyDetails")
    async def ivy_show_property_details(params: Any = None) -> Dict[str, Any]:
        """Handle clicks on property/axiom/invariant code lenses."""
        prop_id = _extract_param(params, "propertyId")

        indexer = getattr(server, "_indexer", None)
        if indexer is None:
            return {"error": "No indexer available"}

        graph = getattr(indexer, "_requirement_graph", None)
        if graph is None or prop_id is None:
            return {"error": "No data available"}

        prop = graph.properties.get(prop_id)
        if prop is None:
            return {"error": f"Property {prop_id!r} not found"}

        return {
            "id": prop.id,
            "kind": prop.kind,
            "file": prop.file,
            "line": prop.line,
            "formulaText": prop.formula_text,
        }

    @server.feature("ivy.navigateToInclude")
    async def ivy_navigate_to_include(params: Any = None) -> Dict[str, Any]:
        """Handle clicks on include directive code lenses."""
        include_name = _extract_param(params, "includeName")

        # Extract from_file for include resolution context.
        # Params may arrive flat  [includeName, fromUri]
        #                or nested [[includeName, fromUri]]
        from_file = None
        if isinstance(params, list) and params:
            inner = params[0] if isinstance(params[0], list) else params
            if len(inner) > 1:
                raw = inner[1]
                if isinstance(raw, str):
                    from_file = (
                        uri_to_path(raw) if raw.startswith("file://") else raw
                    )
        elif isinstance(params, dict):
            uri = params.get("uri") or params.get("fromFile")
            if uri:
                from_file = uri_to_path(uri) if uri.startswith("file://") else uri

        if not include_name:
            return {"error": "No include name provided"}

        indexer = getattr(server, "_indexer", None)
        if indexer is None:
            return {"error": "No indexer available"}

        resolver = getattr(indexer, "_resolver", None)
        if resolver is None:
            return {"error": "No resolver available"}

        if not from_file:
            workspace_root = getattr(indexer, "_workspace_root", "")
            from_file = workspace_root

        resolved = resolver.resolve(include_name, from_file)
        if not resolved:
            return {"error": f"Cannot resolve include {include_name!r}"}

        # Validate resolved path is within workspace root
        workspace_root = getattr(indexer, "_workspace_root", None)
        if workspace_root:
            real_resolved = os.path.realpath(resolved)
            real_root = os.path.realpath(workspace_root)
            if not real_resolved.startswith(real_root + os.sep) and real_resolved != real_root:
                logger.warning(
                    "Resolved include %r escapes workspace: %s",
                    include_name,
                    resolved,
                )
                return {"error": "Resolved path escapes workspace boundary"}

        return {"resolved": resolved, "uri": "file://" + resolved}

    @server.feature("ivy.showRfcDetails")
    async def ivy_show_rfc_details(params: Any = None) -> Dict[str, Any]:
        """Handle clicks on RFC tag code lenses."""
        tag = _extract_param(params, "tag")

        if not tag:
            return {"error": "No RFC tag provided"}

        model = getattr(server, "_semantic_model", None)
        if model is None:
            return {"error": "No semantic model available"}

        node = model.get_node(tag)
        if node is None:
            return {"error": f"RFC tag {tag!r} not found"}

        return {
            "id": node.id,
            "level": getattr(node, "level", None),
            "rfc": getattr(node, "rfc", None),
            "text": getattr(node, "text", None),
        }

    @server.feature("ivy.noop")
    async def ivy_noop(params: Any = None) -> None:
        """No-op command for informational code lenses."""
        return None

    # ------------------------------------------------------------------
    # workspace/executeCommand dispatcher for CodeLens clicks
    # ------------------------------------------------------------------

    _LENS_COMMANDS: Dict[str, Any] = {
        "ivy.showActionRequirements": ivy_show_action_requirements,
        "ivy.showPropertyDetails": ivy_show_property_details,
        "ivy.navigateToInclude": ivy_navigate_to_include,
        "ivy.showRfcDetails": ivy_show_rfc_details,
        "ivy.noop": ivy_noop,
    }

    @server.feature(
        lsp.WORKSPACE_EXECUTE_COMMAND,
        lsp.ExecuteCommandOptions(commands=list(_LENS_COMMANDS.keys())),
    )
    async def execute_command(
        params: lsp.ExecuteCommandParams,
    ) -> Optional[Dict[str, Any]]:
        """Dispatch workspace/executeCommand to code-lens handlers."""
        handler = _LENS_COMMANDS.get(params.command)
        if handler is None:
            return {"error": f"Unknown command: {params.command!r}"}
        return await handler(params.arguments)

    @server.feature("ivy/recompileAll")
    async def ivy_recompile_all(params: Any = None) -> Dict[str, Any]:
        """Re-trigger bulk compilation for all test entry points.

        Validates that the pipeline, compiler manager, scoped requirement
        model, and test files are available, and that no bulk compilation
        is already running.  Spawns a daemon thread that calls
        ``AnalysisPipeline.run_bulk_tier3()`` and returns immediately
        with the test file count.
        """
        pipeline = getattr(server, "_analysis_pipeline", None)
        if pipeline is None:
            return {"success": False, "error": "Analysis pipeline not initialized"}

        if getattr(pipeline, "_compiler_manager", None) is None:
            return {"success": False, "error": "CompilerManager not available"}

        ps = pipeline.get_pipeline_state()
        if ps.get("bulkCompileRunning", False):
            return {"success": False, "error": "Bulk compilation already running"}

        try:
            graph = server._indexer._requirement_graph
        except AttributeError:
            return {"success": False, "error": "Requirement graph not available"}

        if not isinstance(graph, ScopedRequirementModel):
            return {"success": False, "error": "Scoped model not available"}

        test_files = list(graph._test_scopes.keys())
        if not test_files:
            return {"success": False, "error": "No test files found"}

        cancel_event = getattr(server, "_bulk_analysis_cancel", None)

        def _run():
            progress_cb = None
            try:
                progress_cb = server._make_progress_callback(
                    "Ivy Recompilation",
                    "Recompiling {total} test files...",
                    "Recompiled {total} test files",
                    throttle_seconds=1.0,
                )
            except Exception:
                logger.warning(
                    "Could not create progress callback for recompilation; "
                    "user will not see progress updates",
                    exc_info=True,
                )
            pipeline.run_bulk_tier3(
                test_files,
                progress_callback=progress_cb,
                cancel_event=cancel_event,
            )

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run)

        return {
            "success": True,
            "message": f"Recompilation started for {len(test_files)} test files",
            "testFileCount": len(test_files),
        }
