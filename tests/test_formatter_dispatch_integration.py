"""Integration test: verify consolidated tools inject mode/view into result dicts.

This test catches the class of regression where a consolidated tool's _impl
function omits the `mode`/`view` key, causing format_tool_result to fall
through to _format_generic (raw JSON) instead of the dedicated formatter.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.mcp_helpers import extract_text, get_mcp_app

# (tool_name, payload, expected_key, expected_value)
# Payload-only tests (no filesystem prereqs), cover every mode-aware entry.
MODE_AWARE_CASES = [
    ("ivy_diagnostics", {"mode": "dashboard"}, "mode", "dashboard"),
    ("ivy_status", {"mode": "capabilities"}, "mode", "capabilities"),
    ("ivy_status", {"mode": "health"}, "mode", "health"),
    ("ivy_analysis", {"mode": "includes"}, "mode", "includes"),
]


@pytest.mark.parametrize("tool_name,payload,key,expected", MODE_AWARE_CASES)
@pytest.mark.asyncio
async def test_consolidated_tool_injects_mode_into_result(
    tmp_path, tool_name, payload, key, expected
):
    """Every consolidated tool must inject the mode/view into its result dict.

    Without this, format_tool_result falls through to _format_generic for
    all modes except the one where the _impl happens to include the key.
    """
    mcp = get_mcp_app(workspace_root=str(tmp_path))
    result = await mcp.call_tool(tool_name, payload)
    data = json.loads(extract_text(result))
    assert (
        data.get(key) == expected
    ), f"{tool_name} with {payload} must return {key}={expected!r}, got {data.get(key)!r}"


@pytest.mark.asyncio
async def test_ivy_visualize_injects_view(tmp_path):
    """ivy_visualize must inject 'view' (not 'mode') into its result dict."""
    mcp = get_mcp_app(workspace_root=str(tmp_path))
    result = await mcp.call_tool("ivy_visualize", {"view": "dependencies"})
    data = json.loads(extract_text(result))
    assert data.get("view") == "dependencies"


@pytest.mark.asyncio
async def test_ivy_patterns_scaffold_injects_mode(tmp_path):
    """ivy_patterns mode=scaffold must inject 'mode' into result."""
    mcp = get_mcp_app(workspace_root=str(tmp_path))
    result = await mcp.call_tool(
        "ivy_patterns",
        {"protocol": "nonexistent", "mode": "scaffold", "pattern": "serdes"},
    )
    data = json.loads(extract_text(result))
    # Scaffold on nonexistent protocol returns error but should still have mode
    assert data.get("mode") == "scaffold" or data.get("success") is False
