"""Custom Ivy tool commands: verify, compile, show model."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any, Dict, List, Optional, Sequence, Union

from lsprotocol import types as lsp

logger = logging.getLogger(__name__)

DEFAULT_VERIFY_TIMEOUT = 120.0
DEFAULT_COMPILE_TIMEOUT = 300.0
DEFAULT_SHOW_MODEL_TIMEOUT = 30.0


def _find_tool(name: str) -> Optional[str]:
    """Check if an Ivy CLI tool is available on PATH."""
    return shutil.which(name)


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
            await server.progress.create_async(token)
            server.progress.begin(
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
            await proc.wait()
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
                server.progress.end(
                    token, lsp.WorkDoneProgressEnd(message="Done")
                )
            except Exception:
                pass


def register(server: Any) -> None:
    """Register custom Ivy command handlers."""

    @server.feature("ivy/verify")
    async def ivy_verify(params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params["textDocument"]["uri"]
        filepath = uri.replace("file://", "")
        token = params.get("workDoneToken")

        # Smart isolate detection
        position = None
        if params.get("position") is not None:
            p = params["position"]
            position = lsp.Position(line=p["line"], character=p["character"])

        isolate = params.get("isolate")
        if isolate is None and position is not None:
            isolate = _detect_isolate_at_position(server, uri, position)

        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(filepath)

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
    async def ivy_compile(params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params["textDocument"]["uri"]
        filepath = uri.replace("file://", "")
        token = params.get("workDoneToken")
        target = params.get("target", "test")

        cmd = ["ivyc", f"target={target}", filepath]
        result = await _run_tool(cmd, DEFAULT_COMPILE_TIMEOUT, server, token)
        return result

    @server.feature("ivy/showModel")
    async def ivy_show_model(params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params["textDocument"]["uri"]
        filepath = uri.replace("file://", "")
        token = params.get("workDoneToken")

        cmd = ["ivy_show", filepath]
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
