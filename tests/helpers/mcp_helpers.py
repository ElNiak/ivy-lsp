"""Shared MCP test helpers — consolidated from test_tools_*.py."""

from __future__ import annotations

import json
from typing import Any


def get_mcp_app(workspace_root: str | None = None):
    """Create a FastMCP app for testing.

    Uses start_mcp(_return_app=True) from ivy_lsp.mcp_server.
    """
    from ivy_lsp.mcp_server import start_mcp

    root = workspace_root or "/tmp/test-workspace"
    return start_mcp(workspace_root=root, _return_app=True)


def extract_text(result) -> str:
    """Normalize MCP tool response to a plain text string.

    Handles multiple response shapes:
    - dict with 'content' key containing TextContent blocks
    - tuple (content_list, is_error)
    - list of content blocks
    - direct string
    """
    if isinstance(result, str):
        return result
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict) and "content" in result:
        result = result["content"]
    if isinstance(result, list):
        parts = []
        for item in result:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(result)


def extract_json(result) -> dict[str, Any]:
    """Normalize MCP tool response to a parsed JSON dict."""
    return json.loads(extract_text(result))
