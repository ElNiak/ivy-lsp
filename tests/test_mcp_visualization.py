"""Tests for MCP visualization tool wrappers (Task 5).

Tests the three MCP tools added to mcp_server.py that wrap the
visualization handlers: ivy_action_requirements, ivy_model_summary,
and ivy_coverage_gaps.

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
