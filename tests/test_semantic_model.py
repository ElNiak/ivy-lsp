"""Tests for the SemanticModel core."""

import threading

from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel
from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode, TypeNode


class TestSemanticModelCRUD:
    def test_add_and_get_node(self):
        model = SemanticModel()
        node = SymbolNode(
            id="test.ivy:5:foo", name="foo", qualified_name="bar.foo",
            kind="action", file="test.ivy", line=5,
        )
        model.add_node(node)
        assert model.get_node("test.ivy:5:foo") is node

    def test_get_nonexistent_node_returns_none(self):
        model = SemanticModel()
        assert model.get_node("nonexistent") is None

    def test_get_nodes_by_type(self):
        model = SemanticModel()
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="f", line=1)
        s2 = SymbolNode(id="s2", name="b", qualified_name="b", kind="relation", file="f", line=2)
        t1 = TypeNode(id="t1", name="cid", qualified_name="cid", file="f", line=3)
        model.add_node(s1)
        model.add_node(s2)
        model.add_node(t1)
        assert len(model.get_nodes_by_type(SymbolNode)) == 2
        assert len(model.get_nodes_by_type(TypeNode)) == 1

    def test_get_nodes_in_file(self):
        model = SemanticModel()
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1)
        s2 = SymbolNode(id="s2", name="b", qualified_name="b", kind="action", file="b.ivy", line=1)
        model.add_node(s1)
        model.add_node(s2)
        assert len(model.get_nodes_in_file("a.ivy")) == 1
        assert len(model.get_nodes_in_file("b.ivy")) == 1
        assert len(model.get_nodes_in_file("c.ivy")) == 0


class TestSemanticModelEdges:
    def test_add_and_query_edges(self):
        model = SemanticModel()
        model.add_node(SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="f", line=1))
        model.add_node(SymbolNode(id="s2", name="b", qualified_name="b", kind="relation", file="f", line=2))
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
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1)
        s2 = SymbolNode(id="s2", name="b", qualified_name="b", kind="action", file="b.ivy", line=1)
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
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1, tier="tier1")
        model.add_node(s1)
        assert model.get_node("s1").name == "a"

        # Update at tier2 - should overwrite tier1
        s1_v2 = SymbolNode(id="s1", name="a_v2", qualified_name="a_v2", kind="action", file="a.ivy", line=1, tier="tier2")
        model.update_file("a.ivy", [s1_v2], [], "tier2")
        assert model.get_node("s1").name == "a_v2"

    def test_update_file_preserves_higher_tier(self):
        model = SemanticModel()
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1, tier="tier2")
        model.update_file("a.ivy", [s1], [], "tier2")

        # Update at tier1 - should NOT overwrite tier2 data
        s1_lower = SymbolNode(id="s1", name="a_lower", qualified_name="a_lower", kind="action", file="a.ivy", line=1, tier="tier1")
        model.update_file("a.ivy", [s1_lower], [], "tier1")
        # tier2 node should still be there (tier1 can't overwrite tier2)
        assert model.get_node("s1").name == "a"

    def test_update_file_with_edges(self):
        model = SemanticModel()
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1)
        edges = [("s1", SemanticEdgeType.READS, "ext")]
        model.update_file("a.ivy", [s1], edges, "tier1")
        assert model.edge_count() == 1
        assert model.get_outgoing("s1")[0] == (SemanticEdgeType.READS, "ext")


class TestSemanticModelCounts:
    def test_node_and_edge_counts(self):
        model = SemanticModel()
        assert model.node_count() == 0
        assert model.edge_count() == 0
        model.add_node(SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="f", line=1))
        assert model.node_count() == 1
        model.add_edge("s1", SemanticEdgeType.READS, "ext")
        assert model.edge_count() == 1


class TestSemanticModelUpdateFileEdgeCleanup:
    def test_update_file_removes_old_edges(self):
        model = SemanticModel()
        s1 = SymbolNode(id="s1", name="a", qualified_name="a", kind="action", file="a.ivy", line=1)
        s2 = SymbolNode(id="s2", name="b", qualified_name="b", kind="relation", file="a.ivy", line=2)
        edges = [("s1", SemanticEdgeType.READS, "s2")]
        model.update_file("a.ivy", [s1, s2], edges, "tier1")
        assert model.edge_count() == 1
        assert model.get_outgoing("s1") == [(SemanticEdgeType.READS, "s2")]

        # Update with different nodes/edges - old edges should be removed
        s3 = SymbolNode(id="s3", name="c", qualified_name="c", kind="action", file="a.ivy", line=3)
        new_edges = [("s3", SemanticEdgeType.WRITES, "ext")]
        model.update_file("a.ivy", [s3], new_edges, "tier2")

        # Old s1->s2 READS edge should be gone
        assert model.get_outgoing("s1") == []
        # New s3->ext WRITES edge should be present
        outgoing = model.get_outgoing("s3")
        assert len(outgoing) == 1
        assert outgoing[0] == (SemanticEdgeType.WRITES, "ext")


class TestSemanticModelThreadSafety:
    def test_concurrent_reads_during_update(self):
        model = SemanticModel()
        for i in range(100):
            model.add_node(SymbolNode(
                id=f"s{i}", name=f"sym{i}", qualified_name=f"sym{i}",
                kind="action", file="f.ivy", line=i,
            ))

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
                    model.add_node(SymbolNode(
                        id=f"s{i}", name=f"sym{i}", qualified_name=f"sym{i}",
                        kind="action", file="f.ivy", line=i,
                    ))
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
                    nodes = [SymbolNode(
                        id=f"n{file_suffix}_{i}", name=f"sym{i}",
                        qualified_name=f"sym{i}", kind="action",
                        file=filepath, line=i,
                    )]
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
