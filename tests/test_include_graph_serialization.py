"""Tests for IncludeGraph serialization (to_edges / from_edges)."""

from ivy_lsp.core.parsing.symbols import IncludeGraph


class TestIncludeGraphSerialization:
    def test_to_edges_simple(self):
        graph = IncludeGraph()
        graph.add_edge("a.ivy", "b.ivy")
        graph.add_edge("a.ivy", "c.ivy")
        edges = graph.to_edges()
        assert edges == {"a.ivy": ["b.ivy", "c.ivy"]}

    def test_to_edges_empty(self):
        graph = IncludeGraph()
        assert graph.to_edges() == {}

    def test_to_edges_values_sorted(self):
        graph = IncludeGraph()
        graph.add_edge("x.ivy", "z.ivy")
        graph.add_edge("x.ivy", "a.ivy")
        graph.add_edge("x.ivy", "m.ivy")
        edges = graph.to_edges()
        assert edges["x.ivy"] == ["a.ivy", "m.ivy", "z.ivy"]

    def test_from_edges_simple(self):
        data = {"a.ivy": ["b.ivy", "c.ivy"]}
        graph = IncludeGraph.from_edges(data)
        assert graph.get_includes("a.ivy") == {"b.ivy", "c.ivy"}
        assert graph.get_included_by("b.ivy") == {"a.ivy"}
        assert graph.get_included_by("c.ivy") == {"a.ivy"}

    def test_from_edges_empty(self):
        graph = IncludeGraph.from_edges({})
        assert graph.to_edges() == {}

    def test_roundtrip(self):
        graph = IncludeGraph()
        graph.add_edge("a.ivy", "b.ivy")
        graph.add_edge("a.ivy", "c.ivy")
        graph.add_edge("b.ivy", "d.ivy")

        restored = IncludeGraph.from_edges(graph.to_edges())

        assert restored.get_includes("a.ivy") == graph.get_includes("a.ivy")
        assert restored.get_includes("b.ivy") == graph.get_includes("b.ivy")
        assert restored.get_included_by("d.ivy") == graph.get_included_by("d.ivy")

    def test_roundtrip_preserves_transitive(self):
        graph = IncludeGraph()
        graph.add_edge("a.ivy", "b.ivy")
        graph.add_edge("b.ivy", "c.ivy")
        graph.add_edge("c.ivy", "d.ivy")

        restored = IncludeGraph.from_edges(graph.to_edges())
        assert restored.get_transitive_includes("a.ivy") == {"b.ivy", "c.ivy", "d.ivy"}

    def test_roundtrip_multiple_roots(self):
        graph = IncludeGraph()
        graph.add_edge("test1.ivy", "shared.ivy")
        graph.add_edge("test2.ivy", "shared.ivy")
        graph.add_edge("shared.ivy", "types.ivy")

        restored = IncludeGraph.from_edges(graph.to_edges())

        assert restored.get_includes("test1.ivy") == {"shared.ivy"}
        assert restored.get_includes("test2.ivy") == {"shared.ivy"}
        assert restored.get_included_by("shared.ivy") == {"test1.ivy", "test2.ivy"}
        assert restored.get_transitive_includes("test1.ivy") == {
            "shared.ivy",
            "types.ivy",
        }
