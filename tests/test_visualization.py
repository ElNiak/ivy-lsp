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
    handle_coverage_gaps,
    handle_model_summary_table,
    register,
)
from ivy_lsp.semantic.nodes import RfcRequirement  # noqa: E402


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


# ---------------------------------------------------------------------------
# Helpers for coverage gaps tests
# ---------------------------------------------------------------------------


def _build_graph_with_gaps() -> ScopedRequirementModel:
    """Build a graph with unguarded state vars, orphan reqs, and RFC coverage gaps."""
    graph = ScopedRequirementModel()

    # Action with requirements
    graph.add_action(
        ActionNode(
            id="send_pkt",
            name="send_pkt",
            qualified_name="quic.send_pkt",
            file="/test/quic.ivy",
            line=10,
        )
    )

    # State var that IS guarded (read by a require-kind requirement)
    graph.add_state_var(
        StateVarNode(
            id="conn_state",
            name="conn_state",
            qualified_name="quic.conn_state",
            file="/test/quic.ivy",
            line=3,
            is_relation=False,
        )
    )

    # State var that is UNGUARDED (written but not read by any requirement)
    graph.add_state_var(
        StateVarNode(
            id="pkt_count",
            name="pkt_count",
            qualified_name="quic.pkt_count",
            file="/test/quic.ivy",
            line=4,
            is_relation=False,
        )
    )

    # Requirement that reads conn_state (guards it)
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
    graph.add_file_requirements("/test/quic.ivy", [r1])
    graph.add_edge(r1.id, EdgeType.READS, "conn_state")

    # pkt_count is written but never read by any requirement
    graph.add_edge(
        "/test/quic.ivy:20:write:pkt_count", EdgeType.WRITES, "pkt_count"
    )

    return graph


def _build_graph_with_orphan() -> ScopedRequirementModel:
    """Build a graph with an orphan requirement (action not in graph)."""
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

    # Normal requirement pointing at a known action
    r1 = RequirementNode(
        id="/test/quic.ivy:12",
        kind="require",
        formula_text="conn_state(C) = open",
        line=12,
        col=0,
        file="/test/quic.ivy",
        monitor_action="send_pkt",
        mixin_kind="before",
    )

    # Orphan: monitor_action refers to a non-existent action
    r_orphan = RequirementNode(
        id="/test/quic.ivy:30",
        kind="ensure",
        formula_text="ack_sent(C)",
        line=30,
        col=0,
        file="/test/quic.ivy",
        monitor_action="nonexistent_action",
        mixin_kind="after",
    )

    graph.add_file_requirements("/test/quic.ivy", [r1, r_orphan])
    return graph


def _build_graph_with_rfc_coverage() -> ScopedRequirementModel:
    """Build a graph with RFC requirements, some covered and some not."""
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

    # Add RFC requirements to the graph
    graph.add_rfc_requirement(
        RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="Connection must be open before sending",
            level="MUST",
        )
    )
    graph.add_rfc_requirement(
        RfcRequirement(
            id="rfc9000:8.1",
            rfc="RFC9000",
            section="8.1",
            text="Must validate address before use",
            level="MUST",
        )
    )
    graph.add_rfc_requirement(
        RfcRequirement(
            id="rfc9000:17.2",
            rfc="RFC9000",
            section="17.2",
            text="Short header packets must use connection ID",
            level="MUST",
        )
    )

    # Requirement that covers rfc9000:4.1 (via bracket_tags)
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

    graph.add_file_requirements("/test/quic.ivy", [r1])
    # rfc9000:8.1 and rfc9000:17.2 are NOT covered by any requirement

    return graph


# ---------------------------------------------------------------------------
# handle_coverage_gaps
# ---------------------------------------------------------------------------


class TestHandleCoverageGaps:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_coverage_gaps(server, {})
        assert result["unguardedStateVars"] == []
        assert result["orphanRequirements"] == []
        assert result["uncoveredRfcRequirements"] == []
        assert result["summary"]["totalStateVars"] == 0
        assert result["summary"]["unguardedCount"] == 0
        assert result["summary"]["totalRfcReqs"] == 0
        assert result["summary"]["uncoveredRfcCount"] == 0
        assert result["summary"]["orphanReqCount"] == 0

    def test_detects_unguarded_state_var(self):
        """A state var that is written but not read by any requirement is unguarded."""
        graph = _build_graph_with_gaps()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        unguarded_names = [v["name"] for v in result["unguardedStateVars"]]
        assert "pkt_count" in unguarded_names
        assert "conn_state" not in unguarded_names

    def test_unguarded_var_has_expected_fields(self):
        """Each unguarded state var entry has all expected fields."""
        graph = _build_graph_with_gaps()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        pkt_count_entry = next(
            v for v in result["unguardedStateVars"] if v["name"] == "pkt_count"
        )
        assert pkt_count_entry["qualifiedName"] == "quic.pkt_count"
        assert pkt_count_entry["file"] == "/test/quic.ivy"
        assert pkt_count_entry["line"] == 4
        assert pkt_count_entry["isWritten"] is True
        assert pkt_count_entry["severity"] == "high"

    def test_unguarded_but_not_written_is_low_severity(self):
        """A state var that is neither read nor written has low severity."""
        graph = _build_graph_with_gaps()
        # Add a third state var that is not written and not read
        graph.add_state_var(
            StateVarNode(
                id="idle_flag",
                name="idle_flag",
                qualified_name="quic.idle_flag",
                file="/test/quic.ivy",
                line=6,
                is_relation=False,
            )
        )
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        idle_entry = next(
            v for v in result["unguardedStateVars"] if v["name"] == "idle_flag"
        )
        assert idle_entry["isWritten"] is False
        assert idle_entry["severity"] == "low"

    def test_detects_orphan_requirements(self):
        """Requirements whose monitor_action matches no known action are orphans."""
        graph = _build_graph_with_orphan()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        orphan_ids = [o["id"] for o in result["orphanRequirements"]]
        assert "/test/quic.ivy:30" in orphan_ids
        # The requirement pointing at send_pkt is NOT orphaned
        assert "/test/quic.ivy:12" not in orphan_ids

    def test_orphan_has_expected_fields(self):
        """Each orphan entry has id, kind, formulaText, file, line, reason."""
        graph = _build_graph_with_orphan()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        orphan = result["orphanRequirements"][0]
        assert orphan["kind"] == "ensure"
        assert orphan["formulaText"] == "ack_sent(C)"
        assert orphan["file"] == "/test/quic.ivy"
        assert orphan["line"] == 30
        assert "nonexistent_action" in orphan["reason"]

    def test_rfc_coverage_tracking(self):
        """Uncovered RFC requirements are detected correctly."""
        graph = _build_graph_with_rfc_coverage()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        uncovered_ids = [r["id"] for r in result["uncoveredRfcRequirements"]]
        # rfc9000:4.1 is covered by r1
        assert "rfc9000:4.1" not in uncovered_ids
        # rfc9000:8.1 and rfc9000:17.2 are uncovered
        assert "rfc9000:8.1" in uncovered_ids
        assert "rfc9000:17.2" in uncovered_ids

    def test_rfc_uncovered_entry_has_expected_fields(self):
        """Each uncovered RFC entry has id, rfc, section, level, text."""
        graph = _build_graph_with_rfc_coverage()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        entry = next(
            r
            for r in result["uncoveredRfcRequirements"]
            if r["id"] == "rfc9000:8.1"
        )
        assert entry["rfc"] == "RFC9000"
        assert entry["section"] == "8.1"
        assert entry["level"] == "MUST"
        assert entry["text"] == "Must validate address before use"

    def test_summary_counts_are_correct(self):
        """Summary aggregates totalActions, totalRequirements, totalStateVars, etc."""
        graph = _build_graph_with_gaps()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        s = result["summary"]
        assert s["totalActions"] == 1
        assert s["totalRequirements"] == 1
        assert s["totalStateVars"] == 2
        # pkt_count is unguarded
        assert s["unguardedCount"] >= 1

    def test_summary_rfc_counts(self):
        """Summary reflects correct RFC coverage counts."""
        graph = _build_graph_with_rfc_coverage()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        s = result["summary"]
        assert s["totalRfcReqs"] == 3
        # 1 covered (rfc9000:4.1), 2 uncovered
        assert s["uncoveredRfcCount"] == 2

    def test_scope_info_present(self):
        """Result includes scopeInfo."""
        graph = _build_graph_with_gaps()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        assert "scopeInfo" in result
        assert "testFile" in result["scopeInfo"]
        assert "scoped" in result["scopeInfo"]


# ---------------------------------------------------------------------------
# register() — LSP wiring
# ---------------------------------------------------------------------------


class _FakeFeatureServer:
    """Minimal mock that captures @server.feature() registrations."""

    def __init__(self):
        self._handlers = {}
        self._indexer = type("I", (), {"_requirement_graph": _build_graph()})()

    def feature(self, method_name):
        def decorator(fn):
            self._handlers[method_name] = fn
            return fn

        return decorator


class TestVisualizationRegister:
    def test_register_adds_all_endpoints(self):
        server = _FakeFeatureServer()
        register(server)
        assert "ivy/actionRequirements" in server._handlers
        assert "ivy/modelSummaryTable" in server._handlers
        assert "ivy/coverageGaps" in server._handlers

    def test_register_adds_exactly_three_endpoints(self):
        server = _FakeFeatureServer()
        register(server)
        assert len(server._handlers) == 3

    def test_action_requirements_endpoint_callable(self):
        server = _FakeFeatureServer()
        register(server)
        handler = server._handlers["ivy/actionRequirements"]
        result = handler({})
        assert "actions" in result
        assert result["modelReady"] is True

    def test_model_summary_table_endpoint_callable(self):
        server = _FakeFeatureServer()
        register(server)
        handler = server._handlers["ivy/modelSummaryTable"]
        result = handler({})
        assert "rows" in result
        assert "totals" in result

    def test_coverage_gaps_endpoint_callable(self):
        server = _FakeFeatureServer()
        register(server)
        handler = server._handlers["ivy/coverageGaps"]
        result = handler({})
        assert "unguardedStateVars" in result
        assert "summary" in result

    def test_endpoints_return_valid_json_types(self):
        server = _FakeFeatureServer()
        register(server)
        for name, handler in server._handlers.items():
            result = handler({})
            assert isinstance(result, dict), f"{name} should return dict"

    def test_endpoints_handle_none_params(self):
        """Handlers should tolerate None params (defaulting to {})."""
        server = _FakeFeatureServer()
        register(server)
        for name, handler in server._handlers.items():
            result = handler(None)
            assert isinstance(result, dict), f"{name} should handle None params"
