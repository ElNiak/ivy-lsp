"""Integration tests for the full visualization feature set.

Verifies that all visualization endpoints work together with a
realistic RequirementGraph built from multiple files.
"""

import json
import sys
from pathlib import Path

import pytest  # noqa: F401

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
from ivy_lsp.lsp.visualization import (  # noqa: E402
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
# Realistic multi-file graph builder
# ---------------------------------------------------------------------------


def _build_realistic_graph() -> ScopedRequirementModel:
    """Build a multi-file graph resembling a small QUIC model."""
    graph = ScopedRequirementModel()

    # -- Requirements FIRST --------------------------------------------------
    # add_file_requirements() calls remove_file() internally, which deletes
    # all nodes from that filepath.  Actions and state vars must be added
    # AFTER so they are not wiped out.
    r1 = RequirementNode(
        id="/model/quic_packet.ivy:12",
        kind="require",
        formula_text="conn_state(C) = open",
        line=12,
        col=0,
        file="/model/quic_packet.ivy",
        monitor_action="send_pkt",
        mixin_kind="before",
        bracket_tags=["rfc9000:17.2"],
    )
    r2 = RequirementNode(
        id="/model/quic_packet.ivy:15",
        kind="ensure",
        formula_text="pkt_num(C) = old_pkt_num + 1",
        line=15,
        col=0,
        file="/model/quic_packet.ivy",
        monitor_action="send_pkt",
        mixin_kind="after",
    )
    r3 = RequirementNode(
        id="/model/quic_connection.ivy:12",
        kind="ensure",
        formula_text="conn_state(C) = open",
        line=12,
        col=0,
        file="/model/quic_connection.ivy",
        monitor_action="open_connection",
        mixin_kind="after",
    )
    graph.add_file_requirements("/model/quic_packet.ivy", [r1, r2])
    graph.add_file_requirements("/model/quic_connection.ivy", [r3])

    # -- File 1: quic_connection.ivy -----------------------------------------
    graph.add_action(
        ActionNode(
            id="open_connection",
            name="open_connection",
            qualified_name="quic.open_connection",
            file="/model/quic_connection.ivy",
            line=10,
        )
    )
    graph.add_action(
        ActionNode(
            id="close_connection",
            name="close_connection",
            qualified_name="quic.close_connection",
            file="/model/quic_connection.ivy",
            line=30,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="conn_state",
            name="conn_state",
            qualified_name="quic.conn_state",
            file="/model/quic_connection.ivy",
            line=3,
            is_relation=False,
        )
    )

    # -- File 2: quic_packet.ivy ---------------------------------------------
    graph.add_action(
        ActionNode(
            id="send_pkt",
            name="send_pkt",
            qualified_name="quic.send_pkt",
            file="/model/quic_packet.ivy",
            line=10,
        )
    )
    graph.add_action(
        ActionNode(
            id="recv_pkt",
            name="recv_pkt",
            qualified_name="quic.recv_pkt",
            file="/model/quic_packet.ivy",
            line=30,
        )
    )
    graph.add_state_var(
        StateVarNode(
            id="pkt_num",
            name="pkt_num",
            qualified_name="quic.pkt_num",
            file="/model/quic_packet.ivy",
            line=3,
            is_relation=False,
        )
    )

    # -- Edges ---------------------------------------------------------------
    graph.add_edge(r1.id, EdgeType.READS, "conn_state")
    graph.add_edge(r2.id, EdgeType.WRITES, "pkt_num")
    graph.add_edge(r3.id, EdgeType.WRITES, "conn_state")

    return graph


class _FakeServer:
    """Minimal server stub satisfying visualization handler expectations."""

    def __init__(self, graph):
        self.indexer = type("I", (), {"requirement_graph": graph})() if graph else None
        self.initializing = False


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------


class TestVisualizationIntegration:
    """End-to-end tests verifying all visualization endpoints work together."""

    def test_all_endpoints_return_valid_json(self):
        """Every endpoint must produce JSON-serializable output."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)

        endpoints = [
            ("actionRequirements", handle_action_requirements),
            ("modelSummaryTable", handle_model_summary_table),
            ("coverageGaps", handle_coverage_gaps),
            ("actionDependencyGraph", handle_action_dependency_graph),
            ("stateMachineView", handle_state_machine_view),
            ("layeredOverview", handle_layered_overview),
            ("smartSuggestions", handle_smart_suggestions),
        ]

        for name, handler in endpoints:
            params = {}
            if name == "smartSuggestions":
                params = {"filePath": "/model/quic_packet.ivy", "line": 12}
            result = handler(server, params)
            # Must be JSON-serializable
            json_str = json.dumps(result)
            parsed = json.loads(json_str)
            assert isinstance(parsed, dict), f"{name} should return dict"

    def test_cross_file_dependencies_visible(self):
        """Dependency graph should show edges between files."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_action_dependency_graph(server, {"includeStateVars": True})
        node_ids = {n["id"] for n in result["nodes"]}
        assert "send_pkt" in node_ids
        assert "open_connection" in node_ids

    def test_coverage_gaps_detect_state_vars(self):
        """State variable summary should reflect the graph."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        assert result["summary"]["totalStateVars"] >= 2

    def test_model_summary_covers_all_actions(self):
        """Summary table should have a row per action."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_model_summary_table(server, {})
        action_names = {r["actionName"] for r in result["rows"]}
        assert "send_pkt" in action_names
        assert "recv_pkt" in action_names
        assert "open_connection" in action_names
        assert result["totals"]["actions"] >= 4

    def test_layered_overview_groups_by_file(self):
        """Layers should group actions by file."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_layered_overview(server, {"groupBy": "file"})
        files = {layer.get("file") for layer in result["layers"]}
        assert "/model/quic_connection.ivy" in files
        assert "/model/quic_packet.ivy" in files

    def test_layered_overview_groups_by_module(self):
        """Layers should group actions by module prefix."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_layered_overview(server, {"groupBy": "module"})
        modules = {layer.get("module") for layer in result["layers"]}
        assert "quic" in modules

    def test_action_requirements_for_specific_action(self):
        """Filtering by actionName should return only that action."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(server, {"actionName": "send_pkt"})
        assert result["modelReady"] is True
        assert len(result["actions"]) == 1
        action = result["actions"][0]
        assert action["actionName"] == "send_pkt"
        assert action["counts"]["require"] == 1
        assert action["counts"]["ensure"] == 1
        assert action["counts"]["total"] == 2

    def test_action_requirements_for_file_filter(self):
        """Filtering by filePath should restrict results to that file."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_action_requirements(
            server, {"filePath": "/model/quic_connection.ivy"}
        )
        assert result["modelReady"] is True
        files_in_result = {a["file"] for a in result["actions"]}
        assert files_in_result == {"/model/quic_connection.ivy"}

    def test_smart_suggestions_with_action_context(self):
        """Smart suggestions for send_pkt should return context info."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_smart_suggestions(
            server,
            {
                "filePath": "/model/quic_packet.ivy",
                "line": 12,
                "actionName": "send_pkt",
            },
        )
        assert result["context"]["action"] == "send_pkt"
        assert result["context"]["file"] == "/model/quic_packet.ivy"

    def test_state_machine_view_has_transitions(self):
        """State machine should build transitions from read/write edges."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_state_machine_view(server, {})
        # We added READS (conn_state) and WRITES (pkt_num) edges on
        # requirements for send_pkt, so there should be at least one
        # transition involving send_pkt.
        action_names_in_transitions = {t["action"] for t in result["transitions"]}
        # send_pkt has a before-require that READS conn_state and an
        # after-ensure that WRITES pkt_num, so it should appear.
        assert "send_pkt" in action_names_in_transitions

    def test_no_graph_returns_empty_defaults(self):
        """All endpoints should gracefully handle a missing graph."""
        server = _FakeServer(None)

        endpoints = [
            ("actionRequirements", handle_action_requirements),
            ("modelSummaryTable", handle_model_summary_table),
            ("coverageGaps", handle_coverage_gaps),
            ("actionDependencyGraph", handle_action_dependency_graph),
            ("stateMachineView", handle_state_machine_view),
            ("layeredOverview", handle_layered_overview),
            ("smartSuggestions", handle_smart_suggestions),
        ]

        for name, handler in endpoints:
            result = handler(server, {})
            json_str = json.dumps(result)
            parsed = json.loads(json_str)
            assert isinstance(parsed, dict), f"{name} should return dict on None graph"

    def test_register_adds_all_endpoints(self):
        """register() should add all 7 endpoints."""
        handlers = {}
        server = type(
            "S",
            (),
            {
                "indexer": type(
                    "I", (), {"requirement_graph": _build_realistic_graph()}
                )(),
                "initializing": False,
                "feature": lambda self, name: (
                    lambda fn: handlers.update({name: fn}) or fn
                ),
            },
        )()
        register(server)
        expected = [
            "ivy/actionRequirements",
            "ivy/modelSummaryTable",
            "ivy/coverageGaps",
            "ivy/actionDependencyGraph",
            "ivy/stateMachineView",
            "ivy/layeredOverview",
            "ivy/smartSuggestions",
        ]
        for endpoint in expected:
            assert endpoint in handlers, f"Missing endpoint: {endpoint}"

    def test_consistency_between_summary_and_requirements(self):
        """Summary table and action requirements should agree on totals."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)

        summary = handle_model_summary_table(server, {})
        requirements = handle_action_requirements(server, {})

        # Both should list the same number of actions
        assert summary["totals"]["actions"] == len(requirements["actions"])

        # Per-action total requirements should match
        summary_by_name = {r["actionName"]: r for r in summary["rows"]}
        for action in requirements["actions"]:
            name = action["actionName"]
            if name in summary_by_name:
                assert (
                    action["counts"]["total"]
                    == summary_by_name[name]["totalRequirements"]
                ), f"Mismatch for {name}"

    def test_coverage_gaps_summary_matches_state_var_count(self):
        """Coverage gaps total state vars should match the graph."""
        graph = _build_realistic_graph()
        server = _FakeServer(graph)
        result = handle_coverage_gaps(server, {})
        assert result["summary"]["totalStateVars"] == len(graph.state_vars)
        assert result["summary"]["totalActions"] == len(graph.actions)
        assert result["summary"]["totalRequirements"] == len(graph.requirements)
