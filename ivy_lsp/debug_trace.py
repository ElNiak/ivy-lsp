"""Opt-in debug tracing for the Ivy Language Server.

Provides a human-readable debug log file that traces:
- Parser tier selection (which tier was used for each file and why)
- MCP tool I/O (inputs, outputs, durations)
- LSP feature responses (hover, definition, symbols, references)

Enabled via ``IVY_LSP_DEBUG_LOG=1``.  Log file defaults to
``/tmp/ivy-lsp-debug-{hash}.log`` with 5 MB rotation (2 backups).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Generator, List, Optional


class DebugTracer:
    """Human-readable debug tracer writing to a dedicated log file.

    Uses a private Python logger (``ivy_lsp.debug_trace``) with
    ``propagate=False`` so output is isolated from stderr/LSP handlers.
    """

    _MAX_OUTPUT_LEN = 2000

    def __init__(self, log_path: str) -> None:
        """Initialize the tracer with a rotating log file at *log_path*."""
        self._logger = logging.getLogger("ivy_lsp.debug_trace")
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)

        # Remove any existing handlers (e.g. from re-init)
        for h in self._logger.handlers[:]:
            self._logger.removeHandler(h)

        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._log_path = log_path
        self._logger.debug(
            "=== IVY LSP DEBUG TRACE STARTED === [%s]",
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    @property
    def log_path(self) -> str:
        """Return the path to the active debug log file."""
        return self._log_path

    def trace_tier_selection(
        self,
        filepath: str,
        result: Any,
    ) -> None:
        """Log parser tier selection for a file.

        Args:
            filepath: The file that was parsed.
            result: An ``ExtractionResult`` with tier_used, errors, timing_ms,
                    symbol_count, and includes.
        """
        lines: List[str] = []
        lines.append("")
        lines.append(f"=== PARSER TIER === [{time.strftime('%Y-%m-%d %H:%M:%S')}]")
        lines.append(f"File:     {os.path.basename(filepath)}")

        # Report each tier attempt
        for err in getattr(result, "errors", []):
            tier = err.tier
            label = {1: "parser", 2: "lexer", 3: "regex"}.get(tier, f"tier{tier}")
            lines.append(
                f"Tier {tier} ({label}): FAILED after {err.timing_ms:.1f}ms "
                f"— {err.error_type}: {err.message}"
            )

        tier_used = getattr(result, "tier_used", 0)
        timing = getattr(result, "timing_ms", 0.0)
        sym_count = getattr(result, "symbol_count", 0)
        includes = getattr(result, "includes", [])
        label = {1: "parser", 2: "lexer", 3: "regex"}.get(tier_used, f"tier{tier_used}")
        lines.append(
            f"Tier {tier_used} ({label}): OK — "
            f"{sym_count} symbols, {len(includes)} includes in {timing:.1f}ms"
        )

        errors = getattr(result, "errors", [])
        if errors:
            lines.append(f"Result:   DEGRADED (tier {tier_used} of 3)")
        else:
            lines.append(f"Result:   OK (tier {tier_used})")

        self._logger.debug("\n".join(lines))

    def trace_mcp_call(
        self,
        tool_name: str,
        inputs: Dict[str, Any],
        output: Optional[str],
        duration_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """Log an MCP tool invocation.

        Args:
            tool_name: Name of the MCP tool.
            inputs: Tool input arguments.
            output: JSON string result (truncated if large).
            duration_ms: Execution duration in milliseconds.
            error: Error message if the tool failed.
        """
        lines: List[str] = []
        lines.append("")
        lines.append(f"=== MCP TOOL === [{time.strftime('%Y-%m-%d %H:%M:%S')}]")
        lines.append(f"Tool:     {tool_name}")

        # Format inputs compactly
        import json

        try:
            input_str = json.dumps(inputs, default=str)
        except (TypeError, ValueError):
            input_str = str(inputs)
        lines.append(f"Input:    {input_str}")
        lines.append(f"Duration: {duration_ms:.0f}ms")

        if error:
            lines.append(f"Error:    {error}")
        elif output is not None:
            total_bytes = len(output)
            if total_bytes > self._MAX_OUTPUT_LEN:
                truncated = output[: self._MAX_OUTPUT_LEN]
                lines.append(
                    f"Output:   {truncated} [... truncated, {total_bytes} total bytes]"
                )
            else:
                lines.append(f"Output:   {output} [{total_bytes} bytes]")

        self._logger.debug("\n".join(lines))

    def trace_lsp_request(
        self,
        method: str,
        filepath: str,
        position: Optional[str] = None,
        word: Optional[str] = None,
        source: Optional[str] = None,
        result_summary: Optional[str] = None,
    ) -> None:
        """Log an LSP feature request and response.

        Args:
            method: LSP method name (e.g. ``textDocument/hover``).
            filepath: File being queried.
            position: Cursor position string (e.g. ``"42:15"``).
            word: Word at cursor.
            source: Which data path produced the result.
            result_summary: Short description of the result.
        """
        lines: List[str] = []
        lines.append("")
        lines.append(f"=== LSP REQUEST === [{time.strftime('%Y-%m-%d %H:%M:%S')}]")
        lines.append(f"Method:   {method}")

        loc_parts = [os.path.basename(filepath)]
        if position:
            loc_parts.append(position)
        lines.append(f"File:     {':'.join(loc_parts)}")

        if word:
            lines.append(f'Word:     "{word}"')
        if source:
            lines.append(f"Source:   {source}")
        if result_summary:
            lines.append(f"Result:   {result_summary}")
        else:
            lines.append("Result:   (none)")

        self._logger.debug("\n".join(lines))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tracer: Optional[DebugTracer] = None


def get_tracer() -> Optional[DebugTracer]:
    """Return the global DebugTracer, or ``None`` when tracing is disabled."""
    return _tracer


def init_tracer(
    workspace_root: Optional[str] = None,
    log_path: Optional[str] = None,
) -> DebugTracer:
    """Initialize the global DebugTracer and return it.

    Args:
        workspace_root: Used to generate a deterministic log filename.
        log_path: Explicit path override (from ``IVY_LSP_DEBUG_LOG_PATH``).
    """
    global _tracer

    if log_path is None:
        # Generate deterministic filename from workspace root
        key = (workspace_root or "default").encode()
        h = hashlib.sha256(key).hexdigest()[:8]
        log_path = f"/tmp/ivy-lsp-debug-{h}.log"

    _tracer = DebugTracer(log_path)
    return _tracer


class ToolTraceContext:
    """Lightweight start/end tracing for MCP tools.

    Usage::

        _tc = ToolTraceContext("ivy_verify", {"path": p})
        ...
        return _tc.finish(result_json)
    """

    __slots__ = ("_tool_name", "_inputs", "_t0", "_tracer")

    def __init__(self, tool_name: str, inputs: Dict[str, Any]) -> None:
        """Start tracing a tool call; call :meth:`finish` with the result."""
        self._tool_name = tool_name
        self._inputs = inputs
        self._tracer = get_tracer()
        self._t0 = time.monotonic() if self._tracer else 0.0

    def finish(self, result: str) -> str:
        """Log the tool result and return it unchanged (passthrough)."""
        if self._tracer is not None:
            elapsed = (time.monotonic() - self._t0) * 1000
            self._tracer.trace_mcp_call(
                tool_name=self._tool_name,
                inputs=self._inputs,
                output=result,
                duration_ms=elapsed,
            )
        return result

    def finish_error(self, result: str, error: str) -> str:
        """Log the tool error and return the result unchanged."""
        if self._tracer is not None:
            elapsed = (time.monotonic() - self._t0) * 1000
            self._tracer.trace_mcp_call(
                tool_name=self._tool_name,
                inputs=self._inputs,
                output=result,
                duration_ms=elapsed,
                error=error,
            )
        return result


@contextmanager
def trace_tool(
    tool_name: str,
    inputs: Dict[str, Any],
) -> Generator[List[Optional[str]], None, None]:
    """Context manager for tracing MCP tool calls.

    Usage::

        with trace_tool("ivy_verify", {"path": p}) as holder:
            result_json = do_work()
            holder[0] = result_json
            return result_json

    The holder is a single-element list: ``[None]``.  Set ``holder[0]``
    to the result string before exiting the ``with`` block.
    """
    tracer = get_tracer()
    if tracer is None:
        # No-op: yield and return
        holder: List[Optional[str]] = [None]
        yield holder
        return

    holder = [None]
    t0 = time.monotonic()
    error_msg: Optional[str] = None
    try:
        yield holder
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed = (time.monotonic() - t0) * 1000
        tracer.trace_mcp_call(
            tool_name=tool_name,
            inputs=inputs,
            output=holder[0],
            duration_ms=elapsed,
            error=error_msg,
        )
