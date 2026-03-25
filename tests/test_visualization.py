"""Tests for visualization endpoint handlers."""

import asyncio
import sys
from pathlib import Path

import pytest  # noqa: F401

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.requirement_graph import (  # noqa: E402
    ActionNode,
    EdgeType,
    PropertyNode,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel  # noqa: E402
from ivy_lsp.core.semantic.nodes import RfcRequirement  # noqa: E402
from ivy_lsp.lsp.visualization import (  # noqa: E402
    _get_requirement_graph,
    _resolve_scope,
    _serialize_requirement,
    handle_action_dependency_graph,
    handle_action_requirements,
    handle_coverage_gaps,
    handle_layered_overview,
    handle_model_summary_table,
    handle_smart_suggestions,
    handle_state_machine_view,
    register,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_graph() -> ScopedRequirementModel:
    """Build a small graph for testing."""
    graph = ScopedRequirementModel()
    # Add requirements FIRST — add_file_requirements() calls remove_file()
    # internally, which deletes all nodes from that filepath.  Actions and
    # state vars must be added AFTER so they are not wiped out.
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
    graph.add_edge(r1.id, EdgeType.READS, "sent")
    return graph


class _FakeServer:
    def __init__(self, graph=None):
        self.indexer = type("I", (), {"requirement_graph": graph})() if graph else None
        self.initializing = False


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


class TestStateVarsWrittenPerAction:
    """Verify stateVarsWritten is per-action, not global."""

    def test_different_actions_have_different_writes(self):
        """Two actions in different files should have different stateVarsWritten."""
        graph = ScopedRequirementModel()

        # Requirements for two different actions in different files
        r1 = RequirementNode(
            id="/test/file_a.ivy:10",
            kind="require",
            formula_text="x > 0",
            line=10,
            col=0,
            file="/test/file_a.ivy",
            monitor_action="action_a",
            mixin_kind="before",
        )
        r2 = RequirementNode(
            id="/test/file_b.ivy:20",
            kind="require",
            formula_text="y > 0",
            line=20,
            col=0,
            file="/test/file_b.ivy",
            monitor_action="action_b",
            mixin_kind="before",
        )

        # Writes in file_a (belongs to action_a)
        writes_a = [("var_x", "/test/file_a.ivy", 15)]
        # Writes in file_b (belongs to action_b)
        writes_b = [
            ("var_y", "/test/file_b.ivy", 25),
            ("var_z", "/test/file_b.ivy", 26),
        ]

        graph.add_file_requirements("/test/file_a.ivy", [r1], writes_a)
        graph.add_file_requirements("/test/file_b.ivy", [r2], writes_b)

        graph.add_action_if_absent(
            ActionNode(
                id="action_a",
                name="action_a",
                qualified_name="action_a",
                file="/test/file_a.ivy",
                line=5,
            )
        )
        graph.add_action_if_absent(
            ActionNode(
                id="action_b",
                name="action_b",
                qualified_name="action_b",
                file="/test/file_b.ivy",
                line=15,
            )
        )

        for var_name, fp, line in writes_a + writes_b:
            if var_name not in graph.state_vars:
                graph.add_state_var(
                    StateVarNode(
                        id=var_name,
                        name=var_name,
                        qualified_name=var_name,
                        file=fp,
                        line=line,
                    )
                )

        snap = graph.snapshot()

        writes_for_a = snap.get_state_vars_written_by_action("action_a")
        writes_for_b = snap.get_state_vars_written_by_action("action_b")

        # action_a has 1 write (var_x), action_b has 2 writes (var_y, var_z)
        assert len(writes_for_a) == 1
        assert writes_for_a[0].id == "var_x"
        assert len(writes_for_b) == 2
        written_ids = {sv.id for sv in writes_for_b}
        assert written_ids == {"var_y", "var_z"}

        # This should NOT be the global count (3)
        all_written = snap.get_all_state_vars_written()
        assert len(all_written) == 3  # global: all 3 vars
        assert len(writes_for_a) < len(all_written)
        assert len(writes_for_b) < len(all_written)


# ---------------------------------------------------------------------------
# Helpers for coverage gaps tests
# ---------------------------------------------------------------------------


def _build_graph_with_gaps() -> ScopedRequirementModel:
    """Build a graph with unguarded state vars, orphan reqs, and RFC coverage gaps."""
    graph = ScopedRequirementModel()

    # Requirements FIRST (add_file_requirements calls remove_file internally)
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

    graph.add_edge(r1.id, EdgeType.READS, "conn_state")

    # pkt_count is written but never read by any requirement
    graph.add_edge("/test/quic.ivy:20:write:pkt_count", EdgeType.WRITES, "pkt_count")

    return graph


def _build_graph_with_orphan() -> ScopedRequirementModel:
    """Build a graph with an orphan requirement (action not in graph)."""
    graph = ScopedRequirementModel()

    # Requirements FIRST (add_file_requirements calls remove_file internally)
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

    graph.add_action(
        ActionNode(
            id="send_pkt",
            name="send_pkt",
            qualified_name="quic.send_pkt",
            file="/test/quic.ivy",
            line=10,
        )
    )

    return graph


def _build_graph_with_rfc_coverage() -> ScopedRequirementModel:
    """Build a graph with RFC requirements, some covered and some not."""
    graph = ScopedRequirementModel()

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

    # Requirements FIRST (add_file_requirements calls remove_file internally)
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

    graph.add_action(
        ActionNode(
            id="send_pkt",
            name="send_pkt",
            qualified_name="quic.send_pkt",
            file="/test/quic.ivy",
            line=10,
        )
    )
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
            r for r in result["uncoveredRfcRequirements"] if r["id"] == "rfc9000:8.1"
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
# Helpers for action dependency graph tests
# ---------------------------------------------------------------------------


def _build_graph_with_shared_state() -> ScopedRequirementModel:
    """Build a graph where two actions share a state variable."""
    graph = ScopedRequirementModel()
    # Requirements FIRST (add_file_requirements calls remove_file internally)
    r1 = RequirementNode(
        id="/test/quic.ivy:15",
        kind="ensure",
        formula_text="conn_state(C) = sending",
        line=15,
        col=0,
        file="/test/quic.ivy",
        monitor_action="send_pkt",
        mixin_kind="after",
    )
    r2 = RequirementNode(
        id="/test/quic.ivy:25",
        kind="require",
        formula_text="conn_state(C) = open",
        line=25,
        col=0,
        file="/test/quic.ivy",
        monitor_action="recv_pkt",
        mixin_kind="before",
    )
    graph.add_file_requirements("/test/quic.ivy", [r1, r2])
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
            id="conn_state",
            name="conn_state",
            qualified_name="quic.conn_state",
            file="/test/quic.ivy",
            line=3,
            is_relation=False,
        )
    )
    graph.add_edge(r1.id, EdgeType.WRITES, "conn_state")
    graph.add_edge(r2.id, EdgeType.READS, "conn_state")
    return graph


# ---------------------------------------------------------------------------
# handle_action_dependency_graph
# ---------------------------------------------------------------------------


class TestHandleActionDependencyGraph:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_action_dependency_graph(server, {})
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_returns_action_nodes(self):
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        node_ids = {n["id"] for n in result["nodes"]}
        assert "send_pkt" in node_ids
        assert "recv_pkt" in node_ids

    def test_action_node_has_expected_fields(self):
        """Each action node should have id, label, type, file, line, requirementCount."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        action_nodes = [n for n in result["nodes"] if n["type"] == "action"]
        assert len(action_nodes) >= 2
        for node in action_nodes:
            assert "id" in node
            assert "label" in node
            assert "type" in node
            assert node["type"] == "action"
            assert "file" in node
            assert "line" in node
            assert "requirementCount" in node

    def test_shared_state_creates_edge(self):
        """send_pkt WRITES conn_state, recv_pkt READS conn_state -> edge from send_pkt to recv_pkt."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        edges = result["edges"]
        assert len(edges) >= 1
        edge_pairs = {(e["source"], e["target"]) for e in edges}
        # Writer -> Reader direction
        assert ("send_pkt", "recv_pkt") in edge_pairs

    def test_edge_has_label_and_type(self):
        """Edges via shared state should carry label (var name) and type."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        edges = result["edges"]
        assert len(edges) >= 1
        edge = edges[0]
        assert "label" in edge
        assert "type" in edge
        assert edge["type"] == "shared_state"

    def test_no_self_edges(self):
        """Actions should not have edges to themselves via shared state vars."""
        graph = ScopedRequirementModel()
        # Requirements FIRST (add_file_requirements calls remove_file internally)
        r1 = RequirementNode(
            id="/test/quic.ivy:12",
            kind="ensure",
            formula_text="var_x := 1",
            line=12,
            col=0,
            file="/test/quic.ivy",
            monitor_action="act_a",
            mixin_kind="after",
        )
        r2 = RequirementNode(
            id="/test/quic.ivy:13",
            kind="require",
            formula_text="var_x > 0",
            line=13,
            col=0,
            file="/test/quic.ivy",
            monitor_action="act_a",
            mixin_kind="before",
        )
        graph.add_file_requirements("/test/quic.ivy", [r1, r2])
        graph.add_action(
            ActionNode(
                id="act_a",
                name="act_a",
                qualified_name="quic.act_a",
                file="/test/quic.ivy",
                line=10,
            )
        )
        graph.add_state_var(
            StateVarNode(
                id="var_x",
                name="var_x",
                qualified_name="quic.var_x",
                file="/test/quic.ivy",
                line=3,
                is_relation=False,
            )
        )
        graph.add_edge(r1.id, EdgeType.WRITES, "var_x")
        graph.add_edge(r2.id, EdgeType.READS, "var_x")
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        for edge in result["edges"]:
            assert edge["source"] != edge["target"], "Self-edges should not exist"

    def test_includes_state_var_nodes_when_requested(self):
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {"includeStateVars": True})
        types = {n["type"] for n in result["nodes"]}
        assert "stateVar" in types

    def test_state_var_nodes_have_expected_fields(self):
        """State var nodes include id, label, type, file, line."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {"includeStateVars": True})
        sv_nodes = [n for n in result["nodes"] if n["type"] == "stateVar"]
        assert len(sv_nodes) >= 1
        for node in sv_nodes:
            assert "id" in node
            assert "label" in node
            assert node["type"] == "stateVar"
            assert "file" in node
            assert "line" in node

    def test_state_var_edges_when_included(self):
        """When includeStateVars is True, edges include writes and reads types."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {"includeStateVars": True})
        edge_types = {e["type"] for e in result["edges"]}
        assert "writes" in edge_types
        assert "reads" in edge_types

    def test_does_not_include_state_var_nodes_by_default(self):
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        types = {n["type"] for n in result["nodes"]}
        assert "stateVar" not in types

    def test_scope_info_present(self):
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        assert "scopeInfo" in result
        assert "testFile" in result["scopeInfo"]
        assert "scoped" in result["scopeInfo"]

    def test_requirement_count_on_action_nodes(self):
        """RequirementCount reflects the number of requirements for each action."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {})
        send_node = next(n for n in result["nodes"] if n["id"] == "send_pkt")
        recv_node = next(n for n in result["nodes"] if n["id"] == "recv_pkt")
        # send_pkt has r1 (ensure, after), recv_pkt has r2 (require, before)
        assert send_node["requirementCount"] == 1
        assert recv_node["requirementCount"] == 1


# ---------------------------------------------------------------------------
# Helpers for state machine view tests
# ---------------------------------------------------------------------------


def _build_graph_for_state_machine() -> ScopedRequirementModel:
    """Build a graph suitable for state machine view testing.

    Contains:
    - Two actions: send_pkt (writes conn_state), recv_pkt (reads conn_state)
    - One state var: conn_state
    - A require guard on recv_pkt (conn_state = open)
    - An assume guard on recv_pkt (conn_state ~= closed)
    - A property (invariant) that reads conn_state
    """
    graph = ScopedRequirementModel()

    # Requirements FIRST (add_file_requirements calls remove_file internally)
    r1 = RequirementNode(
        id="/test/quic.ivy:15",
        kind="ensure",
        formula_text="conn_state(C) := sending",
        line=15,
        col=0,
        file="/test/quic.ivy",
        monitor_action="send_pkt",
        mixin_kind="after",
    )
    r2 = RequirementNode(
        id="/test/quic.ivy:25",
        kind="require",
        formula_text="conn_state(C) = open",
        line=25,
        col=0,
        file="/test/quic.ivy",
        monitor_action="recv_pkt",
        mixin_kind="before",
    )
    r3 = RequirementNode(
        id="/test/quic.ivy:26",
        kind="assume",
        formula_text="conn_state(C) ~= closed",
        line=26,
        col=0,
        file="/test/quic.ivy",
        monitor_action="recv_pkt",
        mixin_kind="before",
    )
    graph.add_file_requirements("/test/quic.ivy", [r1, r2, r3])

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
            id="conn_state",
            name="conn_state",
            qualified_name="quic.conn_state",
            file="/test/quic.ivy",
            line=3,
            is_relation=False,
        )
    )
    graph.add_edge(r1.id, EdgeType.WRITES, "conn_state")
    graph.add_edge(r2.id, EdgeType.READS, "conn_state")
    graph.add_edge(r3.id, EdgeType.READS, "conn_state")

    # Property (invariant) that reads conn_state
    prop = PropertyNode(
        id="/test/quic.ivy:50",
        kind="invariant",
        name="conn_valid",
        formula_text="forall C. conn_state(C) ~= invalid",
        file="/test/quic.ivy",
        line=50,
    )
    graph.add_property(prop)
    graph.add_edge(prop.id, EdgeType.READS, "conn_state")

    return graph


# ---------------------------------------------------------------------------
# handle_state_machine_view
# ---------------------------------------------------------------------------


class TestHandleStateMachineView:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_state_machine_view(server, {})
        assert result["nodes"] == []
        assert result["transitions"] == []
        assert "scopeInfo" in result

    def test_state_vars_become_nodes(self):
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        state_nodes = [n for n in result["nodes"] if n["type"] == "state"]
        node_ids = {n["id"] for n in state_nodes}
        assert "conn_state" in node_ids

    def test_state_node_has_expected_fields(self):
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        state_nodes = [n for n in result["nodes"] if n["type"] == "state"]
        assert len(state_nodes) >= 1
        node = state_nodes[0]
        assert "id" in node
        assert "label" in node
        assert node["type"] == "state"
        assert "file" in node
        assert "line" in node

    def test_invariant_nodes_from_properties(self):
        """Properties that read active state vars appear as invariant nodes."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        inv_nodes = [n for n in result["nodes"] if n["type"] == "invariant"]
        assert len(inv_nodes) >= 1
        inv = inv_nodes[0]
        assert inv["label"] == "conn_valid"
        assert inv["file"] == "/test/quic.ivy"
        assert inv["line"] == 50

    def test_actions_become_transitions(self):
        """Actions that read/write state vars become transitions."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        transitions = result["transitions"]
        action_names = {t["action"] for t in transitions}
        # send_pkt writes conn_state, recv_pkt reads conn_state
        assert "send_pkt" in action_names or "recv_pkt" in action_names

    def test_transition_has_expected_fields(self):
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        assert len(result["transitions"]) >= 1
        t = result["transitions"][0]
        assert "source" in t
        assert "target" in t
        assert "action" in t
        assert "guards" in t

    def test_guards_from_require_assume(self):
        """recv_pkt has require and assume guards on conn_state."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        recv_transitions = [
            t for t in result["transitions"] if t["action"] == "recv_pkt"
        ]
        # recv_pkt reads conn_state -> should have transitions
        assert len(recv_transitions) >= 1
        guards = recv_transitions[0]["guards"]
        # require and assume formulas should appear as guards
        guard_texts = set(guards)
        assert "conn_state(C) = open" in guard_texts
        assert "conn_state(C) ~= closed" in guard_texts

    def test_send_pkt_has_no_guards(self):
        """send_pkt has only an ensure (not require/assume), so no guards."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        send_transitions = [
            t for t in result["transitions"] if t["action"] == "send_pkt"
        ]
        if send_transitions:
            assert send_transitions[0]["guards"] == []

    def test_state_var_filter(self):
        """When stateVarFilter is set, only matching state vars appear."""
        graph = _build_graph_for_state_machine()
        # Add a second state var that is also active
        graph.add_state_var(
            StateVarNode(
                id="pkt_num",
                name="pkt_num",
                qualified_name="quic.pkt_num",
                file="/test/quic.ivy",
                line=4,
                is_relation=False,
            )
        )
        r_extra = RequirementNode(
            id="/test/quic.ivy:30",
            kind="ensure",
            formula_text="pkt_num(C) := pkt_num(C) + 1",
            line=30,
            col=0,
            file="/test/quic.ivy",
            monitor_action="send_pkt",
            mixin_kind="after",
        )
        # Use add_requirement (not add_file_requirements) to avoid wiping
        # existing nodes via remove_file.
        graph.add_requirement(r_extra)
        graph.add_edge(r_extra.id, EdgeType.CONSTRAINS, r_extra.monitor_action)
        graph.add_edge(r_extra.id, EdgeType.WRITES, "pkt_num")

        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {"stateVarFilter": "conn_state"})
        state_nodes = [n for n in result["nodes"] if n["type"] == "state"]
        state_names = {n["label"] for n in state_nodes}
        assert "conn_state" in state_names
        assert "pkt_num" not in state_names

    def test_inactive_state_vars_excluded(self):
        """State vars not involved in any action monitor are excluded."""
        graph = _build_graph_for_state_machine()
        # Add a state var with no edges
        graph.add_state_var(
            StateVarNode(
                id="orphan_var",
                name="orphan_var",
                qualified_name="quic.orphan_var",
                file="/test/quic.ivy",
                line=99,
                is_relation=False,
            )
        )
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        node_ids = {n["id"] for n in result["nodes"]}
        assert "orphan_var" not in node_ids

    def test_scope_info_present(self):
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        assert "scopeInfo" in result
        assert "testFile" in result["scopeInfo"]
        assert "scoped" in result["scopeInfo"]


# ---------------------------------------------------------------------------
# handle_layered_overview
# ---------------------------------------------------------------------------


class TestHandleLayeredOverview:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_layered_overview(server, {})
        assert result["layers"] == []

    def test_groups_by_file(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_layered_overview(server, {})
        assert len(result["layers"]) >= 1
        layer = result["layers"][0]
        assert "file" in layer
        assert isinstance(layer["actions"], list)
        assert isinstance(layer["stateVars"], list)
        assert isinstance(layer["requirements"], int)

    def test_groups_by_module(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_layered_overview(server, {"groupBy": "module"})
        assert len(result["layers"]) >= 1


# ---------------------------------------------------------------------------
# handle_smart_suggestions
# ---------------------------------------------------------------------------


class TestHandleSmartSuggestions:
    def test_returns_empty_when_no_graph(self):
        server = _FakeServer(None)
        result = handle_smart_suggestions(
            server, {"filePath": "/test/q.ivy", "line": 5}
        )
        assert result["suggestions"] == []

    def test_returns_suggestions_for_monitor_context(self):
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_smart_suggestions(
            server,
            {
                "filePath": "/test/quic.ivy",
                "line": 12,
                "actionName": "send_pkt",
            },
        )
        # Should return a list of suggestions (may be empty if no gaps)
        assert isinstance(result["suggestions"], list)

    def test_workspace_level_returns_uncovered_actions(self):
        """Without actionName, should report actions with no requirements."""
        graph = _build_graph()
        server = _FakeServer(graph)
        # recv_pkt has no CONSTRAINS edges in _build_graph
        result = handle_smart_suggestions(server, {})
        assert isinstance(result["suggestions"], list)
        uncovered = [
            s for s in result["suggestions"] if s["type"] == "uncovered_action"
        ]
        names = [s["name"] for s in uncovered]
        assert "recv_pkt" in names

    def test_workspace_level_returns_unguarded_state(self):
        """Without actionName, should report state vars never read."""
        graph = ScopedRequirementModel()
        # Add a state var that is written but never read
        graph.add_state_var(
            StateVarNode(
                id="orphan_var",
                name="orphan_var",
                qualified_name="q.orphan_var",
                file="/test/q.ivy",
                line=1,
                is_relation=True,
            )
        )
        graph.add_action(
            ActionNode(
                id="act1",
                name="act1",
                qualified_name="q.act1",
                file="/test/q.ivy",
                line=5,
            )
        )
        server = _FakeServer(graph)
        result = handle_smart_suggestions(server, {})
        unguarded = [s for s in result["suggestions"] if s["type"] == "unguarded_state"]
        assert any(s["name"] == "orphan_var" for s in unguarded)

    def test_pattern_hint_for_frame_file(self):
        """Pattern hints are added for frame/message files."""
        graph = _build_graph()
        server = _FakeServer(graph)
        result = handle_smart_suggestions(server, {"filePath": "/test/quic_frame.ivy"})
        hints = [s for s in result["suggestions"] if s["type"] == "pattern_hint"]
        assert len(hints) >= 1
        assert "variant" in hints[0]["message"].lower()


# ---------------------------------------------------------------------------
# register() — LSP wiring
# ---------------------------------------------------------------------------


class _FakeFeatureServer:
    """Minimal mock that captures @server.feature() registrations."""

    def __init__(self):
        self._handlers = {}
        self.indexer = type("I", (), {"requirement_graph": _build_graph()})()
        self.initializing = False

    def feature(self, method_name):
        def decorator(fn):
            if asyncio.iscoroutinefunction(fn):
                import functools

                @functools.wraps(fn)
                def _sync(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))

                self._handlers[method_name] = _sync
            else:
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
        assert "ivy/actionDependencyGraph" in server._handlers
        assert "ivy/stateMachineView" in server._handlers
        assert "ivy/layeredOverview" in server._handlers
        assert "ivy/smartSuggestions" in server._handlers

    def test_register_adds_exactly_seven_endpoints(self):
        server = _FakeFeatureServer()
        register(server)
        assert len(server._handlers) == 7

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

    def test_state_machine_view_endpoint_callable(self):
        server = _FakeFeatureServer()
        register(server)
        handler = server._handlers["ivy/stateMachineView"]
        result = handler({})
        assert "nodes" in result
        assert "transitions" in result

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


# ---------------------------------------------------------------------------
# H4: Numeric parameter validation
# ---------------------------------------------------------------------------


class TestNumericParamValidation:
    """H4: String-type numeric params should be coerced safely."""

    def test_max_edges_string_coercion_no_error(self):
        """MaxEdges as '1' (string) should not raise TypeError."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        # With string "1", should coerce to int and truncate (not error out)
        result = handle_action_dependency_graph(server, {"maxEdges": "1"})
        assert isinstance(result, dict)
        assert "edges" in result
        assert len(result["edges"]) <= 1

    def test_max_transitions_string_truncates_correctly(self):
        """MaxTransitions as '1' (string) should truncate to exactly 1 transition."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        # First verify >1 transitions with default
        baseline = handle_state_machine_view(server, {"maxTransitions": 500})
        baseline_count = len(baseline["transitions"])
        assert baseline_count > 1, "Test graph needs >1 transitions"

        result = handle_state_machine_view(server, {"maxTransitions": "1"})
        assert isinstance(result, dict)
        assert len(result["transitions"]) == 1
        assert result["truncated"] is True

    def test_max_edges_invalid_string_uses_default(self):
        """Non-numeric string for maxEdges should fall back to default."""
        graph = _build_graph_with_shared_state()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {"maxEdges": "not-a-number"})
        assert isinstance(result, dict)
        assert "edges" in result

    def test_max_transitions_invalid_string_uses_default(self):
        """Non-numeric string for maxTransitions should fall back to default."""
        graph = _build_graph_for_state_machine()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {"maxTransitions": "not-a-number"})
        assert isinstance(result, dict)
        assert "transitions" in result
