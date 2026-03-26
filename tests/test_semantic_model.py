"""Tests for the SemanticModel core."""

import threading

from ivy_lsp.core.semantic.edges import SemanticEdgeType
from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import (
    RfcAnnotation,
    RfcRequirement,
    SymbolNode,
    TypeNode,
)


class TestSemanticModelCRUD:
    def test_add_and_get_node(self):
        model = SemanticModel()
        node = SymbolNode(
            id="test.ivy:5:foo",
            name="foo",
            qualified_name="bar.foo",
            kind="action",
            file="test.ivy",
            line=5,
        )
        model.add_node(node)
        assert model.get_node("test.ivy:5:foo") is node

    def test_get_nonexistent_node_returns_none(self):
        model = SemanticModel()
        assert model.get_node("nonexistent") is None

    def test_get_nodes_by_type(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="f", line=1
        )
        s2 = SymbolNode(
            id="s2", name="b", qualified_name="b", kind="relation", file="f", line=2
        )
        t1 = TypeNode(id="t1", name="cid", qualified_name="cid", file="f", line=3)
        model.add_node(s1)
        model.add_node(s2)
        model.add_node(t1)
        assert len(model.get_nodes_by_type(SymbolNode)) == 2
        assert len(model.get_nodes_by_type(TypeNode)) == 1

    def test_get_nodes_in_file(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1
        )
        s2 = SymbolNode(
            id="s2", name="b", qualified_name="b", kind="action", file="b.ivy", line=1
        )
        model.add_node(s1)
        model.add_node(s2)
        assert len(model.get_nodes_in_file("a.ivy")) == 1
        assert len(model.get_nodes_in_file("b.ivy")) == 1
        assert len(model.get_nodes_in_file("c.ivy")) == 0


class TestSemanticModelEdges:
    def test_add_and_query_edges(self):
        model = SemanticModel()
        model.add_node(
            SymbolNode(
                id="s1", name="a", qualified_name="a", kind="action", file="f", line=1
            )
        )
        model.add_node(
            SymbolNode(
                id="s2", name="b", qualified_name="b", kind="relation", file="f", line=2
            )
        )
        model.add_edge("s1", SemanticEdgeType.READS, "s2")

        outgoing = model.get_outgoing("s1")
        assert len(outgoing) == 1
        assert outgoing[0] == (SemanticEdgeType.READS, "s2")

        incoming = model.get_incoming("s2")
        assert len(incoming) == 1
        assert incoming[0] == (SemanticEdgeType.READS, "s1")

    def test_filter_edges_by_type(self):
        model = SemanticModel()
        model.add_edge("a", SemanticEdgeType.READS, "b")
        model.add_edge("a", SemanticEdgeType.WRITES, "c")

        reads = model.get_outgoing("a", SemanticEdgeType.READS)
        assert len(reads) == 1
        writes = model.get_outgoing("a", SemanticEdgeType.WRITES)
        assert len(writes) == 1


class TestSemanticModelFileOps:
    def test_remove_file(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1
        )
        s2 = SymbolNode(
            id="s2", name="b", qualified_name="b", kind="action", file="b.ivy", line=1
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_edge("s1", SemanticEdgeType.READS, "s2")

        model.remove_file("a.ivy")
        assert model.get_node("s1") is None
        assert model.get_node("s2") is not None
        assert model.node_count() == 1
        assert model.edge_count() == 0

    def test_update_file_replaces_at_tier(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="a",
            qualified_name="a",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier1",
        )
        model.add_node(s1)
        assert model.get_node("s1").name == "a"

        # Update at tier2 - should overwrite tier1
        s1_v2 = SymbolNode(
            id="s1",
            name="a_v2",
            qualified_name="a_v2",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier2",
        )
        model.update_file("a.ivy", [s1_v2], [], "tier2")
        assert model.get_node("s1").name == "a_v2"

    def test_update_file_preserves_higher_tier(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="a",
            qualified_name="a",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier2",
        )
        model.update_file("a.ivy", [s1], [], "tier2")

        # Update at tier1 - should NOT overwrite tier2 data
        s1_lower = SymbolNode(
            id="s1",
            name="a_lower",
            qualified_name="a_lower",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier1",
        )
        model.update_file("a.ivy", [s1_lower], [], "tier1")
        # tier2 node should still be there (tier1 can't overwrite tier2)
        assert model.get_node("s1").name == "a"

    def test_update_file_with_edges(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1
        )
        edges = [("s1", SemanticEdgeType.READS, "ext")]
        model.update_file("a.ivy", [s1], edges, "tier1")
        assert model.edge_count() == 1
        assert model.get_outgoing("s1")[0] == (SemanticEdgeType.READS, "ext")


class TestSemanticModelCounts:
    def test_node_and_edge_counts(self):
        model = SemanticModel()
        assert model.node_count() == 0
        assert model.edge_count() == 0
        model.add_node(
            SymbolNode(
                id="s1", name="a", qualified_name="a", kind="action", file="f", line=1
            )
        )
        assert model.node_count() == 1
        model.add_edge("s1", SemanticEdgeType.READS, "ext")
        assert model.edge_count() == 1


class TestSemanticModelUpdateFileEdgeCleanup:
    def test_update_file_removes_old_edges(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1
        )
        s2 = SymbolNode(
            id="s2", name="b", qualified_name="b", kind="relation", file="a.ivy", line=2
        )
        edges = [("s1", SemanticEdgeType.READS, "s2")]
        model.update_file("a.ivy", [s1, s2], edges, "tier1")
        assert model.edge_count() == 1
        assert model.get_outgoing("s1") == [(SemanticEdgeType.READS, "s2")]

        # Update with different nodes/edges - old edges should be removed
        s3 = SymbolNode(
            id="s3", name="c", qualified_name="c", kind="action", file="a.ivy", line=3
        )
        new_edges = [("s3", SemanticEdgeType.WRITES, "ext")]
        model.update_file("a.ivy", [s3], new_edges, "tier2")

        # Old s1->s2 READS edge should be gone
        assert model.get_outgoing("s1") == []
        # New s3->ext WRITES edge should be present
        outgoing = model.get_outgoing("s3")
        assert len(outgoing) == 1
        assert outgoing[0] == (SemanticEdgeType.WRITES, "ext")


class TestIncrementalAdjacency:
    """Adjacency indices must stay correct after incremental updates."""

    def test_remove_file_preserves_unrelated_edges(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1
        )
        s2 = SymbolNode(
            id="s2", name="b", qualified_name="b", kind="action", file="b.ivy", line=1
        )
        s3 = SymbolNode(
            id="s3", name="c", qualified_name="c", kind="action", file="b.ivy", line=2
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_node(s3)
        model.add_edge("s1", SemanticEdgeType.READS, "s2")
        model.add_edge("s2", SemanticEdgeType.WRITES, "s3")

        model.remove_file("a.ivy")

        assert model.edge_count() == 1
        assert len(model.get_outgoing("s2")) == 1
        assert len(model.get_incoming("s2")) == 0

    def test_update_file_replaces_edges_correctly(self):
        model = SemanticModel()
        n1 = SymbolNode(
            id="n1",
            name="x",
            qualified_name="x",
            kind="action",
            file="f.ivy",
            line=1,
            tier="tier1",
        )
        n2 = SymbolNode(
            id="n2",
            name="y",
            qualified_name="y",
            kind="action",
            file="f.ivy",
            line=2,
            tier="tier1",
        )
        model.update_file(
            "f.ivy", [n1, n2], [("n1", SemanticEdgeType.READS, "n2")], "tier1"
        )

        assert model.edge_count() == 1
        assert len(model.get_outgoing("n1")) == 1

        n1b = SymbolNode(
            id="n1",
            name="x",
            qualified_name="x",
            kind="action",
            file="f.ivy",
            line=1,
            tier="tier2",
        )
        n3 = SymbolNode(
            id="n3",
            name="z",
            qualified_name="z",
            kind="action",
            file="f.ivy",
            line=3,
            tier="tier2",
        )
        model.update_file(
            "f.ivy", [n1b, n3], [("n1", SemanticEdgeType.WRITES, "n3")], "tier2"
        )

        assert len(model.get_outgoing("n1", SemanticEdgeType.READS)) == 0
        assert len(model.get_outgoing("n1", SemanticEdgeType.WRITES)) == 1

    def test_cross_file_edges_preserved_on_single_file_update(self):
        model = SemanticModel()
        a = SymbolNode(
            id="a",
            name="a",
            qualified_name="a",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier1",
        )
        b = SymbolNode(
            id="b",
            name="b",
            qualified_name="b",
            kind="action",
            file="b.ivy",
            line=1,
            tier="tier1",
        )
        model.update_file("a.ivy", [a], [], "tier1")
        model.update_file("b.ivy", [b], [], "tier1")
        model.add_edge("a", SemanticEdgeType.HAS_PARAM, "b")

        a2 = SymbolNode(
            id="a",
            name="a",
            qualified_name="a",
            kind="action",
            file="a.ivy",
            line=1,
            tier="tier2",
        )
        model.update_file("a.ivy", [a2], [("a", SemanticEdgeType.READS, "b")], "tier2")

        assert len(model.get_outgoing("a", SemanticEdgeType.HAS_PARAM)) == 0
        assert len(model.get_outgoing("a", SemanticEdgeType.READS)) == 1


class TestSemanticModelThreadSafety:
    def test_concurrent_reads_during_update(self):
        model = SemanticModel()
        for i in range(100):
            model.add_node(
                SymbolNode(
                    id=f"s{i}",
                    name=f"sym{i}",
                    qualified_name=f"sym{i}",
                    kind="action",
                    file="f.ivy",
                    line=i,
                )
            )

        errors = []

        def reader():
            try:
                for _ in range(50):
                    model.get_nodes_by_type(SymbolNode)
                    model.get_nodes_in_file("f.ivy")
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(100, 150):
                    model.add_node(
                        SymbolNode(
                            id=f"s{i}",
                            name=f"sym{i}",
                            qualified_name=f"sym{i}",
                            kind="action",
                            file="f.ivy",
                            line=i,
                        )
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"

    def test_concurrent_update_file(self):
        model = SemanticModel()
        errors = []

        def updater(file_suffix):
            try:
                filepath = f"file_{file_suffix}.ivy"
                for i in range(20):
                    nodes = [
                        SymbolNode(
                            id=f"n{file_suffix}_{i}",
                            name=f"sym{i}",
                            qualified_name=f"sym{i}",
                            kind="action",
                            file=filepath,
                            line=i,
                        )
                    ]
                    edges = [
                        (f"n{file_suffix}_{i}", SemanticEdgeType.READS, f"ext_{i}")
                    ]
                    model.update_file(filepath, nodes, edges, "tier1")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=updater, args=(j,)) for j in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent update_file errors: {errors}"
        # All 4 files should have exactly 1 node each (last update wins)
        for j in range(4):
            nodes = model.get_nodes_in_file(f"file_{j}.ivy")
            assert len(nodes) == 1


class TestSemanticModelDomainQueries:
    """Tests for RequirementGraph-compatible domain query methods."""

    def test_get_requirements_for_action(self):
        from ivy_lsp.core.analysis.requirement_graph import ActionNode, RequirementNode

        model = SemanticModel()
        action = ActionNode(
            id="act:open",
            name="open",
            qualified_name="conn.open",
            file="/t.ivy",
            line=10,
        )
        req = RequirementNode(
            id="/t.ivy:15",
            kind="require",
            formula_text="conn_seen(C)",
            line=15,
            col=0,
            file="/t.ivy",
            monitor_action="conn.open",
            mixin_kind="before",
            bracket_tags=[],
        )
        model.add_node(action)
        model.add_node(req)
        model.add_edge(req.id, SemanticEdgeType.CONSTRAINS, action.id)

        result = model.get_requirements_for_action("act:open")
        assert len(result) == 1
        assert result[0].id == "/t.ivy:15"

    def test_get_requirements_for_action_empty(self):
        model = SemanticModel()
        s = SymbolNode(
            id="s1", name="a", qualified_name="a", kind="action", file="f", line=1
        )
        model.add_node(s)
        assert model.get_requirements_for_action("s1") == []

    def test_get_state_vars_read_by(self):
        from ivy_lsp.core.analysis.requirement_graph import (
            RequirementNode,
            StateVarNode,
        )

        model = SemanticModel()
        req = RequirementNode(
            id="/t.ivy:15",
            kind="require",
            formula_text="conn_seen(C)",
            line=15,
            col=0,
            file="/t.ivy",
            monitor_action="conn.open",
            mixin_kind="before",
            bracket_tags=[],
        )
        sv = StateVarNode(
            id="conn_seen",
            name="conn_seen",
            qualified_name="conn_seen",
            file="/t.ivy",
            line=5,
            is_relation=True,
        )
        model.add_node(req)
        model.add_node(sv)
        model.add_edge(req.id, SemanticEdgeType.READS, sv.id)

        result = model.get_state_vars_read_by("/t.ivy:15")
        assert len(result) == 1
        assert result[0].id == "conn_seen"

    def test_get_coverage_stats_empty(self):
        model = SemanticModel()
        stats = model.get_coverage_stats()
        assert stats["total_requirements"] == 0
        assert stats["covered"] == 0
        assert stats["uncovered"] == 0


class TestNodesByNameIndex:
    """Tests for the _nodes_by_name O(1) lookup index."""

    def _make_node(self, node_id, name, file=None, tier=None):
        """Create a minimal node with required attributes."""
        from types import SimpleNamespace

        return SimpleNamespace(id=node_id, name=name, file=file, tier=tier)

    def test_get_nodes_by_name_returns_matching(self):
        model = SemanticModel()
        n1 = self._make_node("n1", "send", file="a.ivy")
        n2 = self._make_node("n2", "recv", file="a.ivy")
        n3 = self._make_node("n3", "send", file="b.ivy")
        model.add_node(n1)
        model.add_node(n2)
        model.add_node(n3)
        result = model.get_nodes_by_name("send")
        assert len(result) == 2
        assert {r.id for r in result} == {"n1", "n3"}

    def test_get_nodes_by_name_empty_for_missing(self):
        model = SemanticModel()
        assert model.get_nodes_by_name("nonexistent") == []

    def test_remove_file_cleans_name_index(self):
        model = SemanticModel()
        n1 = self._make_node("n1", "send", file="a.ivy")
        n2 = self._make_node("n2", "send", file="b.ivy")
        model.add_node(n1)
        model.add_node(n2)
        model.remove_file("a.ivy")
        result = model.get_nodes_by_name("send")
        assert len(result) == 1
        assert result[0].id == "n2"

    def test_add_node_replace_updates_name_index(self):
        """Replacing a node with a different name must clean old name entry."""
        model = SemanticModel()
        n1 = self._make_node("n1", "old_name", file="a.ivy")
        model.add_node(n1)
        assert len(model.get_nodes_by_name("old_name")) == 1

        n1_updated = self._make_node("n1", "new_name", file="a.ivy")
        model.add_node(n1_updated)
        assert model.get_nodes_by_name("old_name") == []
        assert len(model.get_nodes_by_name("new_name")) == 1

    def test_pickle_backward_compat_rebuilds_name_index(self):
        """Old pickled models without _nodes_by_name should rebuild on load."""
        import pickle

        model = SemanticModel()
        n1 = self._make_node("n1", "action_send", file="a.ivy")
        model.add_node(n1)

        # Simulate old pickle: remove _nodes_by_name before serializing
        state = model.__getstate__()
        state.pop("_nodes_by_name", None)
        old_model = SemanticModel.__new__(SemanticModel)
        old_model.__setstate__(state)

        # Should still work after rebuild
        result = old_model.get_nodes_by_name("action_send")
        assert len(result) == 1

    def test_update_file_maintains_name_index(self):
        """update_file should keep name index consistent."""
        model = SemanticModel()
        n1 = self._make_node("n1", "send", file="a.ivy", tier="tier1")
        model.update_file("a.ivy", [n1], [], "tier1")
        assert len(model.get_nodes_by_name("send")) == 1

        # Replace at tier2
        n1_v2 = self._make_node("n1", "send_v2", file="a.ivy", tier="tier2")
        model.update_file("a.ivy", [n1_v2], [], "tier2")
        assert model.get_nodes_by_name("send") == []
        assert len(model.get_nodes_by_name("send_v2")) == 1

    def test_merge_from_populates_name_index(self):
        """merge_from should index names from the other model."""
        m1 = SemanticModel()
        m2 = SemanticModel()
        n1 = self._make_node("n1", "action_a", file="a.ivy")
        m2.add_node(n1)

        m1.merge_from(m2)
        assert len(m1.get_nodes_by_name("action_a")) == 1


class TestSemanticModelReferenceEdges:
    """Test CALLS, USES, MONITORS, and CONTAINS edge wiring."""

    def test_calls_edge_from_references(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="process",
            qualified_name="process",
            kind="action",
            file="f",
            line=1,
        )
        s2 = SymbolNode(
            id="s2",
            name="connect",
            qualified_name="connect",
            kind="action",
            file="f",
            line=5,
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_edge("s1", SemanticEdgeType.CALLS, "s2")
        outgoing = model.get_outgoing("s1")
        assert (SemanticEdgeType.CALLS, "s2") in outgoing

    def test_uses_edge_from_references(self):
        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="net",
            qualified_name="net",
            kind="instance",
            file="f",
            line=1,
        )
        s2 = SymbolNode(
            id="s2",
            name="endpoint",
            qualified_name="endpoint",
            kind="module",
            file="f",
            line=5,
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_edge("s1", SemanticEdgeType.USES, "s2")
        outgoing = model.get_outgoing("s1")
        assert (SemanticEdgeType.USES, "s2") in outgoing

    def test_contains_edge_from_qualified_names(self):
        model = SemanticModel()
        parent = SymbolNode(
            id="p1",
            name="frame",
            qualified_name="frame",
            kind="module",
            file="f",
            line=1,
        )
        child = SymbolNode(
            id="c1",
            name="ack",
            qualified_name="frame.ack",
            kind="action",
            file="f",
            line=5,
        )
        model.add_node(parent)
        model.add_node(child)
        model.add_edge("p1", SemanticEdgeType.CONTAINS, "c1")
        outgoing = model.get_outgoing("p1")
        assert (SemanticEdgeType.CONTAINS, "c1") in outgoing
