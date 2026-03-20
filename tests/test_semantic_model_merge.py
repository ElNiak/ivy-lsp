"""Tests for SemanticModel.merge_from() — merging per-protocol models."""

import pytest

from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel


class _FakeNode:
    """Minimal node for testing."""

    def __init__(self, id: str, name: str, file: str = "", tier: str = "tier1"):
        self.id = id
        self.name = name
        self.file = file
        self.tier = tier


def test_merge_from_adds_nodes():
    """Nodes from source model appear in target after merge."""
    target = SemanticModel()
    source = SemanticModel()
    source.add_node(_FakeNode("a", "alpha", file="/proto1/a.ivy"))
    source.add_node(_FakeNode("b", "beta", file="/proto1/b.ivy"))

    target.merge_from(source)

    assert target.get_node("a") is not None
    assert target.get_node("b") is not None
    assert target.node_count() == 2


def test_merge_from_adds_edges():
    """Edges from source model appear in target after merge."""
    target = SemanticModel()
    source = SemanticModel()
    source.add_node(_FakeNode("a", "alpha"))
    source.add_node(_FakeNode("b", "beta"))
    source.add_edge("a", SemanticEdgeType.INCLUDES, "b")

    target.merge_from(source)

    assert target.edge_count() == 1
    outgoing = target.get_outgoing("a")
    assert len(outgoing) == 1
    assert outgoing[0] == (SemanticEdgeType.INCLUDES, "b")


def test_merge_from_two_protocols():
    """Merging two disjoint protocol models produces their union."""
    quic_model = SemanticModel()
    quic_model.add_node(_FakeNode("q1", "quic_conn", file="/quic/conn.ivy"))
    quic_model.add_node(_FakeNode("q2", "quic_frame", file="/quic/frame.ivy"))
    quic_model.add_edge("q1", SemanticEdgeType.INCLUDES, "q2")

    tls_model = SemanticModel()
    tls_model.add_node(_FakeNode("t1", "tls_handshake", file="/tls/hs.ivy"))

    merged = SemanticModel()
    merged.merge_from(quic_model)
    merged.merge_from(tls_model)

    assert merged.node_count() == 3
    assert merged.edge_count() == 1
    assert merged.get_node("q1") is not None
    assert merged.get_node("t1") is not None


def test_merge_from_idempotent():
    """Merging the same model twice doesn't duplicate data."""
    source = SemanticModel()
    source.add_node(_FakeNode("x", "ex"))
    source.add_edge("x", SemanticEdgeType.COVERS, "x")

    target = SemanticModel()
    target.merge_from(source)
    target.merge_from(source)

    assert target.node_count() == 1
    assert target.edge_count() == 1


def test_merge_from_preserves_existing():
    """Pre-existing nodes in target are preserved after merge."""
    target = SemanticModel()
    target.add_node(_FakeNode("existing", "already_here"))

    source = SemanticModel()
    source.add_node(_FakeNode("new", "newcomer"))

    target.merge_from(source)

    assert target.node_count() == 2
    assert target.get_node("existing") is not None
    assert target.get_node("new") is not None
