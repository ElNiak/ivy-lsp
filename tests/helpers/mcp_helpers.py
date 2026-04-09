"""Shared MCP test helpers — consolidated from test_tools_*.py."""

from __future__ import annotations

import json


def get_mcp_app(workspace_root: str | None = None):
    """Create a FastMCP app for testing.

    Uses start_mcp(_return_app=True) from ivy_lsp.mcp.server.
    """
    from ivy_lsp.mcp.startup import start_mcp

    root = workspace_root or "/tmp/test-workspace"
    return start_mcp(workspace_root=root, _return_app=True)


def extract_text(result) -> str:
    """Normalize MCP tool response to a plain text string.

    Handles multiple response shapes:
    - direct string
    - dict with 'result' key (returns the result value)
    - dict with 'content' key containing TextContent blocks
    - other dict (json.dumps fallback)
    - tuple (content_list, ...) with optional result in second element
    - list of content blocks
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if "result" in result:
            return result["result"]
        if "content" in result:
            result = result["content"]
        else:
            return json.dumps(result)
    if isinstance(result, tuple):
        content_blocks = result[0]
        if len(result) > 1 and isinstance(result[1], dict) and "result" in result[1]:
            return result[1]["result"]
        result = content_blocks
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
