"""Markdown formatters for MCP tool results.

Each MCP tool has a dedicated formatter that converts its JSON result dict
into human-readable markdown.  ``format_tool_result()`` dispatches by tool
name; ``format_error()`` handles error/timeout responses.

The ``safe_tool`` decorator in ``tools/__init__`` calls these after the
tool handler returns, so tool files themselves remain unchanged (Phase 1).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ivy_lsp.mcp.tools.formatters.primitives import _code_block, _format_context_banner
from ivy_lsp.mcp.tools.formatters.traceability import (
    _format_ivy_coverage,
    _format_ivy_extract_requirements,
    _format_ivy_manifest,
)
from ivy_lsp.mcp.tools.formatters.verification import (
    _format_ivy_compile,
    _format_ivy_diagnostics,
    _format_ivy_model_info,
    _format_ivy_verification_dashboard,
    _format_ivy_verify,
)
from ivy_lsp.mcp.tools.formatters.visualization import (
    _format_ivy_capabilities,
    _format_ivy_health_check,
    _format_ivy_include_graph,
    _format_ivy_model_summary,
    _format_ivy_pattern_scaffold,
    _format_ivy_patterns,
    _format_ivy_quality,
    _format_ivy_scope,
    _format_ivy_visualize,
)

# ---------------------------------------------------------------------------
# Import per-tool formatters from sub-modules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error formatter
# ---------------------------------------------------------------------------


def format_error(data: dict) -> str:
    """Format an error/timeout response as markdown."""
    msg = data.get("message") or data.get("error") or "Unknown error"
    parts = [f"**Error** -- {msg}"]

    note = data.get("note")
    if note:
        parts.append(f"\n> {note}")

    if data.get("timeout"):
        tool = data.get("tool", "unknown")
        parts.append(f"\nTool `{tool}` exceeded its timeout limit.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------


def _format_generic(data: dict) -> str:
    """Render any dict as indented JSON in a code fence (fallback)."""
    # Remove internal fields
    cleaned = {k: v for k, v in data.items() if not k.startswith("_")}
    return _code_block(json.dumps(cleaned, indent=2), "json")


# ---------------------------------------------------------------------------
# Dispatch registry
# ---------------------------------------------------------------------------

_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "ivy_verify": _format_ivy_verify,
    "ivy_compile": _format_ivy_compile,
    "ivy_model_info": _format_ivy_model_info,
    "ivy_diagnostics": _format_ivy_diagnostics,
    "ivy_verification_dashboard": _format_ivy_verification_dashboard,
    "ivy_include_graph": _format_ivy_include_graph,
    "ivy_capabilities": _format_ivy_capabilities,
    "ivy_scope": _format_ivy_scope,
    "ivy_health_check": _format_ivy_health_check,
    "ivy_coverage": _format_ivy_coverage,
    "ivy_extract_requirements": _format_ivy_extract_requirements,
    "ivy_manifest": _format_ivy_manifest,
    "ivy_visualize": _format_ivy_visualize,
    "ivy_model_summary": _format_ivy_model_summary,
    "ivy_patterns": _format_ivy_patterns,
    "ivy_pattern_scaffold": _format_ivy_pattern_scaffold,
    "ivy_quality": _format_ivy_quality,
}


def format_tool_result(tool_name: str, data: dict) -> str:
    """Dispatch to a per-tool formatter, falling back to generic."""
    if not isinstance(data, dict):
        return _code_block(str(data))
    formatter = _FORMATTERS.get(tool_name, _format_generic)
    banner = _format_context_banner(data)
    try:
        body = formatter(data)
    except Exception:
        try:
            body = _format_generic(data)
        except Exception:
            body = _code_block(str(data))
    return (banner + "\n" + body) if banner else body


__all__ = ["format_error", "format_tool_result", "_format_generic"]
