"""Custom Ivy tool commands: verify, compile, show model."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Union

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.utils import uri_to_path
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
    """Run an Ivy CLI tool as async subprocess with progress reporting."""
    start = time.monotonic()

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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Process did not exit after kill; may be orphaned")
            return {
                "success": False,
                "message": f"Timed out after {timeout}s",
                "output": [],
                "duration": time.monotonic() - start,
            }

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        output_lines = (stderr_text + stdout_text).splitlines()
        success = proc.returncode == 0

        return {
            "success": success,
            "message": "OK" if success else f"Exit code {proc.returncode}",
            "output": output_lines,
            "duration": time.monotonic() - start,
        }

    except FileNotFoundError:
        return {
            "success": False,
            "message": f"{cmd[0]} not found on PATH",
            "output": [],
            "duration": time.monotonic() - start,
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


async def _compile_via_executor(
    server: Any,
    ivy_executor: Any,
    base_path: str,
    filepath: str,
    token: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """Run ivyc through the Docker-aware IvyExecutor.

    Wraps the synchronous executor.execute() in a thread to avoid
    blocking the async event loop.
    """
    from pathlib import Path as P

    from panther_ivy.api.compiler import generate_compile_commands

    compile_result = generate_compile_commands(
        ivy_file=P(filepath),
        base_path=base_path,
    )

    start = time.monotonic()

    # Run setup + compile in a thread to avoid blocking the event loop
    loop = asyncio.get_running_loop()

    def _do_compile():
        ivy_executor.execute(
            compile_result.setup_commands,
            workspace_root=os.path.dirname(filepath),
            timeout=30,
        )
        return ivy_executor.execute(
            compile_result.compile_commands,
            workspace_root=os.path.dirname(filepath),
            timeout=300,
        )

    exec_result = await loop.run_in_executor(None, _do_compile)
    duration = time.monotonic() - start

    output_lines = (exec_result.stderr + "\n" + exec_result.stdout).splitlines()

    return {
        "success": exec_result.exit_code == 0,
        "message": "OK" if exec_result.exit_code == 0 else f"Exit code {exec_result.exit_code}",
        "output": output_lines,
        "target": exec_result.target,
        "duration": duration,
    }


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
        all_isolates = _collect_all_isolates(server, enclosing_test, "")
        if module_basename in all_isolates:
            isolate = module_basename

    return enclosing_test, isolate, True


def register(server: Any) -> None:
    """Register custom Ivy command handlers."""

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

        # Smart isolate detection
        position = None
        raw_pos = getattr(params, "position", None)
        if raw_pos is not None:
            position = lsp.Position(line=raw_pos.line, character=raw_pos.character)

        isolate = getattr(params, "isolate", None)
        if isolate is None and position is not None:
            isolate = _detect_isolate_at_position(server, uri, position)

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
                    uri=uri, diagnostics=base_diags + deep_diags
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

        # Try Docker executor if available
        ivy_executor = getattr(server, "_ivy_executor", None)
        ivy_base_path = getattr(server, "_ivy_base_path", None)
        if ivy_executor is not None and ivy_base_path is not None:
            try:
                result = await _compile_via_executor(
                    server, ivy_executor, ivy_base_path,
                    filepath, token,
                )
                _track_end(server, op_id, result)
                return result
            except Exception:
                logger.warning(
                    "Docker executor failed; falling back to native subprocess",
                    exc_info=True,
                )

        # Native subprocess fallback
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

        # Smart isolate detection (same pattern as ivy/verify)
        position = None
        raw_pos = getattr(params, "position", None)
        if raw_pos is not None:
            position = lsp.Position(line=raw_pos.line, character=raw_pos.character)

        # Accept explicit isolate from extension (e.g., quick pick retry)
        isolate = getattr(params, "isolate", None)

        # Try cursor-based detection
        if isolate is None and position is not None:
            isolate = _detect_isolate_at_position(server, uri, position)

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

    def _refresh_open_diagnostics(srv) -> None:
        """Re-publish diagnostics for all open documents.

        Called after active test scope changes so that scoped
        diagnostic filtering takes effect immediately.
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
                    lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
                )
            except Exception:
                logger.warning(
                    "Failed to refresh diagnostics for %s", uri, exc_info=True
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

        _refresh_open_diagnostics(server)

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
                pass

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
        _refresh_open_diagnostics(server)

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
