"""Tests for the Module IR extractor.

Uses mock objects that mimic real Ivy Module/Sig structure so we don't
need Z3 or the ivy package to test extraction logic.  Each test creates
a mock module with reasonable defaults, runs the extractor, and asserts
the resulting CompiledModuleIR fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

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

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _ns(**kwargs: Any) -> SimpleNamespace:
    """Shorthand for SimpleNamespace."""
    return SimpleNamespace(**kwargs)


class _NamedObj:
    """Object with a `name` attribute that stringifies to that name.

    Used for domain/range sort objects where ``str(obj)`` must return
    the sort name (unlike ``SimpleNamespace`` which uses a repr).
    """

    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return self.name


def _make_mock_sort(
    *,
    name: str = "t",
    arity: int = 0,
    sort_type: str = "Sort",
    constructors: Optional[List[str]] = None,
    interpretation: Optional[str] = None,
) -> Any:
    """Create a mock sort object whose ``type().__name__`` is *sort_type*.

    We dynamically create a class with the given *sort_type* name so that
    ``type(obj).__name__`` returns the expected string (e.g. "EnumeratedSort").
    """

    def _defines(self_inner: Any) -> List[SimpleNamespace]:
        if self_inner._constructors is None:
            return []
        return [_ns(name=c) for c in self_inner._constructors]

    cls = type(sort_type, (), {"defines": _defines})
    obj = cls()
    obj.name = name  # type: ignore[attr-defined]
    obj.arity = arity  # type: ignore[attr-defined]
    obj._constructors = constructors  # type: ignore[attr-defined]
    obj.interpretation = interpretation  # type: ignore[attr-defined]
    return obj


class _MockSymbolSort:
    """Mimics a symbol sort with .dom and .rng attributes."""

    def __init__(
        self,
        dom: Optional[List[Any]] = None,
        rng: Any = None,
    ):
        self.dom = dom if dom is not None else []
        self.rng = rng if rng is not None else _NamedObj("void")

    def __str__(self) -> str:
        dom_strs = [str(d) for d in self.dom]
        rng_str = str(self.rng)
        if dom_strs:
            return f"{' * '.join(dom_strs)} -> {rng_str}"
        return rng_str


class _MockSymbol:
    """Mimics an Ivy symbol object."""

    def __init__(
        self,
        name: str = "sym",
        sort: Optional[_MockSymbolSort] = None,
        is_destructor: bool = False,
        is_constructor: bool = False,
    ):
        self.name = name
        self.sort = sort if sort is not None else _MockSymbolSort()
        self._is_destructor = is_destructor
        self._is_constructor = is_constructor


class _MockAction:
    """Mimics an Ivy action object."""

    def __init__(
        self,
        name: str = "act",
        formal_params: Optional[List[Any]] = None,
        formal_returns: Optional[List[Any]] = None,
        args: Optional[List[Any]] = None,
    ):
        self.name = name
        self.formal_params = formal_params if formal_params is not None else []
        self.formal_returns = formal_returns if formal_returns is not None else []
        self.args = args if args is not None else []


def _make_requirement_action(kind: str, formula: str = "true") -> Any:
    """Create a mock requirement action whose type().__name__ is *kind*.

    *kind* should be one of "RequiresAction", "EnsuresAction",
    "AssumeAction", "AssertAction".
    """
    cls = type(kind, (), {"__str__": lambda self: formula})
    obj = cls()
    obj.args = []  # type: ignore[attr-defined]
    return obj


class _MockMixin:
    """Mimics an Ivy mixin (generic / around)."""

    def __init__(self, mixer: str, mixee: str):
        self.args = [_ns(relname=mixer)]
        self.mixee = _ns(relname=mixee)


class MixinBeforeDef(_MockMixin):
    """Mimics Ivy MixinBeforeDef -- type name contains 'Before'."""

    pass


class MixinAfterDef(_MockMixin):
    """Mimics Ivy MixinAfterDef -- type name contains 'After'."""

    pass


class _MockIsolate:
    """Mimics an Ivy isolate object."""

    def __init__(
        self,
        name: str = "iso",
        verified: Optional[List[str]] = None,
        present: Optional[List[str]] = None,
    ):
        self.name = name
        self._verified = verified if verified is not None else []
        self._present = present if present is not None else []

    def verified(self) -> List[SimpleNamespace]:
        return [_ns(relname=v) for v in self._verified]

    def present(self) -> List[SimpleNamespace]:
        return [_ns(relname=p) for p in self._present]


class _MockLabeledFormula:
    """Mimics an Ivy labeled formula (axiom/property/conjecture)."""

    def __init__(
        self,
        label: str = "lbl",
        formula: str = "true",
        lineno: Optional[int] = None,
        temporal: bool = False,
        assumed: bool = False,
    ):
        self.label = _ns(relname=label, lineno=lineno)
        self.formula = formula
        self.temporal = temporal
        self.assumed = assumed
        if lineno is not None:
            self.lineno = lineno

    def __str__(self) -> str:
        return f"{self.label.relname}: {self.formula}"


def _make_mock_sig(
    sorts: Optional[Dict[str, Any]] = None,
    symbols: Optional[Dict[str, Any]] = None,
    destructor_sorts: Optional[Dict[str, Any]] = None,
    constructor_sorts: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    """Build a mock Sig object with reasonable defaults."""
    return _ns(
        sorts=sorts if sorts is not None else {},
        symbols=symbols if symbols is not None else {},
        destructor_sorts=destructor_sorts if destructor_sorts is not None else {},
        constructor_sorts=constructor_sorts if constructor_sorts is not None else {},
    )


def _make_mock_module(
    sig: Optional[SimpleNamespace] = None,
    actions: Optional[Dict[str, Any]] = None,
    public_actions: Optional[set] = None,
    mixins: Optional[Dict[str, List[Any]]] = None,
    isolates: Optional[Dict[str, Any]] = None,
    labeled_axioms: Optional[List[Any]] = None,
    labeled_props: Optional[List[Any]] = None,
    labeled_conjs: Optional[List[Any]] = None,
    definitions: Optional[List[Any]] = None,
    hierarchy: Optional[Dict[str, set]] = None,
    exports: Optional[List[Any]] = None,
    imports: Optional[List[Any]] = None,
    aliases: Optional[Dict[str, str]] = None,
    delegates: Optional[List[Any]] = None,
    mixord: Optional[List[Any]] = None,
    sort_order: Optional[List[str]] = None,
    symbol_order: Optional[List[str]] = None,
) -> SimpleNamespace:
    """Build a mock Module with reasonable defaults for all attributes."""
    if sig is None:
        sig = _make_mock_sig()
    return _ns(
        sig=sig,
        actions=actions if actions is not None else {},
        public_actions=public_actions if public_actions is not None else set(),
        mixins=mixins if mixins is not None else {},
        isolates=isolates if isolates is not None else {},
        labeled_axioms=labeled_axioms if labeled_axioms is not None else [],
        labeled_props=labeled_props if labeled_props is not None else [],
        labeled_conjs=labeled_conjs if labeled_conjs is not None else [],
        definitions=definitions if definitions is not None else [],
        hierarchy=hierarchy if hierarchy is not None else {},
        exports=exports if exports is not None else [],
        imports=imports if imports is not None else [],
        aliases=aliases if aliases is not None else {},
        delegates=delegates if delegates is not None else [],
        mixord=mixord if mixord is not None else [],
        sort_order=sort_order if sort_order is not None else [],
        symbol_order=symbol_order if symbol_order is not None else [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractSorts:
    """Verify extraction of sorts from the module signature."""

    def test_extracts_enumerated_sort(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sort = _make_mock_sort(
            name="pkt_type",
            arity=0,
            sort_type="EnumeratedSort",
            constructors=["initial", "handshake", "one_rtt"],
        )
        sig = _make_mock_sig(sorts={"pkt_type": sort})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.5)

        assert ir.success is True
        assert "pkt_type" in ir.sorts
        s = ir.sorts["pkt_type"]
        assert isinstance(s, SortIR)
        assert s.name == "pkt_type"
        assert s.is_enumerated is True
        assert s.is_uninterpreted is False
        assert s.constructors == ("initial", "handshake", "one_rtt")
        assert s.arity == 0

    def test_extracts_uninterpreted_sort(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sort = _make_mock_sort(
            name="cid",
            arity=0,
            sort_type="UninterpretedSort",
        )
        sig = _make_mock_sig(sorts={"cid": sort})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "cid" in ir.sorts
        s = ir.sorts["cid"]
        assert s.is_uninterpreted is True
        assert s.is_enumerated is False
        assert s.constructors == ()

    def test_extracts_interpreted_sort(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sort = _make_mock_sort(
            name="idx",
            arity=0,
            sort_type="Sort",
            interpretation="int",
        )
        sig = _make_mock_sig(sorts={"idx": sort})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "idx" in ir.sorts
        s = ir.sorts["idx"]
        assert s.is_uninterpreted is False
        assert s.is_enumerated is False
        assert s.interpretation == "int"

    def test_extracts_multiple_sorts(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sorts = {
            "cid": _make_mock_sort(name="cid", sort_type="UninterpretedSort"),
            "pkt_type": _make_mock_sort(
                name="pkt_type",
                sort_type="EnumeratedSort",
                constructors=["initial", "retry"],
            ),
        }
        sig = _make_mock_sig(sorts=sorts)
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.sorts) == 2
        assert "cid" in ir.sorts
        assert "pkt_type" in ir.sorts


class TestExtractSymbols:
    """Verify extraction of symbols from the module signature."""

    def test_extracts_relation_symbol(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        dom_sort_a = _NamedObj("cid")
        dom_sort_b = _NamedObj("pkt_type")
        rng_sort = _NamedObj("bool")
        sym = _MockSymbol(
            name="conn_seen",
            sort=_MockSymbolSort(dom=[dom_sort_a, dom_sort_b], rng=rng_sort),
        )
        sig = _make_mock_sig(symbols={"conn_seen": sym})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "conn_seen" in ir.symbols
        s = ir.symbols["conn_seen"]
        assert isinstance(s, SymbolIR)
        assert s.name == "conn_seen"
        assert s.domain_sorts == ("cid", "pkt_type")
        assert s.range_sort == "bool"
        assert s.is_relation is True

    def test_extracts_destructor_symbol(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        rng_sort = _NamedObj("data_t")
        sym = _MockSymbol(
            name="stream_data",
            sort=_MockSymbolSort(rng=rng_sort),
            is_destructor=True,
        )
        sig = _make_mock_sig(
            symbols={"stream_data": sym},
            destructor_sorts={"stream_data": True},
        )
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "stream_data" in ir.symbols
        s = ir.symbols["stream_data"]
        assert s.is_destructor is True

    def test_extracts_constructor_symbol(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        rng_sort = _NamedObj("pkt")
        sym = _MockSymbol(
            name="mk_pkt",
            sort=_MockSymbolSort(rng=rng_sort),
            is_constructor=True,
        )
        sig = _make_mock_sig(
            symbols={"mk_pkt": sym},
            constructor_sorts={"mk_pkt": True},
        )
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "mk_pkt" in ir.symbols
        s = ir.symbols["mk_pkt"]
        assert s.is_constructor is True

    def test_extracts_symbol_domain_range(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        dom = [_NamedObj("node")]
        rng = _NamedObj("bool")
        sym = _MockSymbol(
            name="active",
            sort=_MockSymbolSort(dom=dom, rng=rng),
        )
        sig = _make_mock_sig(symbols={"active": sym})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        s = ir.symbols["active"]
        assert s.domain_sorts == ("node",)
        assert s.range_sort == "bool"


class TestExtractActions:
    """Verify extraction of actions from the module."""

    def test_extracts_action_with_params(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        action = _MockAction(
            name="send_packet",
            formal_params=[
                _ns(name="dst", sort=_ns(name="cid")),
                _ns(name="pkt", sort=_ns(name="quic_packet")),
            ],
            formal_returns=[
                _ns(name="ok", sort=_ns(name="bool")),
            ],
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            actions={"send_packet": action},
            public_actions={"send_packet"},
            exports=[_ns(relname="send_packet")],
            imports=[],
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "send_packet" in ir.actions
        a = ir.actions["send_packet"]
        assert isinstance(a, ActionIR)
        assert a.name == "send_packet"
        assert len(a.formal_params) == 2
        assert "send_packet" in ir.public_actions
        assert a.is_exported is True

    def test_extracts_imported_action(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        action = _MockAction(name="recv_event")
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            actions={"recv_event": action},
            imports=[_ns(relname="recv_event")],
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "recv_event" in ir.actions
        a = ir.actions["recv_event"]
        assert a.is_imported is True
        assert a.is_exported is False

    def test_extracts_action_no_params(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        action = _MockAction(name="init")
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"init": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        a = ir.actions["init"]
        assert a.formal_params == ()
        assert a.formal_returns == ()


class TestExtractMixins:
    """Verify extraction of mixins from the module."""

    def test_extracts_before_mixin(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        mixin = MixinBeforeDef(
            mixer="shim.before_send",
            mixee="server.handle_send",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            mixins={"server.handle_send": [mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "server.handle_send" in ir.mixins
        mixins = ir.mixins["server.handle_send"]
        assert len(mixins) == 1
        m = mixins[0]
        assert isinstance(m, MixinIR)
        assert m.mixer == "shim.before_send"
        assert m.mixee == "server.handle_send"
        assert m.kind == "before"

    def test_extracts_after_mixin(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        mixin = MixinAfterDef(
            mixer="shim.after_recv",
            mixee="client.handle_recv",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            mixins={"client.handle_recv": [mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        mixins = ir.mixins["client.handle_recv"]
        m = mixins[0]
        assert m.kind == "after"

    def test_detects_mixin_kind_from_type(self):
        """Mixin kind is detected from AST class type, not mixer name."""
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        before_mixin = MixinBeforeDef(
            mixer="shim.pre_check",
            mixee="server.step",
        )
        after_mixin = MixinAfterDef(
            mixer="log.after_step",
            mixee="server.step",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            mixins={"server.step": [before_mixin, after_mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        mixins = ir.mixins["server.step"]
        assert len(mixins) == 2
        assert mixins[0].kind == "before"
        assert mixins[1].kind == "after"

    def test_around_mixin_detected_for_unknown_type(self):
        """A mixin with neither 'Before' nor 'After' in type name -> 'around'."""
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        mixin = _MockMixin(
            mixer="shim.wrap_step",
            mixee="server.step",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            mixins={"server.step": [mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        mixins = ir.mixins["server.step"]
        assert len(mixins) == 1
        assert mixins[0].kind == "around"


class TestExtractIsolates:
    """Verify extraction of isolates from the module."""

    def test_extracts_isolate_verified_present(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        isolate = _MockIsolate(
            name="quic_server_test",
            verified=["quic_server", "tls_handshake"],
            present=["quic_client", "quic_shim"],
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            isolates={"quic_server_test": isolate},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "quic_server_test" in ir.isolates
        iso = ir.isolates["quic_server_test"]
        assert isinstance(iso, IsolateIR)
        assert iso.name == "quic_server_test"
        assert iso.verified_components == ("quic_server", "tls_handshake")
        assert iso.present_components == ("quic_client", "quic_shim")

    def test_extracts_empty_isolate(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        isolate = _MockIsolate(name="empty_iso")
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            isolates={"empty_iso": isolate},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        iso = ir.isolates["empty_iso"]
        assert iso.verified_components == ()
        assert iso.present_components == ()


class TestExtractLabeledFormulas:
    """Verify extraction of labeled axioms, properties, conjectures, definitions."""

    def test_extracts_labeled_axiom(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        axiom = _MockLabeledFormula(
            label="ax1",
            formula="forall C:cid. conn_seen(C) -> true",
            lineno=10,
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, labeled_axioms=[axiom])

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.labeled_axioms) == 1
        a = ir.labeled_axioms[0]
        assert isinstance(a, LabeledFormulaIR)
        assert a.label == "ax1"
        assert "conn_seen" in a.formula_str
        assert a.lineno == 10

    def test_extracts_labeled_property(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        prop = _MockLabeledFormula(
            label="prop1",
            formula="established(C) -> ready(C)",
            lineno=50,
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, labeled_props=[prop])

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.labeled_properties) == 1
        p = ir.labeled_properties[0]
        assert p.label == "prop1"

    def test_extracts_conjecture(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        conj = _MockLabeledFormula(
            label="conj1",
            formula="eventually(done)",
            temporal=True,
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, labeled_conjs=[conj])

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.labeled_conjectures) == 1
        c = ir.labeled_conjectures[0]
        assert c.label == "conj1"
        assert c.temporal is True

    def test_extracts_definition(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        defn = _MockLabeledFormula(
            label="def1",
            formula="is_valid(X) = X > 0",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, definitions=[defn])

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.definitions) == 1
        d = ir.definitions[0]
        assert d.label == "def1"


class TestExtractRequirements:
    """Verify extraction of require/ensure/assume/assert from action bodies."""

    def test_extracts_requires_action(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        req = _make_requirement_action("RequiresAction", "conn_established(dst)")
        action = _MockAction(name="send_packet", args=[req])
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"send_packet": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert len(ir.requirements) >= 1
        r = ir.requirements[0]
        assert isinstance(r, RequirementIR)
        assert r.action_name == "send_packet"
        assert r.kind == "require"

    def test_extracts_ensures_action(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        ens = _make_requirement_action("EnsuresAction", "packet_sent(dst, pkt)")
        action = _MockAction(name="send_packet", args=[ens])
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"send_packet": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        reqs = [r for r in ir.requirements if r.kind == "ensure"]
        assert len(reqs) >= 1
        assert reqs[0].action_name == "send_packet"

    def test_extracts_assume_action(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        assume = _make_requirement_action("AssumeAction", "valid(x)")
        action = _MockAction(name="recv", args=[assume])
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"recv": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        reqs = [r for r in ir.requirements if r.kind == "assume"]
        assert len(reqs) >= 1

    def test_extracts_assert_action(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        assert_act = _make_requirement_action("AssertAction", "ready(self)")
        action = _MockAction(name="init", args=[assert_act])
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"init": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        reqs = [r for r in ir.requirements if r.kind == "assert"]
        assert len(reqs) >= 1


class TestMixinKindPropagation:
    """Verify mixin_kind propagates from mod.mixins to RequirementIR."""

    def test_before_mixer_requirements_tagged_before(self):
        """Requirements in a before-mixer action body get mixin_kind='before'."""
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        req = _make_requirement_action("RequiresAction", "pre(x)")
        mixer_action = _MockAction(name="shim.before_send", args=[req])

        mixin = MixinBeforeDef(
            mixer="shim.before_send",
            mixee="server.handle_send",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            actions={
                "shim.before_send": mixer_action,
                "server.handle_send": _MockAction(),
            },
            mixins={"server.handle_send": [mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        mixer_reqs = [r for r in ir.requirements if r.action_name == "shim.before_send"]
        assert len(mixer_reqs) >= 1
        assert mixer_reqs[0].mixin_kind == "before"

    def test_after_mixer_requirements_tagged_after(self):
        """Requirements in an after-mixer action body get mixin_kind='after'."""
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        ens = _make_requirement_action("EnsuresAction", "post(y)")
        mixer_action = _MockAction(name="shim.after_recv", args=[ens])

        mixin = MixinAfterDef(
            mixer="shim.after_recv",
            mixee="client.handle_recv",
        )
        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            actions={
                "shim.after_recv": mixer_action,
                "client.handle_recv": _MockAction(),
            },
            mixins={"client.handle_recv": [mixin]},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        mixer_reqs = [r for r in ir.requirements if r.action_name == "shim.after_recv"]
        assert len(mixer_reqs) >= 1
        assert mixer_reqs[0].mixin_kind == "after"

    def test_non_mixer_requirements_tagged_direct(self):
        """Requirements in a regular action body (not a mixer) get mixin_kind='direct'."""
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        req = _make_requirement_action("RequiresAction", "valid(x)")
        action = _MockAction(name="server.process", args=[req])
        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig, actions={"server.process": action})

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        reqs = [r for r in ir.requirements if r.action_name == "server.process"]
        assert len(reqs) >= 1
        assert reqs[0].mixin_kind == "direct"


class TestHandlesExtractionErrorGracefully:
    """Verify the extractor never raises, returning failed IR on errors."""

    def test_none_module_returns_failed_ir(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        ir = extract_compiled_module_ir(None, None, "broken.ivy", 0.5)

        assert isinstance(ir, CompiledModuleIR)
        assert ir.success is False
        assert ir.source_file == "broken.ivy"
        assert ir.compile_duration == 0.5
        assert ir.sorts == {}
        assert ir.symbols == {}
        assert ir.actions == {}

    def test_module_with_no_sig_returns_failed_ir(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        mod = _ns(sig=None, actions={}, public_actions=set())
        ir = extract_compiled_module_ir(mod, None, "no_sig.ivy", 0.2)

        assert isinstance(ir, CompiledModuleIR)
        # Should still succeed partially or fail gracefully
        assert ir.source_file == "no_sig.ivy"

    def test_sort_with_broken_defines_does_not_raise(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        def _broken_defines(self):
            raise RuntimeError("Z3 error")

        BrokenSort = type(
            "EnumeratedSort",
            (),
            {
                "defines": _broken_defines,
            },
        )
        broken = BrokenSort()
        broken.name = "broken"
        broken.arity = 0

        sig = _make_mock_sig(sorts={"broken": broken})
        mod = _make_mock_module(sig=sig)

        # Must not raise
        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)
        assert isinstance(ir, CompiledModuleIR)

    def test_symbol_with_broken_sort_does_not_raise(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        class BrokenSymbol:
            name = "bad_sym"

            @property
            def sort(self):
                raise RuntimeError("Z3 explosion")

        sig = _make_mock_sig(symbols={"bad_sym": BrokenSymbol()})
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)
        assert isinstance(ir, CompiledModuleIR)


class TestStructuralMetadata:
    """Verify that structural metadata fields are copied through."""

    def test_copies_hierarchy(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            hierarchy={"server": {"send", "recv"}},
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert "server" in ir.hierarchy
        assert ir.hierarchy["server"] == {"send", "recv"}

    def test_copies_exports_imports(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            exports=[_ns(relname="server.send")],
            imports=[_ns(relname="shim.recv")],
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert ir.exports == ("server.send",)
        assert ir.imports == ("shim.recv",)

    def test_copies_aliases_delegates(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            aliases={"pkt": "quic_packet"},
            delegates=["shim"],
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert ir.aliases == {"pkt": "quic_packet"}
        assert ir.delegates == ("shim",)

    def test_copies_orderings(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sig = _make_mock_sig()
        mod = _make_mock_module(
            sig=sig,
            mixord=["a", "b"],
            sort_order=["cid", "pkt"],
            symbol_order=["conn_seen", "active"],
        )

        ir = extract_compiled_module_ir(mod, sig, "test.ivy", 0.1)

        assert ir.mixord == ("a", "b")
        assert ir.sort_order == ("cid", "pkt")
        assert ir.symbol_order == ("conn_seen", "active")


class TestCompilationMetadata:
    """Verify compilation metadata fields."""

    def test_sets_source_file_and_duration(self):
        from ivy_lsp.core.compilation.extractor import extract_compiled_module_ir

        sig = _make_mock_sig()
        mod = _make_mock_module(sig=sig)

        ir = extract_compiled_module_ir(mod, sig, "my_module.ivy", 2.5)

        assert ir.source_file == "my_module.ivy"
        assert ir.compile_duration == 2.5
        assert ir.success is True
        assert ir.errors == ()
