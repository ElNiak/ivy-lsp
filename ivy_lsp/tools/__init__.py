"""MCP tool modules for ivy-lsp.

Each sub-module registers a logical group of ``@mcp.tool()`` handlers.
``register_all_tools()`` is the single entry-point called by
``mcp_server.start_mcp()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


__all__ = ["register_all_tools"]
