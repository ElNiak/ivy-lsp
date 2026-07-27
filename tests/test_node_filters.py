"""Tests for ivy_lsp.core.semantic.node_filters."""

from ivy_lsp.core.semantic.node_filters import first_node_of_type, nodes_of_type


class _A:
    pass


class _B:
    pass


class TestNodesOfType:
    def test_filters_by_type(self):
        items = [_A(), _B(), _A()]
        result = nodes_of_type(items, _A)
        assert len(result) == 2
        assert all(isinstance(r, _A) for r in result)

    def test_empty_input(self):
        assert nodes_of_type([], _A) == []

    def test_no_matches(self):
        assert nodes_of_type([_B(), _B()], _A) == []


class TestFirstNodeOfType:
    def test_returns_first(self):
        a1, a2 = _A(), _A()
        result = first_node_of_type([_B(), a1, a2], _A)
        assert result is a1

    def test_returns_none_when_no_match(self):
        assert first_node_of_type([_B()], _A) is None

    def test_empty_input(self):
        assert first_node_of_type([], _A) is None
