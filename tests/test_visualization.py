"""Tests for visualization endpoint handlers."""

import sys
from pathlib import Path

import pytest  # noqa: F401

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
from ivy_lsp.features.visualization import (  # noqa: E402
    _get_requirement_graph,
    _resolve_scope,
    _serialize_requirement,
    handle_action_requirements,
    handle_model_summary_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_graph() -> ScopedRequirementModel:
    """Build a small graph for testing."""
    graph = ScopedRequirementModel()
    graph.add_action(
        ActionNode(
            id="send_pkt",
            name="send_pkt",
            qualified_name="quic.send_pkt",
            file="/test/quic.ivy",
            line=10,
        )
    )
    graph.add_action(
        ActionNode(
            id="recv_pkt",
            name="recv_pkt",
            qualified_name="quic.recv_pkt",
            file="/test/quic.ivy",
            line=20,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="sent",
            name="sent",
            qualified_name="quic.sent",
            file="/test/quic.ivy",
            line=5,
            is_relation=True,
        )
    )
    r1 = RequirementNode(
        id="/test/quic.ivy:12",
        kind="require",
        formula_text="conn_state(C) = open",
        line=12,
        col=0,
        file="/test/quic.ivy",
        monitor_action="send_pkt",
        mixin_kind="before",
        bracket_tags=["rfc9000:4.1"],
    )
    r2 = RequirementNode(
        id="/test/quic.ivy:15",
        kind="ensure",
        formula_text="sent(C, P)",
        line=15,
        col=0,
        file="/test/quic.ivy",
        monitor_action="send_pkt",
        mixin_kind="after",
    )
    graph.add_file_requirements("/test/quic.ivy", [r1, r2])
    graph.add_edge(r1.id, EdgeType.READS, "sent")
    return graph


class _FakeServer:
    def __init__(self, graph=None):
        self._indexer = (
            type("I", (), {"_requirement_graph": graph})() if graph else None
        )


# ---------------------------------------------------------------------------
# _get_requirement_graph
# ---------------------------------------------------------------------------


class TestGetRequirementGraph:
    def test_returns_graph_when_available(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        assert _get_requirement_graph(server) is graph

    def test_returns_none_when_no_indexer(self):
        server = _FakeServer(None)
        assert _get_requirement_graph(server) is None


# ---------------------------------------------------------------------------
# _serialize_requirement
# ---------------------------------------------------------------------------


class TestSerializeRequirement:
    def test_serializes_requirement(self):
        graph = _build_graph()
        req = list(graph.requirements.values())[0]
        result = _serialize_requirement(req, graph)
        assert result["id"] == req.id
        assert result["kind"] == req.kind
        assert result["formulaText"] == req.formula_text
        assert result["line"] == req.line
        assert result["file"] == req.file
        assert result["bracketTags"] == req.bracket_tags
        assert isinstance(result["stateVarsRead"], list)


# ---------------------------------------------------------------------------
# handle_action_requirements
# ---------------------------------------------------------------------------


class TestHandleActionRequirements:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_action_requirements(server, {})
        assert result["actions"] == []
        assert result["modelReady"] is False

    def test_returns_all_actions_when_no_filter(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(server, {})
        assert result["modelReady"] is True
        assert len(result["actions"]) == 2
        names = {a["actionName"] for a in result["actions"]}
        assert "send_pkt" in names
        assert "recv_pkt" in names

    def test_filters_by_action_name(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(server, {"actionName": "send_pkt"})
        assert len(result["actions"]) == 1
        action = result["actions"][0]
        assert action["actionName"] == "send_pkt"
        assert len(action["monitors"]["before"]) == 1
        assert len(action["monitors"]["after"]) == 1
        assert action["monitors"]["before"][0]["kind"] == "require"
        assert action["monitors"]["after"][0]["kind"] == "ensure"

    def test_counts_are_correct(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(server, {"actionName": "send_pkt"})
        action = result["actions"][0]
        assert action["counts"]["require"] == 1
        assert action["counts"]["ensure"] == 1
        assert action["counts"]["total"] == 2

    def test_rfc_tags_aggregated(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(server, {"actionName": "send_pkt"})
        action = result["actions"][0]
        assert "rfc9000:4.1" in action["rfcTags"]


# ---------------------------------------------------------------------------
# handle_model_summary_table
# ---------------------------------------------------------------------------


class TestHandleModelSummaryTable:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_model_summary_table(server, {})
        assert result["rows"] == []
        assert result["totals"]["actions"] == 0
        assert result["totals"]["requirements"] == 0

    def test_returns_row_per_action(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        assert len(result["rows"]) == 2

    def test_row_has_correct_counts(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        send_row = next(r for r in result["rows"] if r["actionName"] == "send_pkt")
        # r1 is kind="require" mixin_kind="before" -> beforeRequireCount=1
        assert send_row["beforeRequireCount"] == 1
        # r2 is kind="ensure" mixin_kind="after" -> afterEnsureCount=1
        assert send_row["afterEnsureCount"] == 1
        assert send_row["totalRequirements"] == 2

    def test_recv_pkt_row_has_zero_counts(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        recv_row = next(r for r in result["rows"] if r["actionName"] == "recv_pkt")
        assert recv_row["beforeRequireCount"] == 0
        assert recv_row["afterEnsureCount"] == 0
        assert recv_row["totalRequirements"] == 0

    def test_totals_are_aggregated(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        assert result["totals"]["actions"] == 2
        # Only send_pkt has 2 requirements; recv_pkt has 0
        assert result["totals"]["requirements"] == 2

    def test_rfc_coverage_in_row(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        send_row = next(r for r in result["rows"] if r["actionName"] == "send_pkt")
        # r1 has bracket_tags=["rfc9000:4.1"], r2 has no tags
        assert "rfc9000:4.1" in send_row["rfcTagsCovered"]
        assert send_row["rfcCoverageCount"] == 1

    def test_state_vars_read_aggregated(self):
        """State vars read across all requirements for an action are counted."""
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        send_row = next(r for r in result["rows"] if r["actionName"] == "send_pkt")
        # r1 READS "sent" (edge added in _build_graph)
        assert send_row["stateVarsRead"] >= 1

    def test_scope_info_present(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        assert "scopeInfo" in result
        assert "testFile" in result["scopeInfo"]
        assert "scoped" in result["scopeInfo"]

    def test_row_contains_expected_fields(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        row = result["rows"][0]
        expected_fields = {
            "actionName",
            "qualifiedName",
            "file",
            "line",
            "direction",
            "beforeRequireCount",
            "beforeEnsureCount",
            "afterRequireCount",
            "afterEnsureCount",
            "assumeCount",
            "assertCount",
            "totalRequirements",
            "stateVarsRead",
            "stateVarsWritten",
            "rfcTagsCovered",
            "rfcCoverageCount",
        }
        assert expected_fields.issubset(set(row.keys()))
