"""Tests for context-aware completion enhancements."""

import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.requirement_graph import (
    ActionNode,
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.lsp.completion import compute_semantic_completions


def _build_completion_graph():
    graph = RequirementGraph()
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
    graph.add_edge(r1.id, EdgeType.READS, "conn_state")
    return graph


class TestComputeSemanticCompletions:
    def test_returns_empty_with_no_graph(self):
        result = compute_semantic_completions(None, "/test/quic.ivy", 12, "before")
        assert result == []

    def test_suggests_state_vars_in_before_block(self):
        graph = _build_completion_graph()
        result = compute_semantic_completions(graph, "/test/quic.ivy", 12, "before")
        names = [c["label"] for c in result]
        # Should suggest state vars relevant to the action's context
        assert any("conn_state" in n or "pkt_num" in n for n in names)

    def test_returns_list_of_completion_dicts(self):
        graph = _build_completion_graph()
        result = compute_semantic_completions(graph, "/test/quic.ivy", 12, "before")
        for item in result:
            assert "label" in item
            assert "detail" in item
            assert "kind" in item

    def test_high_relevance_state_vars_sorted_first(self):
        graph = _build_completion_graph()
        result = compute_semantic_completions(graph, "/test/quic.ivy", 12, "before")
        # conn_state has a READS edge (high relevance), pkt_num does not (low)
        labels = [c["label"] for c in result]
        assert "conn_state" in labels
        assert "pkt_num" in labels
        # conn_state should come before pkt_num due to sortText priority
        sort_texts = [c["sortText"] for c in result]
        conn_idx = labels.index("conn_state")
        pkt_idx = labels.index("pkt_num")
        assert sort_texts[conn_idx] < sort_texts[pkt_idx]

    def test_after_block_suggests_written_vars(self):
        graph = _build_completion_graph()
        # Add a WRITES edge so the after-block path has data
        graph.add_edge("write:pkt_num", EdgeType.WRITES, "pkt_num")
        result = compute_semantic_completions(graph, "/test/quic.ivy", 12, "after")
        names = [c["label"] for c in result]
        assert "pkt_num" in names

    def test_no_action_returns_empty(self):
        graph = _build_completion_graph()
        # Line far from any requirement, no enclosing action found
        result = compute_semantic_completions(graph, "/test/quic.ivy", 500, "before")
        assert result == []

    def test_unknown_file_returns_empty(self):
        graph = _build_completion_graph()
        result = compute_semantic_completions(
            graph, "/nonexistent/file.ivy", 12, "before"
        )
        assert result == []

    def test_body_block_same_as_before(self):
        graph = _build_completion_graph()
        result = compute_semantic_completions(graph, "/test/quic.ivy", 12, "body")
        names = [c["label"] for c in result]
        assert any("conn_state" in n or "pkt_num" in n for n in names)
