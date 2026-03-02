"""Tests for coverage hint diagnostics and pattern code actions."""

import sys
from pathlib import Path

import pytest  # noqa: F401

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import (  # noqa: E402
    ActionNode,
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.features.coverage_hints import compute_coverage_hints  # noqa: E402


def _build_hint_graph():
    graph = RequirementGraph()
    # Requirements FIRST (add_file_requirements calls remove_file internally)
    r1 = RequirementNode(
        id="/test/quic.ivy:12", kind="ensure",
        formula_text="pkt_count(C) = old + 1",
        line=12, col=0, file="/test/quic.ivy",
        monitor_action="send_pkt", mixin_kind="after",
    )
    graph.add_file_requirements("/test/quic.ivy", [r1])
    graph.add_action(ActionNode(
        id="send_pkt", name="send_pkt", qualified_name="quic.send_pkt",
        file="/test/quic.ivy", line=10,
    ))
    # Action with no monitors
    graph.add_action(ActionNode(
        id="idle", name="idle", qualified_name="quic.idle",
        file="/test/quic.ivy", line=30,
    ))
    # State var written but not guarded
    graph.add_state_var(StateVarNode(
        id="pkt_count", name="pkt_count", qualified_name="quic.pkt_count",
        file="/test/quic.ivy", line=4, is_relation=False,
    ))
    graph.add_edge(r1.id, EdgeType.WRITES, "pkt_count")
    return graph


class TestComputeCoverageHints:
    def test_returns_empty_with_no_graph(self):
        result = compute_coverage_hints(None, "/test/quic.ivy")
        assert result == []

    def test_detects_action_with_no_monitors(self):
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        messages = [h["message"] for h in result]
        assert any("idle" in m and "no monitor" in m.lower() for m in messages)

    def test_detects_unguarded_state_var(self):
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        messages = [h["message"] for h in result]
        assert any("pkt_count" in m and "written" in m.lower() for m in messages)

    def test_hints_have_required_fields(self):
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        for hint in result:
            assert "line" in hint
            assert "message" in hint
            assert "severity" in hint

    def test_no_false_positive_for_monitored_action(self):
        """send_pkt has a requirement, so it should not appear as unmonitored."""
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        messages = [h["message"] for h in result]
        assert not any(
            "send_pkt" in m and "no monitor" in m.lower() for m in messages
        )

    def test_hint_codes_present(self):
        """Each hint must carry a diagnostic code string."""
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        for hint in result:
            assert "code" in hint
            assert hint["code"].startswith("ivy.")

    def test_filters_by_file(self):
        """Hints for a file that has no nodes should be empty."""
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/other/file.ivy")
        assert result == []

    def test_template_snippet_present_for_no_monitor(self):
        """Unmonitored-action hints should include a template snippet."""
        graph = _build_hint_graph()
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        no_monitor_hints = [
            h for h in result if h.get("code") == "ivy.no-monitor"
        ]
        assert len(no_monitor_hints) > 0
        for hint in no_monitor_hints:
            assert "template" in hint
            assert "after" in hint["template"] or "before" in hint["template"]

    def test_guarded_var_not_flagged(self):
        """A state var that is read by a requirement should not be flagged."""
        graph = _build_hint_graph()
        # Add a READS edge so pkt_count becomes guarded
        graph.add_edge("/test/quic.ivy:12", EdgeType.READS, "pkt_count")
        result = compute_coverage_hints(graph, "/test/quic.ivy")
        messages = [h["message"] for h in result]
        assert not any(
            "pkt_count" in m and "written" in m.lower() for m in messages
        )
