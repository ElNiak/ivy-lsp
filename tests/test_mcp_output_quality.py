"""Tests for MCP tool output quality and schema stability.

Verifies that tool outputs are well-structured for AI consumption
and that JSON schemas don't drift between versions.
"""

import json
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.requirement_graph import (  # noqa: E402
    ActionNode,
    EdgeType,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel  # noqa: E402
from ivy_lsp.core.semantic.nodes import RfcRequirement  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_graph():
    graph = ScopedRequirementModel()
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
    graph.add_edge(r1.id, EdgeType.READS, "conn_open")
    return graph


def _build_graph_with_rfc_gaps():
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


def _build_dependency_graph():
    graph = ScopedRequirementModel()
    r1 = RequirementNode(
        id="/test/q.ivy:12",
        kind="require",
        formula_text="conn_open",
        line=12,
        col=0,
        file="/test/q.ivy",
        monitor_action="send",
        mixin_kind="before",
    )
    r2 = RequirementNode(
        id="/test/q.ivy:15",
        kind="ensure",
        formula_text="pkt_sent",
        line=15,
        col=0,
        file="/test/q.ivy",
        monitor_action="send",
        mixin_kind="after",
    )
    r3 = RequirementNode(
        id="/test/q.ivy:22",
        kind="require",
        formula_text="pkt_sent",
        line=22,
        col=0,
        file="/test/q.ivy",
        monitor_action="recv",
        mixin_kind="before",
    )
    graph.add_file_requirements("/test/q.ivy", [r1, r2, r3])
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
    graph.add_state_var(
        StateVarNode(
            id="pkt_sent",
            name="pkt_sent",
            qualified_name="quic.pkt_sent",
            file="/test/q.ivy",
            line=6,
            is_relation=False,
        )
    )
    graph.add_edge(r1.id, EdgeType.READS, "conn_open")
    graph.add_edge(r2.id, EdgeType.WRITES, "pkt_sent")
    graph.add_edge(r3.id, EdgeType.READS, "pkt_sent")
    return graph


def _build_large_graph(n_actions=10):
    """Build a graph with many actions for pagination testing."""
    graph = ScopedRequirementModel()
    for i in range(n_actions):
        graph.add_action(
            ActionNode(
                id=f"act_{i}",
                name=f"act_{i}",
                qualified_name=f"q.act_{i}",
                file="/test/q.ivy",
                line=i * 10,
            )
        )
    return graph


def _get_mcp_app(requirement_graph=None, workspace_root=None):
    from ivy_lsp.mcp.server import start_mcp

    root = workspace_root or "/tmp/test-workspace"
    return start_mcp(
        workspace_root=root,
        requirement_graph=requirement_graph,
        _return_app=True,
    )


def _extract_text(result) -> str:
    if isinstance(result, dict):
        if "result" in result:
            return result["result"]
        return json.dumps(result)
    if isinstance(result, tuple):
        content_blocks = result[0]
        if len(result) > 1 and isinstance(result[1], dict) and "result" in result[1]:
            return result[1]["result"]
        result = content_blocks
    texts = []
    for block in result:
        if hasattr(block, "text"):
            texts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            texts.append(block["text"])
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Schema stability: ivy_model_summary (detail="requirements")
# ---------------------------------------------------------------------------


class TestActionRequirementsSchema:
    @pytest.mark.asyncio
    async def test_json_shape(self):
        """Top-level keys include: actions, scopeInfo, modelReady."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {"detail": "requirements"})
        parsed = json.loads(_extract_text(result))
        assert "actions" in parsed
        assert "modelReady" in parsed
        assert isinstance(parsed["actions"], list)

    @pytest.mark.asyncio
    async def test_action_entry_shape(self):
        """Each action entry has actionName, qualifiedName, file, line."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {"detail": "requirements"})
        parsed = json.loads(_extract_text(result))
        for action in parsed["actions"]:
            assert "actionName" in action
            assert "qualifiedName" in action
            assert "file" in action
            assert "line" in action

    @pytest.mark.asyncio
    async def test_pagination(self):
        """offset=3, limit=5 on 10 actions -> 5 results."""
        mcp = _get_mcp_app(requirement_graph=_build_large_graph(10))
        result = await mcp.call_tool(
            "ivy_model_summary", {"detail": "requirements", "offset": 3, "limit": 5}
        )
        parsed = json.loads(_extract_text(result))
        assert len(parsed["actions"]) == 5


# ---------------------------------------------------------------------------
# Schema stability: ivy_model_summary
# ---------------------------------------------------------------------------


class TestModelSummarySchema:
    @pytest.mark.asyncio
    async def test_row_shape(self):
        """Each row has actionName, qualifiedName."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert "rows" in parsed
        assert "totals" in parsed
        for row in parsed["rows"]:
            assert "actionName" in row
            assert "qualifiedName" in row

    @pytest.mark.asyncio
    async def test_totals_present(self):
        """Totals dict has expected keys."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {})
        parsed = json.loads(_extract_text(result))
        assert "actions" in parsed["totals"]
        assert "requirements" in parsed["totals"]


# ---------------------------------------------------------------------------
# Schema stability: ivy_coverage (mode="gaps")
# ---------------------------------------------------------------------------


class TestCoverageGapsSchema:
    @pytest.mark.asyncio
    async def test_shape(self):
        """Top-level: unguardedStateVars, uncoveredRfcRequirements, orphanRequirements, summary."""
        mcp = _get_mcp_app(requirement_graph=_build_graph_with_rfc_gaps())
        result = await mcp.call_tool("ivy_coverage", {"mode": "gaps"})
        parsed = json.loads(_extract_text(result))
        assert "unguardedStateVars" in parsed
        assert "uncoveredRfcRequirements" in parsed
        assert "orphanRequirements" in parsed
        assert "summary" in parsed

    @pytest.mark.asyncio
    async def test_summary_keys(self):
        """Summary has totalActions, totalRequirements, totalRfcReqs."""
        mcp = _get_mcp_app(requirement_graph=_build_graph_with_rfc_gaps())
        result = await mcp.call_tool("ivy_coverage", {"mode": "gaps"})
        parsed = json.loads(_extract_text(result))
        s = parsed["summary"]
        assert "totalActions" in s
        assert "totalRequirements" in s
        assert "totalRfcReqs" in s


# ---------------------------------------------------------------------------
# Schema stability: ivy_visualize (view="dependencies")
# ---------------------------------------------------------------------------


class TestDependencyGraphSchema:
    @pytest.mark.asyncio
    async def test_node_shape(self):
        """Each node has id, type, label."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_visualize", {"view": "dependencies"})
        parsed = json.loads(_extract_text(result))
        assert "nodes" in parsed
        assert "edges" in parsed
        for node in parsed["nodes"]:
            assert "id" in node
            assert "type" in node
            assert "label" in node


# ---------------------------------------------------------------------------
# Schema stability: ivy_visualize (view="state_machine")
# ---------------------------------------------------------------------------


class TestStateMachineSchema:
    @pytest.mark.asyncio
    async def test_transition_shape(self):
        """Transitions have action field."""
        mcp = _get_mcp_app(requirement_graph=_build_dependency_graph())
        result = await mcp.call_tool("ivy_visualize", {"view": "state_machine"})
        parsed = json.loads(_extract_text(result))
        assert "nodes" in parsed
        assert "transitions" in parsed
        for t in parsed["transitions"]:
            assert "action" in t


# ---------------------------------------------------------------------------
# All-tools-return-JSON test
# ---------------------------------------------------------------------------


class TestAllToolsReturnJSON:
    @pytest.mark.asyncio
    async def test_capabilities_returns_json(self):
        """ivy_capabilities returns valid JSON."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool("ivy_capabilities", {})
        text = _extract_text(result)
        parsed = json.loads(text)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_extract_requirements_returns_json(self):
        """ivy_extract_requirements returns valid JSON."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": "The sender MUST send data."},
        )
        text = _extract_text(result)
        parsed = json.loads(text)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Error response consistency
# ---------------------------------------------------------------------------


class TestErrorResponseConsistency:
    @pytest.mark.asyncio
    async def test_diagnostics_structural_error_has_success_field(self, tmp_path):
        """Error response has success: false and message key."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "nonexistent.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is False
        assert "message" in parsed

    @pytest.mark.asyncio
    async def test_verify_error_has_success_field(self, tmp_path):
        """ivy_verify error response has success: false."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_verify", {"relative_path": "nonexistent.ivy"})
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is False
        assert "message" in parsed

    @pytest.mark.asyncio
    async def test_diagnostics_error_has_success_field(self, tmp_path):
        """ivy_diagnostics error response has success: false."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "nonexistent.ivy"}
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is False
        assert "message" in parsed


# ---------------------------------------------------------------------------
# Layered overview and smart suggestions
# ---------------------------------------------------------------------------


class TestLayeredOverview:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """ivy_visualize (view=layers) returns parseable JSON."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_visualize", {"view": "layers"})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed, dict)


class TestSmartSuggestions:
    @pytest.mark.asyncio
    async def test_empty_workspace_no_crash(self):
        """ivy_quality (mode=suggestions) with no params -> valid JSON, no crash."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_quality", {"mode": "suggestions"})
        parsed = json.loads(_extract_text(result))
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# P1 improvement tests
# ---------------------------------------------------------------------------


class TestP1RequirementCoverageUncoveredIds:
    """Test the uncovered_ids field added to ivy_coverage (mode=stats)."""

    def test_uncovered_ids_present(self):
        """Coverage computation includes uncovered_ids list."""
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        reqs = [
            RfcRequirement(id="r:1", rfc="RFC", section="1", text="...", level="MUST"),
            RfcRequirement(id="r:2", rfc="RFC", section="2", text="...", level="MUST"),
            RfcRequirement(
                id="r:3", rfc="RFC", section="3", text="...", level="SHOULD"
            ),
        ]
        ann = RfcAnnotation(id="f:1:0", file="f", line=1, tags=["r:1"])
        for r in reqs:
            model.add_node(r)
        model.add_node(ann)

        covered = {"r:1"}
        uncovered_ids = [r.id for r in reqs if r.id not in covered]
        assert "r:2" in uncovered_ids
        assert "r:3" in uncovered_ids
        assert "r:1" not in uncovered_ids


class TestP1DiagnosticsFileField:
    """Test that ivy_diagnostics adds file field to each diagnostic."""

    @pytest.mark.asyncio
    async def test_diagnostics_include_file_per_diagnostic(self, tmp_path):
        """Each diagnostic dict has a file field."""
        (tmp_path / "test.ivy").write_text("type cid\n")  # missing #lang
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_diagnostics", {"relative_path": "test.ivy"})
        parsed = json.loads(_extract_text(result))
        for d in parsed["diagnostics"]:
            assert "file" in d, f"Diagnostic missing 'file' field: {d}"


class TestP1ModelSummarySortLimit:
    """Test sort_by and limit params on ivy_model_summary."""

    @pytest.mark.asyncio
    async def test_model_summary_limit(self):
        """limit=1 on 2 actions -> 1 row + hasMore=True."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {"limit": 1})
        parsed = json.loads(_extract_text(result))
        assert len(parsed["rows"]) == 1
        assert parsed.get("hasMore") is True

    @pytest.mark.asyncio
    async def test_model_summary_sort_by_name(self):
        """sort_by='name' -> rows sorted alphabetically by actionName."""
        mcp = _get_mcp_app(requirement_graph=_build_test_graph())
        result = await mcp.call_tool("ivy_model_summary", {"sort_by": "name"})
        parsed = json.loads(_extract_text(result))
        names = [r["actionName"] for r in parsed["rows"]]
        assert names == sorted(names)


class TestP1CoveragePerLevelPercent:
    """Test that by_level/by_layer entries have coverage_percent."""

    def test_by_level_has_coverage_percent(self):
        """Each by_level entry has coverage_percent and uncovered."""
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        reqs = [
            RfcRequirement(id="r:1", rfc="RFC", section="1", text="...", level="MUST"),
            RfcRequirement(id="r:2", rfc="RFC", section="2", text="...", level="MUST"),
        ]
        for r in reqs:
            model.add_node(r)
        model.add_node(RfcAnnotation(id="f:1:0", file="f", line=1, tags=["r:1"]))

        covered = {"r:1"}
        by_level = {}
        for r in reqs:
            level = r.level
            if level not in by_level:
                by_level[level] = {"total": 0, "covered": 0}
            by_level[level]["total"] += 1
            if r.id in covered:
                by_level[level]["covered"] += 1

        # Apply P1 enhancement
        for entry in by_level.values():
            entry["uncovered"] = entry["total"] - entry["covered"]
            entry["coverage_percent"] = (
                round(100 * entry["covered"] / entry["total"], 1)
                if entry["total"]
                else 0
            )

        assert by_level["MUST"]["uncovered"] == 1
        assert by_level["MUST"]["coverage_percent"] == 50.0
