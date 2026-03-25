"""Tests for the ivy_workspace MCP tool.

Covers:
- set workspace at protocol level
- set workspace with unknown target -> error with available groups
- get workspace returns current state
- list workspaces returns available groups
- clear workspace resets state
- set workspace persists state file to disk
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


# ---------------------------------------------------------------------------
# Mock ToolContext matching the real ToolContext fields used by workspace tool
# ---------------------------------------------------------------------------


@dataclass
class MockToolContext:
    root: str
    active_workspace: Any = None
    workspace_groups: dict = field(default_factory=dict)
    include_resolver: Any = field(default_factory=MagicMock)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATE_FILENAME = ".ivy-workspace-state.json"


def _make_ctx(tmp_path: Path, groups: dict | None = None) -> MockToolContext:
    """Create a MockToolContext rooted at tmp_path with optional groups."""
    resolver = MagicMock()
    resolver._file_to_layer = {}
    ctx = MockToolContext(
        root=str(tmp_path),
        workspace_groups=groups or {},
        include_resolver=resolver,
    )
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_workspace_protocol(tmp_path):
    """Setting a workspace by protocol name activates the group's layers."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {
        "quic": ["quic_stack", "quic_tests", "tls_stack"],
        "minip": ["minip_stack", "minip_tests"],
    }
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    # Capture the tool function registered via @mcp.tool()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)
    assert tool_fn is not None, "ivy_workspace tool was not registered"

    # Call the raw function (bypassing safe_tool since we mock MCP)
    result = await tool_fn(action="set", target="quic")

    assert result["status"] == "ok"
    assert result["active_group"] == "quic"
    assert set(result["active_layers"]) == {"quic_stack", "quic_tests", "tls_stack"}
    assert result["granularity"] == "protocol"
    # Verify ctx was mutated
    assert ctx.active_workspace is not None
    assert ctx.active_workspace.active_group == "quic"


@pytest.mark.asyncio
async def test_set_workspace_unknown_target(tmp_path):
    """Setting an unknown target returns an error listing available groups."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {"quic": ["quic_stack"], "minip": ["minip_stack"]}
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    result = await tool_fn(action="set", target="coap")

    assert result["success"] is False
    assert "coap" in result["message"]
    # Should list available groups
    assert "quic" in result["message"]
    assert "minip" in result["message"]


@pytest.mark.asyncio
async def test_get_workspace(tmp_path):
    """Get action returns current workspace state."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {"quic": ["quic_stack", "quic_tests"]}
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    # First set a workspace
    await tool_fn(action="set", target="quic")

    # Then get it
    result = await tool_fn(action="get")

    assert result["status"] == "ok"
    assert result["active_group"] == "quic"
    assert set(result["active_layers"]) == {"quic_stack", "quic_tests"}
    assert result["granularity"] == "protocol"


@pytest.mark.asyncio
async def test_get_workspace_not_set(tmp_path):
    """Get action when no workspace is set returns appropriate message."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    ctx = _make_ctx(tmp_path)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    result = await tool_fn(action="get")

    assert result["status"] == "ok"
    assert result["active_group"] is None
    assert result["granularity"] == "none"


@pytest.mark.asyncio
async def test_list_workspaces(tmp_path):
    """List action returns available groups and current workspace."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {
        "quic": ["quic_stack", "quic_tests"],
        "minip": ["minip_stack"],
    }
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    # Set quic first
    await tool_fn(action="set", target="quic")

    # List
    result = await tool_fn(action="list")

    assert result["status"] == "ok"
    assert result["active_group"] == "quic"
    assert "quic" in result["available_groups"]
    assert "minip" in result["available_groups"]
    assert set(result["available_groups"]["quic"]) == {"quic_stack", "quic_tests"}


@pytest.mark.asyncio
async def test_clear_workspace(tmp_path):
    """Clear action resets workspace state."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {"quic": ["quic_stack", "quic_tests"]}
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    # Set then clear
    await tool_fn(action="set", target="quic")
    assert ctx.active_workspace is not None
    assert ctx.active_workspace.active_group == "quic"

    result = await tool_fn(action="clear")

    assert result["status"] == "ok"
    assert result["active_group"] is None
    assert result["granularity"] == "none"
    # Resolver should have been called with empty set
    ctx.include_resolver.set_active_workspace.assert_called()
    last_call_arg = ctx.include_resolver.set_active_workspace.call_args[0][0]
    assert last_call_arg == set()


@pytest.mark.asyncio
async def test_set_workspace_persists(tmp_path):
    """State file is written to disk after set."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {"quic": ["quic_stack", "quic_tests"]}
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    await tool_fn(action="set", target="quic")

    state_path = tmp_path / STATE_FILENAME
    assert state_path.exists(), "State file was not written"

    data = json.loads(state_path.read_text())
    assert data["active_group"] == "quic"
    assert set(data["active_layers"]) == {"quic_stack", "quic_tests"}
    assert data["granularity"] == "protocol"


@pytest.mark.asyncio
async def test_set_workspace_with_roles(tmp_path):
    """Setting workspace with roles filters to role_pair granularity."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    groups = {"quic": ["quic_stack", "quic_tests", "tls_stack"]}
    ctx = _make_ctx(tmp_path, groups)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    result = await tool_fn(action="set", target="quic", roles="client,server")

    assert result["status"] == "ok"
    assert result["active_group"] == "quic"
    assert result["granularity"] == "role_pair"


@pytest.mark.asyncio
async def test_invalid_action(tmp_path):
    """An unrecognized action returns an error."""
    from ivy_lsp.tools.workspace import register_workspace_tools

    ctx = _make_ctx(tmp_path)
    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workspace_tools(mcp, ctx)

    result = await tool_fn(action="destroy")

    assert result["success"] is False
    assert "destroy" in result["message"]
