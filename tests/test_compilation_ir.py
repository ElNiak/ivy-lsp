"""Tests for IR sub-dataclasses used in subprocess serialization.

Each test creates an instance with realistic QUIC protocol data,
performs a pickle round-trip (dumps then loads), and asserts equality
and key field values.
"""

from __future__ import annotations

import pickle

import pytest

from ivy_lsp.core.compilation.ir import (
    ActionIR,
    CompiledModuleIR,
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
            constructors=("initial", "handshake", "zero_rtt", "one_rtt", "retry"),
            interpretation=None,
        )
        restored = pickle.loads(pickle.dumps(sort))
        assert restored == sort
        assert restored.name == "quic_packet_type"
        assert restored.is_enumerated is True
        assert restored.constructors == (
            "initial",
            "handshake",
            "zero_rtt",
            "one_rtt",
            "retry",
        )

    def test_pickle_round_trip_uninterpreted(self):
        sort = SortIR(
            name="stream_id",
            arity=0,
            is_uninterpreted=True,
            is_enumerated=False,
            constructors=(),
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
        assert sort.constructors == ()
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
            domain_sorts=("cid", "quic_packet_type"),
            range_sort="bool",
            is_destructor=False,
            is_constructor=False,
            is_relation=True,
        )
        restored = pickle.loads(pickle.dumps(sym))
        assert restored == sym
        assert restored.name == "conn_seen"
        assert restored.is_relation is True
        assert restored.domain_sorts == ("cid", "quic_packet_type")
        assert restored.range_sort == "bool"

    def test_pickle_round_trip_destructor(self):
        sym = SymbolIR(
            name="stream_data",
            sort_str="stream_id -> stream_data_t",
            domain_sorts=("stream_id",),
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
        assert sym.domain_sorts == ()
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
            formal_params=("dst:cid", "pkt:quic_packet"),
            formal_returns=("ok:bool",),
            is_exported=True,
            is_imported=False,
        )
        restored = pickle.loads(pickle.dumps(action))
        assert restored == action
        assert restored.name == "quic_server.send_packet"
        assert restored.formal_params == ("dst:cid", "pkt:quic_packet")
        assert restored.formal_returns == ("ok:bool",)
        assert restored.is_exported is True
        assert restored.is_imported is False

    def test_pickle_round_trip_imported(self):
        action = ActionIR(
            name="quic_client.recv_packet",
            formal_params=("src:cid",),
            formal_returns=("pkt:quic_packet",),
            is_exported=False,
            is_imported=True,
        )
        restored = pickle.loads(pickle.dumps(action))
        assert restored == action
        assert restored.is_imported is True

    def test_defaults(self):
        action = ActionIR(name="noop")
        assert action.formal_params == ()
        assert action.formal_returns == ()
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
            verified_components=(
                "quic_server",
                "tls_handshake",
                "packet_protection",
            ),
            present_components=(
                "quic_client",
                "quic_shim",
                "net",
            ),
        )
        restored = pickle.loads(pickle.dumps(isolate))
        assert restored == isolate
        assert restored.name == "quic_server_test"
        assert restored.verified_components == (
            "quic_server",
            "tls_handshake",
            "packet_protection",
        )
        assert restored.present_components == (
            "quic_client",
            "quic_shim",
            "net",
        )

    def test_defaults(self):
        isolate = IsolateIR(name="empty_isolate")
        assert isolate.verified_components == ()
        assert isolate.present_components == ()

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
        formula = LabeledFormulaIR(label="inv", formula_str="true")
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


class TestCompiledModuleIR:
    """Pickle round-trip and factory tests for the top-level container."""

    def _make_full_ir(self) -> CompiledModuleIR:
        """Build a realistic CompiledModuleIR with QUIC protocol data."""
        return CompiledModuleIR(
            sorts={
                "quic_packet_type": SortIR(
                    name="quic_packet_type",
                    is_enumerated=True,
                    constructors=("initial", "handshake", "one_rtt"),
                ),
                "cid": SortIR(
                    name="cid",
                    is_uninterpreted=True,
                    interpretation="bv[64]",
                ),
            },
            symbols={
                "conn_seen": SymbolIR(
                    name="conn_seen",
                    sort_str="cid * quic_packet_type -> bool",
                    domain_sorts=("cid", "quic_packet_type"),
                    range_sort="bool",
                    is_relation=True,
                ),
            },
            actions={
                "quic_server.send_packet": ActionIR(
                    name="quic_server.send_packet",
                    formal_params=("dst:cid", "pkt:quic_packet"),
                    formal_returns=("ok:bool",),
                    is_exported=True,
                ),
            },
            public_actions=frozenset({"quic_server.send_packet"}),
            mixins={
                "quic_server.handle_send": (
                    MixinIR(
                        mixer="quic_shim.send_event",
                        mixee="quic_server.handle_send",
                        kind="before",
                    ),
                ),
            },
            isolates={
                "quic_server_test": IsolateIR(
                    name="quic_server_test",
                    verified_components=("quic_server",),
                    present_components=("quic_client", "quic_shim"),
                ),
            },
            labeled_axioms=(
                LabeledFormulaIR(
                    label="ax1",
                    formula_str="forall C:cid. conn_seen(C) -> true",
                    lineno=10,
                ),
            ),
            labeled_properties=(
                LabeledFormulaIR(
                    label="prop1",
                    formula_str="forall C:cid. established(C) -> ready(C)",
                    lineno=50,
                ),
            ),
            labeled_conjectures=(
                LabeledFormulaIR(
                    label="conj1",
                    formula_str="eventually(done)",
                    temporal=True,
                ),
            ),
            definitions=(
                LabeledFormulaIR(
                    label="def1",
                    formula_str="is_valid(X) = X > 0",
                ),
            ),
            requirements=(
                RequirementIR(
                    action_name="quic_server.send_packet",
                    kind="require",
                    formula_str="conn_established(dst)",
                    mixin_kind="before",
                ),
            ),
            hierarchy={"quic_server": frozenset({"send_packet", "recv_packet"})},
            exports=("quic_server.send_packet",),
            imports=("quic_shim.recv_event",),
            aliases={"pkt_type": "quic_packet_type"},
            delegates=("quic_shim",),
            mixord=("quic_shim.send_event", "quic_server.handle_send"),
            sort_order=("cid", "quic_packet_type"),
            symbol_order=("conn_seen",),
            errors=(),
            success=True,
            source_file="quic_server_test.ivy",
            compile_duration=1.234,
        )

    def test_pickle_round_trip_success(self):
        ir = self._make_full_ir()
        restored = pickle.loads(pickle.dumps(ir))
        assert restored == ir
        assert restored.success is True
        assert restored.source_file == "quic_server_test.ivy"
        assert restored.compile_duration == 1.234
        # Verify sub-IR contents survived
        assert "quic_packet_type" in restored.sorts
        assert restored.sorts["quic_packet_type"].is_enumerated is True
        assert restored.sorts["quic_packet_type"].constructors == (
            "initial",
            "handshake",
            "one_rtt",
        )
        assert "conn_seen" in restored.symbols
        assert restored.symbols["conn_seen"].is_relation is True
        assert "quic_server.send_packet" in restored.actions
        assert restored.public_actions == frozenset({"quic_server.send_packet"})
        assert len(restored.mixins["quic_server.handle_send"]) == 1
        assert restored.isolates["quic_server_test"].name == "quic_server_test"
        assert len(restored.labeled_axioms) == 1
        assert len(restored.labeled_properties) == 1
        assert len(restored.labeled_conjectures) == 1
        assert len(restored.definitions) == 1
        assert len(restored.requirements) == 1
        assert restored.hierarchy == {
            "quic_server": frozenset({"send_packet", "recv_packet"}),
        }
        assert restored.exports == ("quic_server.send_packet",)
        assert restored.imports == ("quic_shim.recv_event",)
        assert restored.aliases == {"pkt_type": "quic_packet_type"}
        assert restored.errors == ()

    def test_pickle_round_trip_failure(self):
        ir = CompiledModuleIR(
            source_file="broken.ivy",
            errors=(
                "line 10: type error in sort declaration",
                "line 25: undefined symbol 'foo'",
            ),
            success=False,
            compile_duration=0.5,
        )
        restored = pickle.loads(pickle.dumps(ir))
        assert restored == ir
        assert restored.success is False
        assert restored.source_file == "broken.ivy"
        assert restored.compile_duration == 0.5
        assert len(restored.errors) == 2
        assert "type error" in restored.errors[0]
        assert "undefined symbol" in restored.errors[1]
        # All container fields should be empty defaults
        assert restored.sorts == {}
        assert restored.symbols == {}
        assert restored.actions == {}
        assert restored.public_actions == frozenset()
        assert restored.mixins == {}
        assert restored.isolates == {}
        assert restored.labeled_axioms == ()
        assert restored.labeled_properties == ()
        assert restored.labeled_conjectures == ()
        assert restored.definitions == ()
        assert restored.requirements == ()
        assert restored.hierarchy == {}
        assert restored.exports == ()
        assert restored.imports == ()
        assert restored.aliases == {}
        assert restored.delegates == ()
        assert restored.mixord == ()
        assert restored.sort_order == ()
        assert restored.symbol_order == ()

    def test_empty_ir_factory(self):
        ir = CompiledModuleIR.empty(
            source_file="test.ivy",
            errors=["compilation failed"],
            duration=0.1,
        )
        assert ir.success is False
        assert ir.source_file == "test.ivy"
        assert ir.errors == ("compilation failed",)
        assert ir.compile_duration == 0.1
        assert ir.sorts == {}
        assert ir.symbols == {}
        assert ir.actions == {}
        assert ir.public_actions == frozenset()

    def test_empty_ir_factory_defaults(self):
        ir = CompiledModuleIR.empty(source_file="minimal.ivy")
        assert ir.success is False
        assert ir.source_file == "minimal.ivy"
        assert ir.errors == ()
        assert ir.compile_duration == 0.0

    def test_frozen(self):
        ir = CompiledModuleIR.empty(source_file="test.ivy")
        with pytest.raises(AttributeError):
            ir.success = True  # type: ignore[misc]
