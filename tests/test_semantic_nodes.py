"""Tests for semantic model node and edge types."""

import pytest

from ivy_lsp.semantic.edges import EdgeType, SemanticEdgeType
from ivy_lsp.semantic.nodes import (  # Re-exported backward compat
    ActionNode,
    MonitorNode,
    PropertyNode,
    RequirementNode,
    RfcAnnotation,
    RfcRequirement,
    StateVarNode,
    SymbolNode,
    TypeNode,
)


class TestSymbolNode:
    def test_defaults(self):
        n = SymbolNode(
            id="test.ivy:10:foo",
            name="foo",
            qualified_name="bar.foo",
            kind="action",
            file="test.ivy",
            line=10,
        )
        assert n.col == 0
        assert n.sort_name is None
        assert n.arity == 0
        assert n.params == []
        assert n.return_sort is None
        assert n.tier == "tier1"

    def test_with_type_info(self):
        n = SymbolNode(
            id="test.ivy:5:send",
            name="send",
            qualified_name="quic.send",
            kind="action",
            file="test.ivy",
            line=5,
            sort_name="action",
            arity=2,
            params=["dst:ip.endpoint", "pkt:quic_packet"],
            return_sort="bool",
            tier="tier2",
        )
        assert n.arity == 2
        assert len(n.params) == 2
        assert n.tier == "tier2"


class TestTypeNode:
    def test_plain_type(self):
        n = TypeNode(
            id="test.ivy:3:cid",
            name="cid",
            qualified_name="quic.cid",
            file="test.ivy",
            line=3,
        )
        assert n.is_enum is False
        assert n.variants == []

    def test_enum_type(self):
        n = TypeNode(
            id="test.ivy:4:pkt_type",
            name="pkt_type",
            qualified_name="quic.pkt_type",
            file="test.ivy",
            line=4,
            is_enum=True,
            variants=["initial", "handshake", "zero_rtt", "one_rtt"],
        )
        assert n.is_enum is True
        assert len(n.variants) == 4


class TestMonitorNode:
    def test_creation(self):
        n = MonitorNode(
            id="test.ivy:20:monitor",
            action_name="send_pkt",
            mixin_kind="before",
            file="test.ivy",
            line=20,
            requirement_ids=["test.ivy:21", "test.ivy:22"],
        )
        assert len(n.requirement_ids) == 2


class TestRfcRequirement:
    def test_creation(self):
        n = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data...",
            level="MUST",
            layer="frame",
        )
        assert n.testable is True
        assert n.level == "MUST"


class TestRfcAnnotation:
    def test_creation(self):
        n = RfcAnnotation(
            id="test.ivy:15:0",
            file="test.ivy",
            line=15,
            tags=["rfc9000:4.1", "rfc9000:8.1"],
        )
        assert len(n.tags) == 2
        assert n.node_id is None


class TestBackwardCompatReexports:
    def test_requirement_node_importable(self):
        n = RequirementNode(
            id="x:1",
            kind="require",
            formula_text="true",
            line=0,
            col=0,
            file="x",
            monitor_action="a",
            mixin_kind="before",
        )
        assert n.kind == "require"

    def test_state_var_node_importable(self):
        n = StateVarNode(id="v", name="v", qualified_name="v", file="x", line=0)
        assert n.is_relation is False

    def test_action_node_importable(self):
        n = ActionNode(id="a", name="a", qualified_name="a", file="x", line=0)
        assert n.name == "a"

    def test_property_node_importable(self):
        n = PropertyNode(
            id="p:1",
            kind="invariant",
            name="p",
            formula_text="true",
            file="x",
            line=0,
        )
        assert n.kind == "invariant"


class TestSemanticEdgeType:
    def test_all_values_iterable(self):
        values = list(SemanticEdgeType)
        assert len(values) >= 13  # 5 existing + 8 new
        names = {e.name for e in values}
        assert "READS" in names
        assert "COVERS" in names
        assert "HAS_TYPE" in names

    def test_existing_edge_type_still_works(self):
        assert EdgeType.READS.value == "reads"
        assert EdgeType.CONSTRAINS.value == "constrains"
