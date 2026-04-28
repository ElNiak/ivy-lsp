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

    def build_context_metadata(self) -> dict:
        """Minimal mock of ToolContext.build_context_metadata()."""
        ctx: dict = {}
        ws = self.active_workspace
        if ws is None or not getattr(ws, "active_group", None):
            return ctx
        ctx["workspace"] = ws.active_group
        ctx["layers"] = sorted(ws.active_layers)
        ctx["set_by"] = getattr(ws, "set_by", "unknown")
        return ctx


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


def _register_and_capture(ctx: MockToolContext):
    """Register workspace tools on a mock MCP and return the captured tool function."""
    from ivy_lsp.mcp.tools.workspace import register_workspace_tools

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
    assert tool_fn is not None, "ivy_workspace tool was not registered"
    return tool_fn


@pytest.fixture
def make_workspace_tool(tmp_path):
    """Factory fixture: create a MockToolContext and register the workspace tool."""

    def _factory(groups: dict | None = None):
        ctx = _make_ctx(tmp_path, groups)
        tool_fn = _register_and_capture(ctx)
        return tool_fn, ctx

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_workspace_protocol(make_workspace_tool):
    """Setting a workspace by protocol name activates the group's layers."""
    groups = {
        "quic": ["quic_stack", "quic_tests", "tls_stack"],
        "minip": ["minip_stack", "minip_tests"],
    }
    tool_fn, ctx = make_workspace_tool(groups)

    result = await tool_fn(action="set", target="quic")

    assert result["success"] is True
    assert set(result["active_layers"]) == {"quic_stack", "quic_tests", "tls_stack"}
    assert "files_in_scope" in result
    # Verify ctx was mutated (canonical source of truth for active_group/granularity)
    assert ctx.active_workspace is not None
    assert ctx.active_workspace.active_group == "quic"
    assert ctx.active_workspace.granularity == "protocol"


@pytest.mark.asyncio
async def test_set_workspace_unknown_target(make_workspace_tool):
    """Setting an unknown target returns an error listing available groups."""
    groups = {"quic": ["quic_stack"], "minip": ["minip_stack"]}
    tool_fn, ctx = make_workspace_tool(groups)

    result = await tool_fn(action="set", target="coap")

    assert result["success"] is False
    assert "coap" in result["message"]
    # Should list available groups
    assert "quic" in result["message"]
    assert "minip" in result["message"]


@pytest.mark.asyncio
async def test_get_workspace(make_workspace_tool):
    """Get action returns current workspace state."""
    groups = {"quic": ["quic_stack", "quic_tests"]}
    tool_fn, ctx = make_workspace_tool(groups)

    # First set a workspace
    await tool_fn(action="set", target="quic")

    # Then get it
    result = await tool_fn(action="get")

    assert result["success"] is True
    assert result["active_group"] == "quic"
    assert set(result["active_layers"]) == {"quic_stack", "quic_tests"}
    assert result["granularity"] == "protocol"


@pytest.mark.asyncio
async def test_get_workspace_not_set(make_workspace_tool):
    """Get action when no workspace is set returns appropriate message."""
    tool_fn, ctx = make_workspace_tool()

    result = await tool_fn(action="get")

    assert result["success"] is True
    assert result["active_group"] is None
    assert result["granularity"] == "none"


@pytest.mark.asyncio
async def test_list_workspaces(make_workspace_tool):
    """List action returns available groups and current workspace."""
    groups = {
        "quic": ["quic_stack", "quic_tests"],
        "minip": ["minip_stack"],
    }
    tool_fn, ctx = make_workspace_tool(groups)

    # Set quic first
    await tool_fn(action="set", target="quic")

    # List
    result = await tool_fn(action="list")

    assert result["success"] is True
    assert result["active_group"] == "quic"
    assert "quic" in result["available_groups"]
    assert "minip" in result["available_groups"]
    assert set(result["available_groups"]["quic"]) == {"quic_stack", "quic_tests"}


@pytest.mark.asyncio
async def test_clear_workspace(make_workspace_tool):
    """Clear action resets workspace state."""
    groups = {"quic": ["quic_stack", "quic_tests"]}
    tool_fn, ctx = make_workspace_tool(groups)

    # Set then clear
    await tool_fn(action="set", target="quic")
    assert ctx.active_workspace is not None
    assert ctx.active_workspace.active_group == "quic"

    result = await tool_fn(action="clear")

    assert result == {"success": True}
    # Canonical state lives on ctx, not the trimmed response
    assert ctx.active_workspace.active_group is None
    assert ctx.active_workspace.granularity == "none"
    # Resolver should have been called with empty set
    ctx.include_resolver.set_active_workspace.assert_called()
    last_call_arg = ctx.include_resolver.set_active_workspace.call_args[0][0]
    assert last_call_arg == set()


@pytest.mark.asyncio
async def test_set_workspace_persists(make_workspace_tool, tmp_path):
    """State file is written to disk after set."""
    groups = {"quic": ["quic_stack", "quic_tests"]}
    tool_fn, ctx = make_workspace_tool(groups)

    await tool_fn(action="set", target="quic")

    state_path = tmp_path / STATE_FILENAME
    assert state_path.exists(), "State file was not written"

    data = json.loads(state_path.read_text())
    assert data["active_group"] == "quic"
    assert set(data["active_layers"]) == {"quic_stack", "quic_tests"}
    assert data["granularity"] == "protocol"


@pytest.mark.asyncio
async def test_set_workspace_with_roles(make_workspace_tool):
    """Setting workspace with roles filters to role_pair granularity."""
    groups = {"quic": ["quic_stack", "quic_tests", "tls_stack"]}
    tool_fn, ctx = make_workspace_tool(groups)

    result = await tool_fn(action="set", target="quic", roles="client,server")

    assert result["success"] is True
    # Canonical state lives on ctx; response does not echo input target/roles
    assert ctx.active_workspace.active_group == "quic"
    assert ctx.active_workspace.granularity == "role_pair"


@pytest.mark.asyncio
async def test_invalid_action(make_workspace_tool):
    """An unrecognized action returns an error."""
    tool_fn, ctx = make_workspace_tool()

    result = await tool_fn(action="destroy")

    assert result["success"] is False
    assert "destroy" in result["message"]


# ---------------------------------------------------------------------------
# Tests: _handle_get fallback to persisted state
# ---------------------------------------------------------------------------


class TestHandleGetFallbackToPersisted:
    """_handle_get should fall back to persisted state when in-memory is None."""

    def test_get_returns_persisted_explicit_state(self, tmp_path):
        """Restore persisted explicit state when in-memory is None."""
        from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
        from ivy_lsp.mcp.tools.workspace import _handle_get

        state_file = tmp_path / STATE_FILENAME
        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic", "quic_tests"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )
        ws.save(str(state_file))

        ctx = _make_ctx(tmp_path)
        ctx.active_workspace = None
        result = _handle_get(ctx)

        assert result["success"] is True
        assert result["active_group"] == "quic"
        assert result["set_by"] == "explicit"
        assert ctx.active_workspace is not None
        assert ctx.active_workspace.active_group == "quic"

    def test_get_returns_cleared_when_no_state_file(self, tmp_path):
        """When both in-memory and persisted state are missing, return cleared."""
        from ivy_lsp.mcp.tools.workspace import _handle_get

        ctx = _make_ctx(tmp_path)
        ctx.active_workspace = None
        result = _handle_get(ctx)

        assert result["active_group"] is None
        assert result["set_by"] == "cleared"

    def test_get_returns_cleared_when_persisted_is_cleared(self, tmp_path):
        """When persisted state is also cleared, return cleared."""
        from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
        from ivy_lsp.mcp.tools.workspace import _handle_get

        state_file = tmp_path / STATE_FILENAME
        ws = ActiveWorkspace.cleared()
        ws.save(str(state_file))

        ctx = _make_ctx(tmp_path)
        ctx.active_workspace = None
        result = _handle_get(ctx)

        assert result["active_group"] is None
        assert result["set_by"] == "cleared"
