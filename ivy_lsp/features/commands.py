"""Custom Ivy tool commands: verify, compile, show model."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Sequence, Union

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel

logger = logging.getLogger(__name__)

_VALID_IVY_PARAM = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def _validate_ivy_param(value: str) -> str:
    """Validate an Ivy CLI parameter (isolate name, target, etc.).

    NOTE: Duplicated in mcp_server.py — keep both in sync.
    """
    if not value or not _VALID_IVY_PARAM.match(value):
        raise ValueError(f"Invalid Ivy parameter: {value!r}")
    return value


DEFAULT_VERIFY_TIMEOUT = 120.0
DEFAULT_COMPILE_TIMEOUT = 300.0
DEFAULT_SHOW_MODEL_TIMEOUT = 30.0


def _find_tool(name: str) -> Optional[str]:
    """Check if an Ivy CLI tool is available on PATH."""
    return shutil.which(name)


def _resolve_via_staging(server: Any, filepath: str) -> str:
    """Return the staging-directory path for *filepath* if available.

    Ivy CLI tools resolve ``include`` directives relative to the input
    file's directory.  By redirecting the tool to the staging directory
    -- where every workspace .ivy file has a flat symlink -- the tool
    can find all includes regardless of the original directory structure.

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

    filepath = uri.replace("file://", "")
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


async def _run_tool(
    cmd: List[str],
    timeout: float,
    server: Any,
    token: Optional[Union[str, int]] = None,
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


def register(server: Any) -> None:
    """Register custom Ivy command handlers."""

    @server.feature("ivy/verify")
    async def ivy_verify(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri.replace("file://", "")
        token = getattr(params, "workDoneToken", None)

        # Smart isolate detection
        position = None
        raw_pos = getattr(params, "position", None)
        if raw_pos is not None:
            position = lsp.Position(line=raw_pos.line, character=raw_pos.character)

        isolate = getattr(params, "isolate", None)
        if isolate is None and position is not None:
            isolate = _detect_isolate_at_position(server, uri, position)

        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={_validate_ivy_param(isolate)}")
        cmd.append(staged_filepath)

        result = await _run_tool(cmd, DEFAULT_VERIFY_TIMEOUT, server, token)
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

        return result

    @server.feature("ivy/compile")
    async def ivy_compile(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri.replace("file://", "")
        token = getattr(params, "workDoneToken", None)
        target = getattr(params, "target", "test")

        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivyc", f"target={_validate_ivy_param(target)}", staged_filepath]
        result = await _run_tool(cmd, DEFAULT_COMPILE_TIMEOUT, server, token)
        return result

    @server.feature("ivy/showModel")
    async def ivy_show_model(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri.replace("file://", "")
        token = getattr(params, "workDoneToken", None)

        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_show", staged_filepath]
        result = await _run_tool(
            cmd, DEFAULT_SHOW_MODEL_TIMEOUT, server, token
        )
        return result

    @server.feature("ivy/capabilities")
    def ivy_capabilities(params: Any = None) -> Dict[str, Any]:
        return {
            "fullMode": getattr(server, "_full_mode", False),
            "ivyCheckAvailable": _find_tool("ivy_check") is not None,
            "ivycAvailable": _find_tool("ivyc") is not None,
            "ivyShowAvailable": _find_tool("ivy_show") is not None,
        }

    @server.feature("ivy/setActiveTest")
    def ivy_set_active_test(params) -> Dict[str, Any]:
        """Set the active test scope for diagnostics and code lenses.

        The client is responsible for triggering a diagnostic refresh
        after a successful response.
        """
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

        return {
            "success": True,
            "activeTest": active.test_file if active else None,
        }

    @server.feature("ivy/listTests")
    def ivy_list_tests(params: Any = None) -> Dict[str, Any]:
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
    async def ivy_compile_test(params) -> Dict[str, Any]:
        """Compile a specific test file with ivyc target=test."""
        test_file = getattr(params, "testFile", None)
        if not test_file:
            return {
                "success": False,
                "message": "No testFile specified",
                "output": [],
                "duration": 0.0,
            }

        token = getattr(params, "workDoneToken", None)
        staged = _resolve_via_staging(server, test_file)
        cmd = ["ivyc", "target=test", staged]
        result = await _run_tool(cmd, DEFAULT_COMPILE_TIMEOUT, server, token)

        # Store compilation result in scoped model if available
        try:
            graph = server._indexer._requirement_graph
            if isinstance(graph, ScopedRequirementModel):
                graph._compilation_results[test_file] = result
        except AttributeError:
            pass

        return result
