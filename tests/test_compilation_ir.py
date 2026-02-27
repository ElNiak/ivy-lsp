"""Tests for IR sub-dataclasses used in subprocess serialization.

Each test creates an instance with realistic QUIC protocol data,
performs a pickle round-trip (dumps then loads), and asserts equality
and key field values.
"""
from __future__ import annotations

import pickle

import pytest

from ivy_lsp.compilation.ir import (
    ActionIR,
    IsolateIR,
    LabeledFormulaIR,
    MixinIR,
    RequirementIR,
    SortIR,
    SymbolIR,
)


class TestSortIR:
    """Pickle round-trip for SortIR with enumerated constructors."""

    def test_pickle_round_trip_enumerated(self):
        sort = SortIR(
            name="quic_packet_type",
            arity=0,
            is_uninterpreted=False,
            is_enumerated=True,
            constructors=["initial", "handshake", "zero_rtt", "one_rtt", "retry"],
            interpretation=None,
        )
        restored = pickle.loads(pickle.dumps(sort))
        assert restored == sort
        assert restored.name == "quic_packet_type"
        assert restored.is_enumerated is True
        assert restored.constructors == [
            "initial",
            "handshake",
            "zero_rtt",
            "one_rtt",
            "retry",
        ]

    def test_pickle_round_trip_uninterpreted(self):
        sort = SortIR(
            name="stream_id",
            arity=0,
            is_uninterpreted=True,
            is_enumerated=False,
            constructors=[],
            interpretation="bv[62]",
        )
        restored = pickle.loads(pickle.dumps(sort))
        assert restored == sort
        assert restored.is_uninterpreted is True
        assert restored.interpretation == "bv[62]"

    def test_defaults(self):
        sort = SortIR(name="cid")
        assert sort.arity == 0
        assert sort.is_uninterpreted is False
        assert sort.is_enumerated is False
        assert sort.constructors == []
        assert sort.interpretation is None

    def test_frozen(self):
        sort = SortIR(name="cid")
        with pytest.raises(AttributeError):
            sort.name = "other"  # type: ignore[misc]


class TestSymbolIR:
    """Pickle round-trip for SymbolIR with relation flag."""

    def test_pickle_round_trip_relation(self):
        sym = SymbolIR(
            name="conn_seen",
            sort_str="cid * quic_packet_type -> bool",
            domain_sorts=["cid", "quic_packet_type"],
            range_sort="bool",
            is_destructor=False,
            is_constructor=False,
            is_relation=True,
        )
        restored = pickle.loads(pickle.dumps(sym))
        assert restored == sym
        assert restored.name == "conn_seen"
        assert restored.is_relation is True
        assert restored.domain_sorts == ["cid", "quic_packet_type"]
        assert restored.range_sort == "bool"

    def test_pickle_round_trip_destructor(self):
        sym = SymbolIR(
            name="stream_data",
            sort_str="stream_id -> stream_data_t",
            domain_sorts=["stream_id"],
            range_sort="stream_data_t",
            is_destructor=True,
            is_constructor=False,
            is_relation=False,
        )
        restored = pickle.loads(pickle.dumps(sym))
        assert restored == sym
        assert restored.is_destructor is True

    def test_defaults(self):
        sym = SymbolIR(name="x")
        assert sym.sort_str == ""
        assert sym.domain_sorts == []
        assert sym.range_sort == ""
        assert sym.is_destructor is False
        assert sym.is_constructor is False
        assert sym.is_relation is False

    def test_frozen(self):
        sym = SymbolIR(name="x")
        with pytest.raises(AttributeError):
            sym.is_relation = True  # type: ignore[misc]


class TestActionIR:
    """Pickle round-trip for ActionIR with formal params/returns."""

    def test_pickle_round_trip(self):
        action = ActionIR(
            name="quic_server.send_packet",
            formal_params=["dst:cid", "pkt:quic_packet"],
            formal_returns=["ok:bool"],
            is_exported=True,
            is_imported=False,
        )
        restored = pickle.loads(pickle.dumps(action))
        assert restored == action
        assert restored.name == "quic_server.send_packet"
        assert restored.formal_params == ["dst:cid", "pkt:quic_packet"]
        assert restored.formal_returns == ["ok:bool"]
        assert restored.is_exported is True
        assert restored.is_imported is False

    def test_pickle_round_trip_imported(self):
        action = ActionIR(
            name="quic_client.recv_packet",
            formal_params=["src:cid"],
            formal_returns=["pkt:quic_packet"],
            is_exported=False,
            is_imported=True,
        )
        restored = pickle.loads(pickle.dumps(action))
        assert restored == action
        assert restored.is_imported is True

    def test_defaults(self):
        action = ActionIR(name="noop")
        assert action.formal_params == []
        assert action.formal_returns == []
        assert action.is_exported is False
        assert action.is_imported is False

    def test_frozen(self):
        action = ActionIR(name="noop")
        with pytest.raises(AttributeError):
            action.name = "other"  # type: ignore[misc]


class TestMixinIR:
    """Pickle round-trip for MixinIR."""

    def test_pickle_round_trip_before(self):
        mixin = MixinIR(
            mixer="quic_shim.send_event",
            mixee="quic_server.handle_send",
            kind="before",
        )
        restored = pickle.loads(pickle.dumps(mixin))
        assert restored == mixin
        assert restored.mixer == "quic_shim.send_event"
        assert restored.mixee == "quic_server.handle_send"
        assert restored.kind == "before"

    def test_pickle_round_trip_after(self):
        mixin = MixinIR(
            mixer="quic_shim.log_recv",
            mixee="quic_client.handle_recv",
            kind="after",
        )
        restored = pickle.loads(pickle.dumps(mixin))
        assert restored == mixin
        assert restored.kind == "after"

    def test_frozen(self):
        mixin = MixinIR(mixer="a", mixee="b", kind="before")
        with pytest.raises(AttributeError):
            mixin.kind = "after"  # type: ignore[misc]


class TestIsolateIR:
    """Pickle round-trip for IsolateIR with verified/present components."""

    def test_pickle_round_trip(self):
        isolate = IsolateIR(
            name="quic_server_test",
            verified_components=[
                "quic_server",
                "tls_handshake",
                "packet_protection",
            ],
            present_components=[
                "quic_client",
                "quic_shim",
                "net",
            ],
        )
        restored = pickle.loads(pickle.dumps(isolate))
        assert restored == isolate
        assert restored.name == "quic_server_test"
        assert restored.verified_components == [
            "quic_server",
            "tls_handshake",
            "packet_protection",
        ]
        assert restored.present_components == [
            "quic_client",
            "quic_shim",
            "net",
        ]

    def test_defaults(self):
        isolate = IsolateIR(name="empty_isolate")
        assert isolate.verified_components == []
        assert isolate.present_components == []

    def test_frozen(self):
        isolate = IsolateIR(name="x")
        with pytest.raises(AttributeError):
            isolate.name = "y"  # type: ignore[misc]


class TestLabeledFormulaIR:
    """Pickle round-trip for LabeledFormulaIR."""

    def test_pickle_round_trip(self):
        formula = LabeledFormulaIR(
            label="quic_invariant_01",
            formula_str="forall C:cid. conn_established(C) -> crypto_ready(C)",
            lineno=142,
            temporal=False,
            is_assumed=False,
        )
        restored = pickle.loads(pickle.dumps(formula))
        assert restored == formula
        assert restored.label == "quic_invariant_01"
        assert restored.formula_str == (
            "forall C:cid. conn_established(C) -> crypto_ready(C)"
        )
        assert restored.lineno == 142
        assert restored.temporal is False
        assert restored.is_assumed is False

    def test_pickle_round_trip_temporal_assumed(self):
        formula = LabeledFormulaIR(
            label="liveness_guarantee",
            formula_str="eventually(packet_delivered(P))",
            lineno=None,
            temporal=True,
            is_assumed=True,
        )
        restored = pickle.loads(pickle.dumps(formula))
        assert restored == formula
        assert restored.temporal is True
        assert restored.is_assumed is True
        assert restored.lineno is None

    def test_defaults(self):
        formula = LabeledFormulaIR(
            label="inv", formula_str="true"
        )
        assert formula.lineno is None
        assert formula.temporal is False
        assert formula.is_assumed is False

    def test_frozen(self):
        formula = LabeledFormulaIR(label="inv", formula_str="true")
        with pytest.raises(AttributeError):
            formula.label = "other"  # type: ignore[misc]


class TestRequirementIR:
    """Pickle round-trip for RequirementIR."""

    def test_pickle_round_trip_require(self):
        req = RequirementIR(
            action_name="quic_server.send_packet",
            kind="require",
            formula_str="conn_established(dst)",
            mixin_kind="before",
        )
        restored = pickle.loads(pickle.dumps(req))
        assert restored == req
        assert restored.action_name == "quic_server.send_packet"
        assert restored.kind == "require"
        assert restored.formula_str == "conn_established(dst)"
        assert restored.mixin_kind == "before"

    def test_pickle_round_trip_ensure(self):
        req = RequirementIR(
            action_name="quic_server.send_packet",
            kind="ensure",
            formula_str="packet_sent(dst, pkt)",
            mixin_kind="after",
        )
        restored = pickle.loads(pickle.dumps(req))
        assert restored == req
        assert restored.kind == "ensure"
        assert restored.mixin_kind == "after"

    def test_pickle_round_trip_direct(self):
        req = RequirementIR(
            action_name="quic_server.init",
            kind="assert",
            formula_str="server_ready(self)",
            mixin_kind="direct",
        )
        restored = pickle.loads(pickle.dumps(req))
        assert restored == req
        assert restored.mixin_kind == "direct"

    def test_default_mixin_kind(self):
        req = RequirementIR(
            action_name="act",
            kind="require",
            formula_str="true",
        )
        assert req.mixin_kind == "direct"

    def test_frozen(self):
        req = RequirementIR(
            action_name="act",
            kind="require",
            formula_str="true",
        )
        with pytest.raises(AttributeError):
            req.kind = "ensure"  # type: ignore[misc]
