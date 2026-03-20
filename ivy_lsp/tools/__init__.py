"""MCP tool modules for ivy-lsp.

Each sub-module registers a logical group of ``@mcp.tool()`` handlers.
``register_all_tools()`` is the single entry-point called by
``mcp_server.start_mcp()``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
import types
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-tool timeout configuration (seconds)
# ---------------------------------------------------------------------------

_TOOL_TIMEOUTS: dict[str, float] = {
    "ivy_verify": 180.0,
    "ivy_compile": 360.0,
    "ivy_model_info": 60.0,
    "ivy_diagnostics": 120.0,
    "ivy_include_graph": 60.0,
    "ivy_capabilities": 10.0,
    "ivy_coverage": 120.0,
    "ivy_extract_requirements": 30.0,
    "ivy_manifest": 60.0,
    "ivy_visualize": 60.0,
    "ivy_model_summary": 60.0,
    "ivy_patterns": 60.0,
    "ivy_pattern_scaffold": 30.0,
    "ivy_quality": 60.0,
    "ivy_scope": 30.0,
    "ivy_verification_dashboard": 30.0,
    "ivy_health_check": 10.0,
}

_DEFAULT_TIMEOUT: float = 60.0

# ---------------------------------------------------------------------------
# Per-tool metadata (cost, category, model dependency)
# ---------------------------------------------------------------------------

_TOOL_METADATA: dict[str, dict[str, Any]] = {
    "ivy_verify": {"cost": "high", "category": "verification", "needs_model": False},
    "ivy_compile": {"cost": "high", "category": "verification", "needs_model": False},
    "ivy_model_info": {"cost": "medium", "category": "analysis", "needs_model": False},
    "ivy_diagnostics": {"cost": "medium", "category": "analysis", "needs_model": True},
    "ivy_include_graph": {
        "cost": "medium",
        "category": "analysis",
        "needs_model": False,
    },
    "ivy_capabilities": {"cost": "low", "category": "analysis", "needs_model": False},
    "ivy_coverage": {"cost": "high", "category": "traceability", "needs_model": True},
    "ivy_extract_requirements": {
        "cost": "low",
        "category": "traceability",
        "needs_model": False,
    },
    "ivy_manifest": {
        "cost": "medium",
        "category": "traceability",
        "needs_model": False,
    },
    "ivy_visualize": {
        "cost": "medium",
        "category": "visualization",
        "needs_model": True,
    },
    "ivy_model_summary": {
        "cost": "medium",
        "category": "visualization",
        "needs_model": True,
    },
    "ivy_patterns": {"cost": "medium", "category": "patterns", "needs_model": False},
    "ivy_pattern_scaffold": {
        "cost": "low",
        "category": "patterns",
        "needs_model": False,
    },
    "ivy_quality": {"cost": "medium", "category": "quality", "needs_model": True},
    "ivy_scope": {"cost": "low", "category": "analysis", "needs_model": True},
    "ivy_verification_dashboard": {
        "cost": "low",
        "category": "verification",
        "needs_model": False,
    },
    "ivy_health_check": {"cost": "low", "category": "analysis", "needs_model": False},
}


def get_tool_metadata(tool_name: str | None = None) -> dict[str, Any]:
    """Return metadata for *tool_name*, or all metadata if *tool_name* is None."""
    if tool_name is not None:
        return dict(_TOOL_METADATA.get(tool_name, {}))
    return {k: dict(v) for k, v in _TOOL_METADATA.items()}


# ---------------------------------------------------------------------------
# Tool metrics (Phase 7 telemetry will consume these)
# ---------------------------------------------------------------------------


@dataclass
class ToolMetrics:
    """Per-tool call metrics."""

    call_count: int = 0
    total_duration: float = 0.0
    error_count: int = 0
    timeout_count: int = 0


_tool_metrics: dict[str, ToolMetrics] = {}

# Lazily initialised semaphore for concurrency enforcement.
_tool_semaphore: asyncio.Semaphore | None = None


def get_tool_metrics() -> dict[str, ToolMetrics]:
    """Return a shallow copy of per-tool metrics."""
    return dict(_tool_metrics)


def _get_effective_timeout(tool_name: str) -> float:
    """Compute the effective timeout for *tool_name*.

    Resolution order:
    1. Per-tool env override  ``IVY_LSP_TOOL_TIMEOUT_<TOOL_NAME_UPPER>``
    2. Base timeout from ``_TOOL_TIMEOUTS`` (or ``_DEFAULT_TIMEOUT``)
       scaled by ``get_config().tool_timeout_scale``
    """
    # Lazy import to avoid circular dependency (config -> tools -> config).
    from ivy_lsp.config import get_config

    cfg = get_config()

    # 1. Per-tool env var override (unscaled – explicit value wins)
    env_key = f"IVY_LSP_TOOL_TIMEOUT_{tool_name.upper()}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        try:
            return max(1.0, float(env_val))
        except (ValueError, TypeError):
            pass

    # 2. Base * scale
    base = _TOOL_TIMEOUTS.get(tool_name, _DEFAULT_TIMEOUT)
    return max(1.0, base * cfg.tool_timeout_scale)


def _ensure_semaphore() -> asyncio.Semaphore:
    """Return the module-level semaphore, creating it on first use.

    Must be called from within a running event loop.
    """
    global _tool_semaphore
    if _tool_semaphore is None:
        from ivy_lsp.config import get_config

        _tool_semaphore = asyncio.Semaphore(get_config().max_concurrent_tools)
    return _tool_semaphore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def error_response(message: str) -> dict:
    """Return an error response dict."""
    return {"success": False, "message": message}


def _timeout_response(tool_name: str, timeout: float) -> dict:
    """Return a response dict for a timed-out tool call."""
    return {
        "success": False,
        "message": f"Tool timed out after {timeout:.0f}s",
        "timeout": True,
        "tool": tool_name,
    }


# ---------------------------------------------------------------------------
# Markdown formatting layer
# ---------------------------------------------------------------------------

from ivy_lsp.tools.formatters import format_error, format_tool_result


def _truncate_if_needed(result_str: str) -> str:
    """Truncate result string if it exceeds the configured max_result_chars."""
    from ivy_lsp.config import get_config

    max_chars = get_config().max_result_chars
    if max_chars > 0 and len(result_str) > max_chars:
        return (
            result_str[:max_chars]
            + "\n\n---\n*Truncated at "
            + str(max_chars)
            + " chars. Use more specific parameters to narrow results.*"
        )
    return result_str


def _format_result(tool_name: str, result: object) -> str | object:
    """Post-process a tool result into markdown.

    Handles two cases:
    - ``str``: Parse JSON, then dispatch to the per-tool formatter.
    - ``dict``: Dispatch directly (Phase 2 — tools returning dicts).

    If the result is neither, or parsing fails, return it unchanged.

    Set ``IVY_LSP_RAW_JSON=1`` to disable formatting (useful for tests
    that assert on the raw JSON structure).
    """
    if os.environ.get("IVY_LSP_RAW_JSON"):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return _truncate_if_needed(result)
        if isinstance(parsed, dict):
            if not parsed.get("success", True):
                return format_error(parsed)
            return _truncate_if_needed(format_tool_result(tool_name, parsed))
        return _truncate_if_needed(result)
    if isinstance(result, dict):
        if not result.get("success", True):
            return format_error(result)
        return _truncate_if_needed(format_tool_result(tool_name, result))
    return result


def _summarize_for_log(value: Any, max_len: int = 240) -> Any:
    """Return a compact, JSON-safe summary for observability logs."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > max_len:
            return value[:max_len] + "..."
        return value

    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 8:
                summary["..."] = f"{len(value)} keys"
                break
            summary[str(key)] = _summarize_for_log(item, max_len=max_len)
        return summary

    if isinstance(value, (list, tuple, set)):
        seq = list(islice(value, 8))
        compact = [_summarize_for_log(item, max_len=max_len) for item in seq]
        if len(value) > len(seq):
            compact.append(f"... ({len(value)} total)")
        return compact

    rendered = repr(value)
    if len(rendered) > max_len:
        rendered = rendered[:max_len] + "..."
    return rendered


# ---------------------------------------------------------------------------
# safe_tool – the single decorator applied to every MCP tool handler
# ---------------------------------------------------------------------------


def safe_tool(fn):
    """Decorator that adds timeout, concurrency, metrics, and crash safety.

    Applied to every MCP tool handler.  Responsibilities:

    1. Acquires a concurrency semaphore (lazy-init from config).
    2. Enforces a per-tool timeout via ``asyncio.wait_for``.
    3. Records call metrics (count, duration, errors, timeouts).
    4. Catches unhandled exceptions and returns a JSON error instead of
       crashing the sidecar process.

    The wrapper is rebuilt with the original function's ``__globals__`` so
    that FastMCP can resolve ``ForwardRef`` type annotations (``Literal``,
    etc.) that result from ``from __future__ import annotations``.
    """

    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        from ivy_lsp.config import get_config
        from ivy_lsp.session_observability import get_session_logger

        tool_name = fn.__name__
        timeout = _get_effective_timeout(tool_name)
        sem = _ensure_semaphore()
        cfg = get_config()
        call_id = f"{tool_name}-{int(time.time() * 1000)}-{id(asyncio.current_task())}"
        logger_session = get_session_logger()

        arg_summary = [_summarize_for_log(arg) for arg in list(args)[:4]]
        kw_summary = {k: _summarize_for_log(v) for k, v in list(kwargs.items())[:8]}

        # Ensure per-tool metrics bucket exists.
        if tool_name not in _tool_metrics:
            _tool_metrics[tool_name] = ToolMetrics()
        metrics = _tool_metrics[tool_name]

        start = time.monotonic()
        try:
            if cfg.debug_log:
                logger.debug(
                    "[TOOL-START] %s call_id=%s timeout=%.1fs",
                    tool_name,
                    call_id,
                    timeout,
                )
                logger_session.log_event(
                    channel="mcp",
                    event_type="call_start",
                    name=tool_name,
                    status="started",
                    payload={"args": arg_summary, "kwargs": kw_summary},
                    call_id=call_id,
                )

            async with sem:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            result = _format_result(tool_name, result)

            if cfg.debug_log:
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.debug(
                    "[TOOL-END] %s call_id=%s duration_ms=%.0f",
                    tool_name,
                    call_id,
                    elapsed_ms,
                )
                logger_session.log_event(
                    channel="mcp",
                    event_type="call_end",
                    name=tool_name,
                    status="ok",
                    duration_ms=elapsed_ms,
                    payload={
                        "result_type": type(result).__name__,
                        "result": _summarize_for_log(result),
                    },
                    call_id=call_id,
                )

            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            metrics.timeout_count += 1
            metrics.error_count += 1
            logger.error(
                "MCP tool %s timed out after %.1fs (limit %.0fs)",
                tool_name,
                elapsed,
                timeout,
            )
            if cfg.debug_log:
                logger_session.log_event(
                    channel="mcp",
                    event_type="call_end",
                    name=tool_name,
                    status="timeout",
                    duration_ms=elapsed * 1000,
                    payload={"timeout_s": timeout},
                    call_id=call_id,
                )
            return format_error(
                {
                    "success": False,
                    "message": f"Tool timed out after {timeout:.0f}s",
                    "timeout": True,
                    "tool": tool_name,
                }
            )
        except Exception as exc:
            metrics.error_count += 1
            logger.error(
                "Unhandled exception in MCP tool %s: %s",
                tool_name,
                exc,
                exc_info=True,
            )
            if cfg.debug_log:
                logger_session.log_event(
                    channel="mcp",
                    event_type="call_end",
                    name=tool_name,
                    status="error",
                    duration_ms=(time.monotonic() - start) * 1000,
                    payload={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    call_id=call_id,
                )
            return format_error(
                {"success": False, "message": f"Internal error in {tool_name}: {exc}"}
            )
        finally:
            elapsed = time.monotonic() - start
            metrics.call_count += 1
            metrics.total_duration += elapsed

    # FastMCP resolves ForwardRef type annotations using func.__globals__.
    # The wrapper lives in tools/__init__.py whose globals lack Literal and
    # other imports from tool modules.  Rebuild the wrapper with fn's
    # __globals__ — all names the wrapper body references (logger,
    # error_response, _timeout_response, _get_effective_timeout,
    # _ensure_semaphore, _tool_metrics, ToolMetrics, asyncio, time) are
    # also available there via each tool module's own imports *or* via
    # closure.  We inject the missing names into fn's globals so the
    # rebuilt function can find them.
    _injected_names = {
        "logger": logger,
        "error_response": error_response,
        "_timeout_response": _timeout_response,
        "_get_effective_timeout": _get_effective_timeout,
        "_ensure_semaphore": _ensure_semaphore,
        "_tool_metrics": _tool_metrics,
        "ToolMetrics": ToolMetrics,
        "asyncio": asyncio,
        "time": time,
        "format_error": format_error,
        "format_tool_result": format_tool_result,
        "_format_result": _format_result,
        "_truncate_if_needed": _truncate_if_needed,
        "_summarize_for_log": _summarize_for_log,
    }
    patched_globals = {**fn.__globals__, **_injected_names}

    wrapper = types.FunctionType(
        _wrapper.__code__,
        patched_globals,
        _wrapper.__name__,
        _wrapper.__defaults__,
        _wrapper.__closure__,
    )
    functools.update_wrapper(wrapper, fn)
    return wrapper


from ivy_lsp.tools.analysis import register_analysis_tools
from ivy_lsp.tools.patterns import register_pattern_tools
from ivy_lsp.tools.quality import register_quality_tools
from ivy_lsp.tools.traceability import register_traceability_tools
from ivy_lsp.tools.verification import register_verification_tools
from ivy_lsp.tools.visualization import register_visualization_tools

if TYPE_CHECKING:
    from ivy_lsp.mcp_server import ToolContext


def register_all_tools(mcp: Any, ctx: ToolContext) -> None:
    """Register every MCP tool group on *mcp* using shared *ctx*."""
    register_verification_tools(mcp, ctx)
    register_analysis_tools(mcp, ctx)
    register_traceability_tools(mcp, ctx)
    register_visualization_tools(mcp, ctx)
    register_pattern_tools(mcp, ctx)
    register_quality_tools(mcp, ctx)


__all__ = [
    "ToolMetrics",
    "error_response",
    "get_tool_metadata",
    "get_tool_metrics",
    "register_all_tools",
    "safe_tool",
]
