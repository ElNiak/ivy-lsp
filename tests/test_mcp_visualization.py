"""Tests for MCP visualization tool wrappers (Tasks 5 and 15).

Tests the five MCP tools added to mcp_server.py that wrap the
visualization handlers: ivy_action_requirements, ivy_model_summary,
ivy_coverage_gaps (P1, Task 5), ivy_action_dependency_graph, and
ivy_state_machine_view (P2, Task 15).

Uses FastMCP.call_tool() for integration-level testing of tool
registration, parameter passing, and JSON output.
"""

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import (  # noqa: E402
    ActionNode,
    EdgeType,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.analysis.test_scope import ScopedRequirementModel  # noqa: E402
from ivy_lsp.semantic.nodes import RfcRequirement  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_graph() -> ScopedRequirementModel:
    """Build a small requirement graph for MCP tool testing."""
    graph = ScopedRequirementModel()
    graph.add_action(
        ActionNode(
            id="send",
            name="send",
            qualified_name="quic.send",
            file="/test/q.ivy",
            line=10,
        )
    )
    graph.add_action(
        ActionNode(
            id="recv",
            name="recv",
            qualified_name="quic.recv",
            file="/test/q.ivy",
            line=20,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="conn_open",
            name="conn_open",
            qualified_name="quic.conn_open",
            file="/test/q.ivy",
            line=5,
            is_relation=False,
        )
    )
    r1 = RequirementNode(
        id="/test/q.ivy:12",
        kind="require",
        formula_text="x > 0",
        line=12,
        col=0,
        file="/test/q.ivy",
        monitor_action="send",
        mixin_kind="before",
        bracket_tags=["rfc9000:4.1"],
    )
    r2 = RequirementNode(
        id="/test/q.ivy:15",
        kind="ensure",
        formula_text="sent(C, P)",
        line=15,
        col=0,
        file="/test/q.ivy",
        monitor_action="send",
        mixin_kind="after",
    )
    graph.add_file_requirements("/test/q.ivy", [r1, r2])
    graph.add_edge(r1.id, EdgeType.READS, "conn_open")
    return graph


def _build_graph_with_rfc_gaps() -> ScopedRequirementModel:
    """Build a graph with RFC coverage gaps for coverage_gaps testing."""
    graph = _build_test_graph()
    graph.add_rfc_requirement(
        RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="Connection must be open",
            level="MUST",
        )
    )
    graph.add_rfc_requirement(
        RfcRequirement(
            id="rfc9000:8.1",
            rfc="RFC9000",
            section="8.1",
            text="Must validate address",
            level="MUST",
        )
    )
    return graph


def _get_mcp_app(requirement_graph=None):
    """Create a FastMCP app with visualization tools registered, without running."""
    from ivy_lsp.mcp_server import start_mcp

    return start_mcp(
        workspace_root="/tmp/test-workspace",
        requirement_graph=requirement_graph,
        _return_app=True,
    )


# ---------------------------------------------------------------------------
# Test: start_mcp accepts requirement_graph parameter
# ---------------------------------------------------------------------------


class TestStartMcpSignature:
    def test_start_mcp_accepts_requirement_graph(self):
        """start_mcp() should accept a requirement_graph keyword argument."""
        mcp = _get_mcp_app(requirement_graph=None)
        assert mcp is not None

    def test_start_mcp_accepts_graph_instance(self):
        """start_mcp() should accept a real RequirementGraph instance."""
        graph = _build_test_graph()
        mcp = _get_mcp_app(requirement_graph=graph)
        assert mcp is not None


# ---------------------------------------------------------------------------
# Test: visualization tools are registered
# ---------------------------------------------------------------------------


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_visualization_tools_registered(self):
        """The three visualization tools should appear in list_tools()."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "ivy_action_requirements" in tool_names
        assert "ivy_model_summary" in tool_names
        assert "ivy_coverage_gaps" in tool_names

    @pytest.mark.asyncio
    async def test_existing_tools_still_registered(self):
        """Pre-existing tools like ivy_verify should still be present."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "ivy_verify" in tool_names
        assert "ivy_lint" in tool_names
        assert "ivy_capabilities" in tool_names


# ---------------------------------------------------------------------------
# Test: ivy_action_requirements tool
# ---------------------------------------------------------------------------


class TestIvyActionRequirementsTool:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """Tool should return a parseable JSON string."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_action_requirements", {})
        # call_tool returns content blocks; extract text
        text = _extract_text(result)
        parsed = json.loads(text)
        assert isinstance(parsed["actions"], list)
        assert parsed["modelReady"] is True

    @pytest.mark.asyncio
    async def test_returns_all_actions_when_no_filter(self):
        """Without filters, all actions should be returned."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_action_requirements", {})
        parsed = json.loads(_extract_text(result))
        names = {a["actionName"] for a in parsed["actions"]}
        assert "send" in names
        assert "recv" in names

    @pytest.mark.asyncio
    async def test_action_name_filter(self):
        """Filtering by action_name should return only matching actions."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool(
            "ivy_action_requirements", {"action_name": "send"}
        )
        parsed = json.loads(_extract_text(result))
        assert len(parsed["actions"]) == 1
        assert parsed["actions"][0]["actionName"] == "send"

    @pytest.mark.asyncio
    async def test_missing_graph_returns_empty(self):
        """When no requirement_graph is provided, modelReady should be False."""
        mcp = _get_mcp_app(requirement_graph=None)
        result = await mcp.call_tool("ivy_action_requirements", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["modelReady"] is False
        assert parsed["actions"] == []


# ---------------------------------------------------------------------------
# Test: ivy_model_summary tool
# ---------------------------------------------------------------------------


class TestIvyModelSummaryTool:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """Tool should return a parseable JSON string with rows and totals."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed["rows"], list)
        assert "totals" in parsed

    @pytest.mark.asyncio
    async def test_row_per_action(self):
        """Each action should produce one row."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert len(parsed["rows"]) == 2

    @pytest.mark.asyncio
    async def test_totals_correct(self):
        """Totals should reflect the graph contents."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["totals"]["actions"] == 2
        assert parsed["totals"]["requirements"] == 2

    @pytest.mark.asyncio
    async def test_missing_graph_returns_empty(self):
        """When no requirement_graph is provided, rows should be empty."""
        mcp = _get_mcp_app(requirement_graph=None)
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["rows"] == []
        assert parsed["totals"]["actions"] == 0


# ---------------------------------------------------------------------------
# Test: ivy_coverage_gaps tool
# ---------------------------------------------------------------------------


class TestIvyCoverageGapsTool:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """Tool should return parseable JSON with gap categories."""
        graph = _build_graph_with_rfc_gaps()
        mcp = _get_mcp_app(requirement_graph=graph)
        result = await mcp.call_tool("ivy_coverage_gaps", {})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed["unguardedStateVars"], list)
        assert isinstance(parsed["uncoveredRfcRequirements"], list)
        assert isinstance(parsed["orphanRequirements"], list)
        assert "summary" in parsed

    @pytest.mark.asyncio
    async def test_detects_uncovered_rfc(self):
        """Uncovered RFC requirements should appear in the output."""
        graph = _build_graph_with_rfc_gaps()
        mcp = _get_mcp_app(requirement_graph=graph)
        result = await mcp.call_tool("ivy_coverage_gaps", {})
        parsed = json.loads(_extract_text(result))
        uncovered_ids = [r["id"] for r in parsed["uncoveredRfcRequirements"]]
        # rfc9000:4.1 is covered by r1's bracket_tags
        assert "rfc9000:4.1" not in uncovered_ids
        # rfc9000:8.1 is not covered
        assert "rfc9000:8.1" in uncovered_ids

    @pytest.mark.asyncio
    async def test_summary_counts(self):
        """Summary should have correct aggregate counts."""
        graph = _build_graph_with_rfc_gaps()
        mcp = _get_mcp_app(requirement_graph=graph)
        result = await mcp.call_tool("ivy_coverage_gaps", {})
        parsed = json.loads(_extract_text(result))
        s = parsed["summary"]
        assert s["totalActions"] == 2
        assert s["totalRequirements"] == 2
        assert s["totalRfcReqs"] == 2

    @pytest.mark.asyncio
    async def test_missing_graph_returns_empty(self):
        """When no requirement_graph is provided, all gaps should be empty."""
        mcp = _get_mcp_app(requirement_graph=None)
        result = await mcp.call_tool("ivy_coverage_gaps", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["unguardedStateVars"] == []
        assert parsed["summary"]["totalActions"] == 0


# ---------------------------------------------------------------------------
# Helpers: richer graph for P2 tools (dependency graph / state machine)
# ---------------------------------------------------------------------------


def _build_dependency_graph() -> ScopedRequirementModel:
    """Build a graph with READS and WRITES edges for dependency graph tests.

    Graph shape:
    - action "send" has r1 (require, before) that READS "conn_open"
    - action "send" has r2 (ensure, after) that WRITES "pkt_sent"
    - action "recv" has r3 (require, before) that READS "pkt_sent"
    This creates a dependency edge: send -> recv via "pkt_sent".
    """
    graph = ScopedRequirementModel()
    graph.add_action(
        ActionNode(
            id="send", name="send", qualified_name="quic.send",
            file="/test/q.ivy", line=10,
        )
    )
    graph.add_action(
        ActionNode(
            id="recv", name="recv", qualified_name="quic.recv",
            file="/test/q.ivy", line=20,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="conn_open", name="conn_open",
            qualified_name="quic.conn_open",
            file="/test/q.ivy", line=5, is_relation=False,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="pkt_sent", name="pkt_sent",
            qualified_name="quic.pkt_sent",
            file="/test/q.ivy", line=6, is_relation=False,
        )
    )
    r1 = RequirementNode(
        id="/test/q.ivy:12", kind="require", formula_text="conn_open",
        line=12, col=0, file="/test/q.ivy",
        monitor_action="send", mixin_kind="before",
    )
    r2 = RequirementNode(
        id="/test/q.ivy:15", kind="ensure", formula_text="pkt_sent",
        line=15, col=0, file="/test/q.ivy",
        monitor_action="send", mixin_kind="after",
    )
    r3 = RequirementNode(
        id="/test/q.ivy:22", kind="require", formula_text="pkt_sent",
        line=22, col=0, file="/test/q.ivy",
        monitor_action="recv", mixin_kind="before",
    )
    graph.add_file_requirements("/test/q.ivy", [r1, r2, r3])
    # send reads conn_open, writes pkt_sent
    graph.add_edge(r1.id, EdgeType.READS, "conn_open")
    graph.add_edge(r2.id, EdgeType.WRITES, "pkt_sent")
    # recv reads pkt_sent
    graph.add_edge(r3.id, EdgeType.READS, "pkt_sent")
    return graph


# ---------------------------------------------------------------------------
# Test: P2 tools are registered alongside P1 tools
# ---------------------------------------------------------------------------


class TestP2ToolRegistration:
    @pytest.mark.asyncio
    async def test_p2_visualization_tools_registered(self):
        """The two P2 visualization tools should appear in list_tools()."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "ivy_action_dependency_graph" in tool_names
        assert "ivy_state_machine_view" in tool_names

    @pytest.mark.asyncio
    async def test_p1_tools_still_present_after_p2(self):
        """P1 visualization tools must still be registered after P2 additions."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "ivy_action_requirements" in tool_names
        assert "ivy_model_summary" in tool_names
        assert "ivy_coverage_gaps" in tool_names


# ---------------------------------------------------------------------------
# Test: ivy_action_dependency_graph tool
# ---------------------------------------------------------------------------


class TestIvyActionDependencyGraphTool:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """Tool should return parseable JSON with nodes and edges."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_action_dependency_graph", {})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed["nodes"], list)
        assert isinstance(parsed["edges"], list)

    @pytest.mark.asyncio
    async def test_action_nodes_present(self):
        """Both actions should appear as nodes in the dependency graph."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_action_dependency_graph", {})
        parsed = json.loads(_extract_text(result))
        node_ids = {n["id"] for n in parsed["nodes"] if n["type"] == "action"}
        assert "send" in node_ids
        assert "recv" in node_ids

    @pytest.mark.asyncio
    async def test_shared_state_edge(self):
        """An edge from send to recv via pkt_sent should exist."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_action_dependency_graph", {})
        parsed = json.loads(_extract_text(result))
        shared_edges = [
            e for e in parsed["edges"]
            if e["type"] == "shared_state"
        ]
        assert len(shared_edges) >= 1
        edge = shared_edges[0]
        assert edge["source"] == "send"
        assert edge["target"] == "recv"

    @pytest.mark.asyncio
    async def test_include_state_vars(self):
        """With include_state_vars=True, state var nodes should appear."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool(
            "ivy_action_dependency_graph", {"include_state_vars": True}
        )
        parsed = json.loads(_extract_text(result))
        state_var_nodes = [
            n for n in parsed["nodes"] if n["type"] == "stateVar"
        ]
        assert len(state_var_nodes) >= 1
        state_var_names = {n["label"] for n in state_var_nodes}
        assert "pkt_sent" in state_var_names

    @pytest.mark.asyncio
    async def test_missing_graph_returns_empty(self):
        """When no requirement_graph is provided, nodes/edges should be empty."""
        mcp = _get_mcp_app(requirement_graph=None)
        result = await mcp.call_tool("ivy_action_dependency_graph", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["nodes"] == []
        assert parsed["edges"] == []


# ---------------------------------------------------------------------------
# Test: ivy_state_machine_view tool
# ---------------------------------------------------------------------------


class TestIvyStateMachineViewTool:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """Tool should return parseable JSON with nodes and transitions."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_state_machine_view", {})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed["nodes"], list)
        assert isinstance(parsed["transitions"], list)

    @pytest.mark.asyncio
    async def test_state_nodes_present(self):
        """Active state variables should appear as state nodes."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_state_machine_view", {})
        parsed = json.loads(_extract_text(result))
        state_nodes = [n for n in parsed["nodes"] if n["type"] == "state"]
        state_labels = {n["label"] for n in state_nodes}
        # conn_open is read, pkt_sent is read and written
        assert "pkt_sent" in state_labels
        assert "conn_open" in state_labels

    @pytest.mark.asyncio
    async def test_transitions_include_action(self):
        """Transitions should reference the action name."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_state_machine_view", {})
        parsed = json.loads(_extract_text(result))
        action_names = {t["action"] for t in parsed["transitions"]}
        assert "send" in action_names

    @pytest.mark.asyncio
    async def test_state_var_filter(self):
        """Filtering by state_var_filter should limit state nodes."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool(
            "ivy_state_machine_view", {"state_var_filter": "pkt_sent"}
        )
        parsed = json.loads(_extract_text(result))
        state_nodes = [n for n in parsed["nodes"] if n["type"] == "state"]
        assert len(state_nodes) == 1
        assert state_nodes[0]["label"] == "pkt_sent"

    @pytest.mark.asyncio
    async def test_missing_graph_returns_empty(self):
        """When no requirement_graph is provided, nodes/transitions should be empty."""
        mcp = _get_mcp_app(requirement_graph=None)
        result = await mcp.call_tool("ivy_state_machine_view", {})
        parsed = json.loads(_extract_text(result))
        assert parsed["nodes"] == []
        assert parsed["transitions"] == []


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _extract_text(result) -> str:
    """Extract text content from MCP call_tool result.

    call_tool can return different shapes depending on the MCP SDK version:
    - tuple(list[TextContent], dict) -- SDK >= 1.9
    - list[TextContent] -- older SDKs
    - dict -- raw dict
    """
    if isinstance(result, dict):
        if "result" in result:
            return result["result"]
        return json.dumps(result)
    # Handle tuple: (content_blocks, metadata_dict)
    if isinstance(result, tuple):
        content_blocks = result[0]
        # If the second element is a dict with "result", use that directly
        if len(result) > 1 and isinstance(result[1], dict) and "result" in result[1]:
            return result[1]["result"]
        # Otherwise, extract from the content blocks list
        result = content_blocks
    # Sequence of content blocks
    texts = []
    for block in result:
        if hasattr(block, "text"):
            texts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            texts.append(block["text"])
    return "\n".join(texts)
