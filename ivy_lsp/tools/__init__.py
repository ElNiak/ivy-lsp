"""MCP tool modules for ivy-lsp.

Each sub-module registers a logical group of ``@mcp.tool()`` handlers.
``register_all_tools()`` is the single entry-point called by
``mcp_server.start_mcp()``.
"""

from __future__ import annotations

import functools
import json
import logging
import types
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


def error_response(message: str) -> str:
    """Return a JSON error response string."""
    return json.dumps({"success": False, "message": message})


def safe_tool(fn):
    """Decorator that catches unhandled exceptions in MCP tool handlers.

    Returns an ``error_response(...)`` JSON string instead of letting the
    exception propagate through FastMCP/uvicorn and kill the sidecar.

    The wrapper is rebuilt with the original function's ``__globals__`` so
    that FastMCP can resolve ``ForwardRef`` type annotations (``Literal``,
    etc.) that result from ``from __future__ import annotations``.
    """

    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            logger.error(
                "Unhandled exception in MCP tool %s: %s",
                fn.__name__,
                exc,
                exc_info=True,
            )
            return error_response(f"Internal error in {fn.__name__}: {exc}")

    # FastMCP resolves ForwardRef type annotations using func.__globals__.
    # The wrapper lives in tools/__init__.py whose globals lack Literal and
    # other imports from tool modules.  Rebuild the wrapper with fn's
    # __globals__ — all names the wrapper body references (logger,
    # error_response) are also available there via each tool module's own
    # imports.
    wrapper = types.FunctionType(
        _wrapper.__code__,
        fn.__globals__,
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


__all__ = ["error_response", "register_all_tools", "safe_tool"]
