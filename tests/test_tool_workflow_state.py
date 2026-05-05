"""Tests for the ivy_workflow_state MCP tool.

Locks the trimmed response shape so future edits don't reintroduce echoed
input fields ("action", echoed workflow/phase/state). The source of truth
for state is the YAML files on disk; responses only carry novel info.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


@dataclass
class MockToolContext:
    root: str
    active_workspace: Any = None
    workspace_groups: dict = field(default_factory=dict)
    include_resolver: Any = field(default_factory=MagicMock)

    def build_context_metadata(self) -> dict:
        """Minimal mock matching ToolContext.build_context_metadata()."""
        ctx: dict = {}
        ws = self.active_workspace
        if ws is None or not getattr(ws, "active_group", None):
            return ctx
        ctx["workspace"] = ws.active_group
        ctx["layers"] = sorted(ws.active_layers)
        ctx["set_by"] = getattr(ws, "set_by", "unknown")
        return ctx


def _make_ctx_with_protocol(tmp_path: Path, protocol: str) -> MockToolContext:
    """Create a context with a protocol-testing/<protocol>/ directory."""
    proto_dir = tmp_path / "protocol-testing" / protocol
    proto_dir.mkdir(parents=True)
    return MockToolContext(root=str(tmp_path))


def _register_and_capture(ctx: MockToolContext):
    from ivy_lsp.mcp.tools.workflow_state import register_workflow_state_tools

    mcp = MagicMock()
    tool_fn = None

    def capture_tool():
        def decorator(fn):
            nonlocal tool_fn
            tool_fn = fn
            return fn

        return decorator

    mcp.tool = capture_tool
    register_workflow_state_tools(mcp, ctx)
    assert tool_fn is not None
    return tool_fn


@pytest.fixture
def workflow_tool(tmp_path):
    ctx = _make_ctx_with_protocol(tmp_path, "bgp")
    tool_fn = _register_and_capture(ctx)
    return tool_fn, ctx, tmp_path


# ---------------------------------------------------------------------------
# set: response carries only novel info (no echoed workflow/phase/action)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_response_is_trimmed(workflow_tool):
    """action='set' returns success + novel fields only. No echoed inputs."""
    tool_fn, ctx, tmp_path = workflow_tool

    result = await tool_fn(
        action="set", workflow="build", phase="scoped", protocol="bgp"
    )

    assert result["success"] is True
    assert "started" in result  # novel: server-generated timestamp
    assert result["phase_transition"] is None  # novel: no prior state
    assert "protocol_dir" in result
    # Echoed inputs must NOT appear in the response
    assert "action" not in result
    assert "workflow" not in result
    assert "phase" not in result
    assert "invocation_depth" not in result
    assert "caller" not in result


@pytest.mark.asyncio
async def test_set_persists_full_data_to_file(workflow_tool):
    """The YAML file persists the canonical 3-field active-workflow schema."""
    tool_fn, ctx, tmp_path = workflow_tool

    await tool_fn(
        action="set",
        workflow="build",
        phase="scoped",
        protocol="bgp",
    )

    state_file = (
        tmp_path / "protocol-testing" / "bgp" / ".panther-ivy" / "active-workflow"
    )
    assert state_file.exists()
    data = yaml.safe_load(state_file.read_text())
    assert data["workflow"] == "build"
    assert data["phase"] == "scoped"
    assert "started" in data
    # Regression guard for the journaling-contract.md §4 invariant: the YAML
    # carries the 3-field current-state triple only; composition history
    # lives in the workflow-journal pending_dispatch / workflow_resumed pair.
    assert set(data.keys()) == {"workflow", "phase", "started"}


@pytest.mark.asyncio
async def test_set_surfaces_phase_transition_when_phase_changes(workflow_tool):
    """phase_transition is novel info (server derives it); it appears only on change."""
    tool_fn, ctx, tmp_path = workflow_tool

    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")
    result = await tool_fn(
        action="set", workflow="build", phase="compiled", protocol="bgp"
    )

    assert result["phase_transition"] == {"from": "scoped", "to": "compiled"}


# ---------------------------------------------------------------------------
# get / get_build / get_journal: read tools, no "action" echo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_response_drops_action(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")

    result = await tool_fn(action="get", protocol="bgp")

    assert result["success"] is True
    assert result["active"] is True
    assert result["workflow"] == "build"  # this is a READ, not an echo
    assert result["phase"] == "scoped"
    assert "action" not in result


@pytest.mark.asyncio
async def test_get_build_response_drops_action(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(
        action="set_build",
        protocol="bgp",
        state=json.dumps({"stage": "done", "artifacts": ["a.out"]}),
    )

    result = await tool_fn(action="get_build", protocol="bgp")

    assert result["success"] is True
    assert result["has_build"] is True
    assert result["state"] == {"stage": "done", "artifacts": ["a.out"]}
    assert "action" not in result


# ---------------------------------------------------------------------------
# set_build: drops echoed "state" dict from response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_build_response_drops_echoed_state(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool

    result = await tool_fn(
        action="set_build",
        protocol="bgp",
        state=json.dumps({"stage": "compiled"}),
    )

    assert result == {
        "success": True,
        "protocol_dir": str(tmp_path / "protocol-testing" / "bgp"),
    }


@pytest.mark.asyncio
async def test_set_build_persists_state_to_file(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(
        action="set_build", protocol="bgp", state=json.dumps({"stage": "compiled"})
    )

    build_file = (
        tmp_path / "protocol-testing" / "bgp" / ".panther-ivy" / "build-state.yaml"
    )
    assert yaml.safe_load(build_file.read_text()) == {"stage": "compiled"}


# ---------------------------------------------------------------------------
# clear: response is minimal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_response_drops_action(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")

    result = await tool_fn(action="clear", protocol="bgp")

    assert result == {
        "success": True,
        "protocol_dir": str(tmp_path / "protocol-testing" / "bgp"),
    }


# ---------------------------------------------------------------------------
# append_journal: novel-only (ts, journal_size, rotated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_journal_response_shape(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")

    result = await tool_fn(
        action="append_journal",
        event_type="progress",
        state=json.dumps({"note": "compiled ok"}),
        protocol="bgp",
    )

    assert result["success"] is True
    assert "ts" in result  # novel
    assert result["journal_size"] == 1
    assert result["rotated"] is False
    # Old "event" echo must not reappear
    assert "event" not in result
    assert "action" not in result


@pytest.mark.asyncio
async def test_append_journal_persists_entry(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")
    await tool_fn(
        action="append_journal",
        event_type="progress",
        state=json.dumps({"note": "x"}),
        protocol="bgp",
    )

    journal_file = (
        tmp_path / "protocol-testing" / "bgp" / ".panther-ivy" / "workflow-journal.yaml"
    )
    entries = yaml.safe_load(journal_file.read_text())
    # One phase_transition was added by the second set() above? No, this test
    # only called set() once (no prior phase), so only the append is journaled.
    assert len(entries) == 1
    assert entries[0]["type"] == "progress"
    assert entries[0]["payload"] == {"note": "x"}


@pytest.mark.asyncio
async def test_append_journal_rotation_reports_post_rotation_size(workflow_tool):
    """On rotation, journal_size reflects the kept half, not the pre-rotation total."""
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")

    # Seed 200 entries directly to avoid 200 MCP calls.
    state_path = (
        tmp_path / "protocol-testing" / "bgp" / ".panther-ivy" / "workflow-journal.yaml"
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = [
        {
            "ts": "seed",
            "type": "progress",
            "workflow": "build",
            "phase": "scoped",
            "payload": {"i": i},
        }
        for i in range(200)
    ]
    state_path.write_text(yaml.safe_dump(seeded))

    # The 201st entry triggers rotation.
    result = await tool_fn(
        action="append_journal",
        event_type="progress",
        state=json.dumps({"n": 201}),
        protocol="bgp",
    )

    assert result["rotated"] is True
    # After rotation: 201 -> archived 100, kept 101.
    assert result["journal_size"] == 101


@pytest.mark.asyncio
async def test_get_journal_response_drops_action(workflow_tool):
    tool_fn, ctx, tmp_path = workflow_tool
    await tool_fn(action="set", workflow="build", phase="scoped", protocol="bgp")
    await tool_fn(
        action="append_journal",
        event_type="progress",
        state=json.dumps({"n": 1}),
        protocol="bgp",
    )

    result = await tool_fn(action="get_journal", protocol="bgp", last_n=10)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["total"] == 1
    assert len(result["entries"]) == 1
    assert "action" not in result
