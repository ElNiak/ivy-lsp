"""Log handlers, filters, and debug tracing for the Ivy LSP server.

Consolidates ivy_lsp.lsp_log_handler, ivy_lsp.utils.log_dedup_filter,
and ivy_lsp.debug_trace into a single module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, Tuple

from lsprotocol import types as lsp

if TYPE_CHECKING:
    from ivy_lsp.server import IvyLanguageServer


# --- LSP Log Handler ---


class LspLogHandler(logging.Handler):
    """Bridge Python logging -> LSP window/logMessage notifications.

    Rate-limited to prevent flooding the stdio pipe, which can cause
    write-side blocking and contribute to thread pool starvation.
    """

    _LEVEL_MAP = {
        logging.DEBUG: lsp.MessageType.Log,
        logging.INFO: lsp.MessageType.Info,
        logging.WARNING: lsp.MessageType.Warning,
        logging.ERROR: lsp.MessageType.Error,
        logging.CRITICAL: lsp.MessageType.Error,
    }

    _CAT_PRIORITY = {"MIL": 1, "DIA": 2, "PER": 3, "ACT": 4}
    _CAT_MIN_INTERVAL = {"MIL": 0.01, "DIA": 0.01, "PER": 0.1, "ACT": 0.1}
    _DEFAULT_MIN_INTERVAL = 0.05
    _MAX_MESSAGE_LEN = 8192  # 8 KB cap per log message

    _tls = threading.local()  # per-thread recursion guard

    def __init__(self, server: "IvyLanguageServer"):
        """Initialize with a reference to the language server."""
        super().__init__()
        self._server = server
        self._lock = threading.Lock()  # non-reentrant; no I/O under lock
        self._last_emit = 0.0
        self._drop_counts: dict = {}
        self._pipe_dead = False

    @staticmethod
    def _extract_category(msg: str) -> str:
        if msg.startswith("[MIL"):
            return "MIL"
        if msg.startswith("[ACT"):
            return "ACT"
        if msg.startswith("[DIA"):
            return "DIA"
        if msg.startswith("[PER"):
            return "PER"
        return ""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record as a ``window/logMessage`` notification."""
        if self._pipe_dead:
            return
        # Per-thread recursion guard: pygls logs inside _send_data(),
        # which would re-enter this handler. Skip to prevent infinite loop.
        if getattr(self._tls, "sending", False):
            return

        # --- fast path: all state under lock (no I/O) ---
        with self._lock:
            now = time.monotonic()
            if record.levelno < logging.WARNING:
                msg = self.format(record)
                cat = self._extract_category(msg)
                min_interval = self._CAT_MIN_INTERVAL.get(
                    cat, self._DEFAULT_MIN_INTERVAL
                )
                if getattr(self._server, "initializing", False):
                    min_interval = max(min_interval, 1.0)
                if (now - self._last_emit) < min_interval:
                    cat_key = cat or "_untagged"
                    self._drop_counts[cat_key] = self._drop_counts.get(cat_key, 0) + 1
                    return
            else:
                msg = self.format(record)

            if len(msg) > self._MAX_MESSAGE_LEN:
                msg = msg[: self._MAX_MESSAGE_LEN] + "... [truncated]"

            msg_type = self._LEVEL_MAP.get(record.levelno, lsp.MessageType.Log)
            if self._drop_counts:
                parts = []
                for k, v in sorted(self._drop_counts.items()):
                    label = k if k != "_untagged" else "other"
                    parts.append(f"{v} {label}")
                suppression = "[" + ", ".join(parts) + " messages suppressed]"
                msg = f"{msg} {suppression}"
                self._drop_counts = {}
            self._last_emit = now

        # --- slow path: send notification WITHOUT holding lock ---
        self._tls.sending = True
        try:
            self._server.window_log_message(
                lsp.LogMessageParams(type=msg_type, message=msg)
            )
        except Exception:
            self._pipe_dead = True
            try:
                sys.stderr.write(f"[ivy-lsp-fallback] {msg}\n")
                sys.stderr.flush()
            except Exception:
                pass
        finally:
            self._tls.sending = False


# --- Dedup Filter ---


class DedupFilter(logging.Filter):
    """Suppress duplicate log messages by (file, line, message) key.

    After the first occurrence, duplicates are counted silently.
    Every *summary_interval* seconds (default 60), a summary of
    suppressed duplicates is emitted at DEBUG level.
    """

    def __init__(self, summary_interval: float = 60.0) -> None:
        """Initialize with a summary interval in seconds."""
        super().__init__()
        self._seen: Dict[Tuple[str, int, str], int] = {}
        self._summary_interval = summary_interval
        self._last_summary = time.monotonic()

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for duplicate messages, True for first occurrence."""
        key = (
            getattr(record, "filename", ""),
            record.lineno,
            record.getMessage(),
        )

        count = self._seen.get(key, 0)
        self._seen[key] = count + 1

        if count == 0:
            # First occurrence — allow through
            return True

        # Check if it's time for a summary
        now = time.monotonic()
        if now - self._last_summary >= self._summary_interval:
            self._emit_summary(record)
            self._last_summary = now

        # Suppress duplicate
        return False

    def _emit_summary(self, record: logging.LogRecord) -> None:
        """Emit a summary of suppressed duplicates."""
        suppressed = {k: v for k, v in self._seen.items() if v > 1}
        if not suppressed:
            return
        total = sum(v - 1 for v in suppressed.values())
        top_3 = sorted(suppressed.items(), key=lambda x: x[1], reverse=True)[:3]
        details = ", ".join(
            f"{k[2][:60]}... (x{v})" if len(k[2]) > 60 else f"{k[2]} (x{v})"
            for k, v in top_3
        )
        record.msg = (
            f"[dedup] Suppressed {total} duplicate log messages. Top: {details}"
        )
        record.args = None

    def reset(self) -> None:
        """Clear dedup state (e.g. at session boundaries)."""
        self._seen.clear()
        self._last_summary = time.monotonic()


# --- Debug Tracer ---


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
        status: str = "ok",
        call_id: str = "",
    ) -> None:
        """Log an LSP feature request and response.

        Args:
            method: LSP method name (e.g. ``textDocument/hover``).
            filepath: File being queried.
            position: Cursor position string (e.g. ``"42:15"``).
            word: Word at cursor.
            source: Which data path produced the result.
            result_summary: Short description of the result.
            status: Outcome status (e.g. ``ok``, ``empty``, ``degraded``, ``error``).
            call_id: Optional correlation id for linking tool/lsp events.
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
        lines.append(f"Status:   {status}")
        if result_summary:
            lines.append(f"Result:   {result_summary}")
        else:
            lines.append("Result:   (none)")

        self._logger.debug("\n".join(lines))

        try:
            from ivy_lsp.infra.observability.session import get_session_logger

            get_session_logger().log_event(
                channel="lsp",
                event_type="request",
                name=method,
                status=status,
                call_id=call_id or None,
                payload={
                    "filepath": filepath,
                    "position": position,
                    "word": word,
                    "source": source,
                    "result_summary": result_summary,
                },
            )
        except (OSError, TypeError, ValueError):
            # Observability logging must never affect LSP behavior.
            pass


# --- Debug Tracer Singleton ---

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
        try:
            from ivy_lsp.infra.observability.session import (
                get_session_id,
                resolve_session_log_dir,
            )

            session_dir = resolve_session_log_dir(
                get_session_id(),
                workspace_root=workspace_root,
            )
            session_dir.mkdir(parents=True, exist_ok=True)
            log_path = str(session_dir / "debug-trace.log")
        except (OSError, TypeError, ValueError):
            # Generate deterministic filename from workspace root
            key = (workspace_root or "default").encode()
            h = hashlib.sha256(key).hexdigest()[:8]
            log_path = f"/tmp/ivy-lsp-debug-{h}.log"

    _tracer = DebugTracer(log_path)
    return _tracer


# --- Tool Trace Helpers ---


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

    def _record(self, result: Any, error: Optional[str] = None) -> None:
        """Record the trace if a tracer is active."""
        if self._tracer is not None:
            import json as _json

            elapsed = (time.monotonic() - self._t0) * 1000
            trace_output = _json.dumps(result) if isinstance(result, dict) else result
            self._tracer.trace_mcp_call(
                tool_name=self._tool_name,
                inputs=self._inputs,
                output=trace_output,
                duration_ms=elapsed,
                error=error,
            )

    def finish(self, result: Any) -> Any:
        """Log the tool result and return it unchanged (passthrough)."""
        self._record(result)
        return result


@contextmanager
def trace_tool(
    tool_name: str,
    inputs: Dict[str, Any],
) -> Generator[List[Any], None, None]:
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
        holder: List[Any] = [None]
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
        import json as _json

        elapsed = (time.monotonic() - t0) * 1000
        trace_output = holder[0]
        if isinstance(trace_output, dict):
            trace_output = _json.dumps(trace_output)
        tracer.trace_mcp_call(
            tool_name=tool_name,
            inputs=inputs,
            output=trace_output,
            duration_ms=elapsed,
            error=error_msg,
        )
