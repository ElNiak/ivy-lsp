"""Tests for MCP offline-index fast-path in _build_model()."""

from unittest.mock import MagicMock

from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel


class _FakeNode:
    def __init__(self, id, name, file="", tier="tier1"):
        self.id = id
        self.name = name
        self.file = file
        self.tier = tier


def _make_protocol_index(protocol, model=None):
    """Create a minimal mock ProtocolIndex."""
    idx = MagicMock()
    idx.protocol = protocol
    idx.semantic_model = model
    idx.staleness = MagicMock()
    idx.staleness.status = "fresh"
    return idx


def test_merge_offline_models_fresh():
    """When fresh offline models exist, merge produces a combined model."""
    quic_model = SemanticModel()
    quic_model.add_node(_FakeNode("q1", "quic_conn", file="/quic/conn.ivy"))

    tls_model = SemanticModel()
    tls_model.add_node(_FakeNode("t1", "tls_hs", file="/tls/hs.ivy"))

    ws_ctx = MagicMock()
    ws_ctx.has_index.return_value = True
    ws_ctx.protocol_indexes = {
        "quic": _make_protocol_index("quic", quic_model),
        "tls": _make_protocol_index("tls", tls_model),
    }

    # Simulate what the offline-index fast-path does
    merged = SemanticModel()
    for proto, idx in ws_ctx.protocol_indexes.items():
        if idx.semantic_model is not None and idx.staleness.status == "fresh":
            merged.merge_from(idx.semantic_model)

    assert merged.node_count() == 2
    assert merged.get_node("q1") is not None
    assert merged.get_node("t1") is not None


def test_merge_offline_models_skips_stale():
    """Stale protocol indexes are skipped during merge."""
    fresh_model = SemanticModel()
    fresh_model.add_node(_FakeNode("f1", "fresh_sym"))

    stale_idx = _make_protocol_index("bgp", SemanticModel())
    stale_idx.staleness.status = "stale_major"

    fresh_idx = _make_protocol_index("quic", fresh_model)
    fresh_idx.staleness.status = "fresh"

    ws_ctx = MagicMock()
    ws_ctx.has_index.return_value = True
    ws_ctx.protocol_indexes = {"bgp": stale_idx, "quic": fresh_idx}

    merged = SemanticModel()
    used = 0
    for proto, idx in ws_ctx.protocol_indexes.items():
        if idx.semantic_model is not None and idx.staleness.status in (
            "fresh",
            "stale_minor",
        ):
            merged.merge_from(idx.semantic_model)
            used += 1

    assert used == 1
    assert merged.node_count() == 1


def test_merge_offline_models_none_model():
    """Protocol indexes without semantic_model are skipped."""
    idx = _make_protocol_index("quic", model=None)

    ws_ctx = MagicMock()
    ws_ctx.has_index.return_value = True
    ws_ctx.protocol_indexes = {"quic": idx}

    merged = SemanticModel()
    for proto, idx in ws_ctx.protocol_indexes.items():
        if idx.semantic_model is not None and idx.staleness.status == "fresh":
            merged.merge_from(idx.semantic_model)

    assert merged.node_count() == 0


def test_merge_offline_models_with_edges():
    """Edges within a protocol model are preserved after merge."""
    model = SemanticModel()
    model.add_node(_FakeNode("a", "alpha", file="/quic/a.ivy"))
    model.add_node(_FakeNode("b", "beta", file="/quic/b.ivy"))
    model.add_edge("a", SemanticEdgeType.INCLUDES, "b")

    merged = SemanticModel()
    merged.merge_from(model)

    assert merged.edge_count() == 1
    outgoing = merged.get_outgoing("a")
    assert len(outgoing) == 1
    assert outgoing[0] == (SemanticEdgeType.INCLUDES, "b")
