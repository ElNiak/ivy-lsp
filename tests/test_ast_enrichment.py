"""Tests for AstEnrichmentAdapter -- Tier 2 type information extraction.

All tests use mock AST objects so ``ivy`` does NOT need to be installed.
The adapter's ``_process_decl`` dispatches via ``isinstance()``, which
requires real ``ivy.ivy_ast`` classes.  We therefore test via
``extract_type_info`` and ``_extract_type_decl`` / ``_extract_action_decl``
directly with duck-typed mock objects, and we patch ``ivy.ivy_ast`` where
``isinstance`` checks are needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from ivy_lsp.adapters.ast_enrichment_adapter import AstEnrichmentAdapter, _extract_line
from ivy_lsp.adapters.protocols import IAstEnrichmentAdapter, TypeAnnotation


# ---------------------------------------------------------------------------
# Mock AST factories
# ---------------------------------------------------------------------------


def _make_lineno(line: int = 1) -> SimpleNamespace:
    """Return a mock lineno object with a ``.line`` attribute (1-based)."""
    return SimpleNamespace(line=line)


def _make_type_decl(
    name: str,
    line: int = 1,
    *,
    enum_variants: list[str] | None = None,
) -> SimpleNamespace:
    """Create a mock TypeDecl.

    Parameters:
        name: Sort name (e.g. ``"cid"``).
        line: 1-based line number.
        enum_variants: If given, the type has extension members.
    """
    decl = SimpleNamespace()
    decl.defines = lambda: [(name, None)]
    decl.lineno = _make_lineno(line)

    if enum_variants is not None:
        extensions = [SimpleNamespace(rep=v) for v in enum_variants]
        sort_def = SimpleNamespace(extension=extensions)
        decl.args = [sort_def]
    else:
        decl.args = []

    return decl


def _make_action_decl(
    name: str,
    line: int = 1,
    *,
    params: list[tuple[str, str | None]] | None = None,
    returns: list[tuple[str, str | None]] | None = None,
) -> SimpleNamespace:
    """Create a mock ActionDecl.

    Parameters:
        name: Action name (e.g. ``"send"``).
        line: 1-based line number.
        params: List of ``(name, sort_or_None)`` for formal parameters.
        returns: List of ``(name, sort_or_None)`` for formal returns.
    """
    formal_params = []
    if params:
        for pname, psort in params:
            p = SimpleNamespace(rep=pname, relname=pname, sort=psort)
            formal_params.append(p)

    formal_returns = []
    if returns:
        for rname, rsort in returns:
            r = SimpleNamespace(rep=rname, relname=rname, sort=rsort)
            formal_returns.append(r)

    action_def = SimpleNamespace(
        formal_params=formal_params,
        formal_returns=formal_returns,
    )

    decl = SimpleNamespace()
    decl.defines = lambda: [(name, None)]
    decl.lineno = _make_lineno(line)
    decl.args = [action_def]

    return decl


def _make_ast(decls: list[Any]) -> SimpleNamespace:
    """Wrap a list of decls into a mock AST object with a ``.decls`` attr."""
    return SimpleNamespace(decls=decls)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """AstEnrichmentAdapter satisfies IAstEnrichmentAdapter at runtime."""

    def test_isinstance_check(self):
        adapter = AstEnrichmentAdapter()
        assert isinstance(adapter, IAstEnrichmentAdapter)


# ---------------------------------------------------------------------------
# Edge cases: None / empty AST
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """extract_type_info handles None, missing decls, empty decls."""

    def test_none_ast_returns_empty(self):
        adapter = AstEnrichmentAdapter()
        assert adapter.extract_type_info(None, "test.ivy", "") == []

    def test_ast_without_decls_attr_returns_empty(self):
        adapter = AstEnrichmentAdapter()
        assert adapter.extract_type_info(object(), "test.ivy", "") == []

    def test_ast_with_empty_decls_returns_empty(self):
        adapter = AstEnrichmentAdapter()
        ast = _make_ast([])
        assert adapter.extract_type_info(ast, "test.ivy", "") == []


# ---------------------------------------------------------------------------
# TypeDecl extraction (called directly, bypassing isinstance dispatch)
# ---------------------------------------------------------------------------


class TestExtractTypeDecl:
    """_extract_type_decl produces correct TypeAnnotation for sort/enum decls."""

    def test_simple_sort(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl("cid", line=5)
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "cid"
        assert ta.qualified_name == "cid"
        assert ta.sort_name == "cid"
        assert ta.is_enum is False
        assert ta.variants == []
        assert ta.file == "test.ivy"
        assert ta.line == 4  # 0-based from 1-based line 5

    def test_enum_sort_extracts_variants(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl("stream_kind", line=3, enum_variants=["unidir", "bidir"])
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "stream_kind"
        assert ta.is_enum is True
        assert ta.variants == ["unidir", "bidir"]

    def test_enum_with_many_variants(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl(
            "packet_type", line=10, enum_variants=["initial", "handshake", "one_rtt"]
        )
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "quic.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.is_enum is True
        assert len(ta.variants) == 3
        assert "initial" in ta.variants
        assert "one_rtt" in ta.variants
        assert ta.file == "quic.ivy"
        assert ta.line == 9

    def test_type_decl_no_defines_produces_nothing(self):
        adapter = AstEnrichmentAdapter()
        decl = SimpleNamespace(defines=lambda: [], args=[], lineno=_make_lineno(1))
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "test.ivy", annotations)
        assert annotations == []


# ---------------------------------------------------------------------------
# ActionDecl extraction (called directly, bypassing isinstance dispatch)
# ---------------------------------------------------------------------------


class TestExtractActionDecl:
    """_extract_action_decl produces correct TypeAnnotation for actions."""

    def test_action_with_params_and_return(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl(
            "send",
            line=7,
            params=[("src", "cid"), ("dst", "cid")],
            returns=[("result", "bool")],
        )
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "send"
        assert ta.sort_name == "action"
        assert ta.arity == 2
        assert ta.params == ["src:cid", "dst:cid"]
        assert ta.return_sort == "bool"
        assert ta.file == "test.ivy"
        assert ta.line == 6

    def test_action_with_no_params_no_return(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl("noop", line=1)
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "noop"
        assert ta.sort_name == "action"
        assert ta.arity == 0
        assert ta.params == []
        assert ta.return_sort is None

    def test_action_with_params_no_return(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl(
            "recv",
            line=20,
            params=[("pkt", "packet"), ("conn", "cid")],
        )
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.arity == 2
        assert ta.params == ["pkt:packet", "conn:cid"]
        assert ta.return_sort is None

    def test_action_param_without_sort(self):
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl(
            "step",
            line=1,
            params=[("x", None)],
        )
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.params == ["x"]
        assert ta.arity == 1

    def test_action_no_defines_produces_nothing(self):
        adapter = AstEnrichmentAdapter()
        action_def = SimpleNamespace(formal_params=[], formal_returns=[])
        decl = SimpleNamespace(
            defines=lambda: [],
            args=[action_def],
            lineno=_make_lineno(1),
        )
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)
        assert annotations == []


# ---------------------------------------------------------------------------
# Fake ivy.ivy_ast types for isinstance dispatch tests
# ---------------------------------------------------------------------------

# We create real classes that can be used with isinstance().
# Objects are created as instances of these classes with attributes set
# after construction (the classes use __dict__ freely).


class _FakeTypeDecl:
    """Stand-in for ivy.ivy_ast.TypeDecl."""

    pass


class _FakeActionDecl:
    """Stand-in for ivy.ivy_ast.ActionDecl."""

    pass


def _make_fake_ia_module() -> SimpleNamespace:
    """Return a fake ``ivy.ivy_ast`` module with TypeDecl and ActionDecl."""
    return SimpleNamespace(TypeDecl=_FakeTypeDecl, ActionDecl=_FakeActionDecl)


def _patch_ivy_ast():
    """Context manager that patches sys.modules so ``import ivy.ivy_ast as ia`` works.

    Returns a context manager and the fake ia module for assertions.
    """
    ia_module = _make_fake_ia_module()
    # The import statement ``import ivy.ivy_ast as ia`` does:
    #   1. Looks up "ivy" in sys.modules
    #   2. Looks up "ivy.ivy_ast" in sys.modules
    #   3. Also sets ia = sys.modules["ivy"].ivy_ast
    # We need both sys.modules entries AND the ivy mock's attribute to match.
    ivy_mock = SimpleNamespace(ivy_ast=ia_module)
    return patch.dict("sys.modules", {"ivy": ivy_mock, "ivy.ivy_ast": ia_module})


def _as_type_decl(
    name: str,
    line: int = 1,
    *,
    enum_variants: list[str] | None = None,
) -> _FakeTypeDecl:
    """Build a _FakeTypeDecl with the same attributes as _make_type_decl."""
    obj = _FakeTypeDecl()
    obj.defines = lambda: [(name, None)]  # type: ignore[attr-defined]
    obj.lineno = _make_lineno(line)  # type: ignore[attr-defined]
    if enum_variants is not None:
        extensions = [SimpleNamespace(rep=v) for v in enum_variants]
        sort_def = SimpleNamespace(extension=extensions)
        obj.args = [sort_def]  # type: ignore[attr-defined]
    else:
        obj.args = []  # type: ignore[attr-defined]
    return obj


def _as_action_decl(
    name: str,
    line: int = 1,
    *,
    params: list[tuple[str, str | None]] | None = None,
    returns: list[tuple[str, str | None]] | None = None,
) -> _FakeActionDecl:
    """Build a _FakeActionDecl with the same attributes as _make_action_decl."""
    formal_params = []
    if params:
        for pname, psort in params:
            formal_params.append(SimpleNamespace(rep=pname, relname=pname, sort=psort))
    formal_returns = []
    if returns:
        for rname, rsort in returns:
            formal_returns.append(SimpleNamespace(rep=rname, relname=rname, sort=rsort))
    action_def = SimpleNamespace(
        formal_params=formal_params, formal_returns=formal_returns
    )
    obj = _FakeActionDecl()
    obj.defines = lambda: [(name, None)]  # type: ignore[attr-defined]
    obj.lineno = _make_lineno(line)  # type: ignore[attr-defined]
    obj.args = [action_def]  # type: ignore[attr-defined]
    return obj


# ---------------------------------------------------------------------------
# _process_decl dispatch (requires patching ivy.ivy_ast)
# ---------------------------------------------------------------------------


class TestProcessDeclDispatch:
    """_process_decl dispatches to the correct extractor based on type."""

    def test_dispatch_skips_when_ivy_unavailable(self):
        """When ivy.ivy_ast cannot be imported, _process_decl returns silently."""
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl("cid")
        annotations: List[TypeAnnotation] = []

        # Patch the import inside _process_decl to raise ImportError
        with patch.dict("sys.modules", {"ivy": None, "ivy.ivy_ast": None}):
            adapter._process_decl(decl, "test.ivy", annotations)
        assert annotations == []

    def test_dispatch_type_decl(self):
        """When isinstance matches TypeDecl, _extract_type_decl is called."""
        adapter = AstEnrichmentAdapter()
        decl = _as_type_decl("my_sort", line=3)

        annotations: List[TypeAnnotation] = []
        with _patch_ivy_ast():
            adapter._process_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        assert annotations[0].name == "my_sort"

    def test_dispatch_action_decl(self):
        """When isinstance matches ActionDecl, _extract_action_decl is called."""
        adapter = AstEnrichmentAdapter()
        decl = _as_action_decl("act", line=5, params=[("x", "nat")])

        annotations: List[TypeAnnotation] = []
        with _patch_ivy_ast():
            adapter._process_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        assert annotations[0].name == "act"
        assert annotations[0].sort_name == "action"

    def test_dispatch_unknown_decl_type_produces_nothing(self):
        """Declarations that are neither TypeDecl nor ActionDecl are skipped."""
        adapter = AstEnrichmentAdapter()

        # A plain object that is not an instance of either fake class
        decl = SimpleNamespace(defines=lambda: [("x", None)])

        annotations: List[TypeAnnotation] = []
        with _patch_ivy_ast():
            adapter._process_decl(decl, "test.ivy", annotations)

        assert annotations == []


# ---------------------------------------------------------------------------
# Full extract_type_info with patched dispatch
# ---------------------------------------------------------------------------


class TestExtractTypeInfoIntegration:
    """End-to-end test of extract_type_info with a mock AST."""

    def test_mixed_decl_types(self):
        """An AST with both TypeDecl and ActionDecl produces annotations for both."""
        adapter = AstEnrichmentAdapter()

        type_decl = _as_type_decl("cid", line=3, enum_variants=["a", "b"])
        action_decl = _as_action_decl(
            "send", line=10, params=[("src", "cid")], returns=[("ok", "bool")]
        )
        ast = _make_ast([type_decl, action_decl])

        with _patch_ivy_ast():
            annotations = adapter.extract_type_info(ast, "test.ivy", "")

        assert len(annotations) == 2

        type_ann = annotations[0]
        assert type_ann.name == "cid"
        assert type_ann.is_enum is True
        assert type_ann.variants == ["a", "b"]

        action_ann = annotations[1]
        assert action_ann.name == "send"
        assert action_ann.sort_name == "action"
        assert action_ann.params == ["src:cid"]
        assert action_ann.return_sort == "bool"

    def test_failing_decl_is_skipped(self):
        """A decl whose processing raises an exception is skipped gracefully."""
        adapter = AstEnrichmentAdapter()

        # First decl will break because defines() raises
        bad_decl = _FakeTypeDecl()
        bad_decl.defines = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[attr-defined]

        good_decl = _as_type_decl("ok_sort", line=5)

        ast = _make_ast([bad_decl, good_decl])

        with _patch_ivy_ast():
            annotations = adapter.extract_type_info(ast, "test.ivy", "")

        # The bad decl is skipped, good decl succeeds
        assert len(annotations) == 1
        assert annotations[0].name == "ok_sort"


# ---------------------------------------------------------------------------
# Helper: _extract_line
# ---------------------------------------------------------------------------


class TestTypeAnnotationNaming:
    """M6: name should be short leaf, qualified_name should be full path."""

    def test_dotted_type_name_splits_correctly(self):
        """For 'quic.frame_type', name should be 'frame_type', qualified_name 'quic.frame_type'."""
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl("quic.frame_type", line=5)
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "frame_type", f"name should be short leaf, got '{ta.name}'"
        assert ta.qualified_name == "quic.frame_type", (
            f"qualified_name should be full path, got '{ta.qualified_name}'"
        )

    def test_dotted_action_name_splits_correctly(self):
        """For 'quic.connection.send', name should be 'send'."""
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl("quic.connection.send", line=10, params=[("x", "cid")])
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "send", f"name should be short leaf, got '{ta.name}'"
        assert ta.qualified_name == "quic.connection.send", (
            f"qualified_name should be full path, got '{ta.qualified_name}'"
        )

    def test_simple_type_name_unchanged(self):
        """Non-dotted names: name == qualified_name."""
        adapter = AstEnrichmentAdapter()
        decl = _make_type_decl("bool", line=1)
        annotations: List[TypeAnnotation] = []
        adapter._extract_type_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "bool"
        assert ta.qualified_name == "bool"

    def test_simple_action_name_unchanged(self):
        """Non-dotted action names: name == qualified_name."""
        adapter = AstEnrichmentAdapter()
        decl = _make_action_decl("send", line=1)
        annotations: List[TypeAnnotation] = []
        adapter._extract_action_decl(decl, "test.ivy", annotations)

        assert len(annotations) == 1
        ta = annotations[0]
        assert ta.name == "send"
        assert ta.qualified_name == "send"


class TestExtractLine:
    """Unit tests for the _extract_line helper."""

    def test_valid_lineno(self):
        decl = SimpleNamespace(lineno=_make_lineno(10))
        assert _extract_line(decl) == 9  # 0-based

    def test_line_one_gives_zero(self):
        decl = SimpleNamespace(lineno=_make_lineno(1))
        assert _extract_line(decl) == 0

    def test_no_lineno_gives_zero(self):
        decl = SimpleNamespace()
        assert _extract_line(decl) == 0

    def test_lineno_without_line_attr_gives_zero(self):
        decl = SimpleNamespace(lineno=SimpleNamespace())
        assert _extract_line(decl) == 0


# ---------------------------------------------------------------------------
# ast_to_symbols enhancements: _extract_constant_detail
# ---------------------------------------------------------------------------


class TestConstantDetailEnhancement:
    """The _extract_constant_detail helper in ast_to_symbols."""

    def test_constant_detail_with_sort(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        atom = SimpleNamespace(args=[], sort="nat")
        detail = _extract_constant_detail(atom)
        assert detail == ": nat"

    def test_relation_detail_with_params_and_sort(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        p1 = SimpleNamespace(rep="X", sort="cid")
        p2 = SimpleNamespace(rep="Y", sort="cid")
        atom = SimpleNamespace(args=[p1, p2], sort="bool")
        detail = _extract_constant_detail(atom)
        assert detail is not None
        assert "(X:cid, Y:cid)" in detail
        assert ": bool" in detail

    def test_constant_detail_no_sort_no_args(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        atom = SimpleNamespace(args=[], sort=None)
        detail = _extract_constant_detail(atom)
        assert detail is None

    def test_constant_detail_params_without_sort(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        p1 = SimpleNamespace(rep="X", sort=None)
        atom = SimpleNamespace(args=[p1], sort=None)
        detail = _extract_constant_detail(atom)
        assert detail is not None
        assert "(X)" in detail

    def test_constant_detail_sort_only_no_args(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        atom = SimpleNamespace(sort="pkt_num")
        detail = _extract_constant_detail(atom)
        assert detail == ": pkt_num"

    def test_constant_detail_handles_exception(self):
        from ivy_lsp.parsing.ast_to_symbols import _extract_constant_detail

        # An object where accessing .args raises
        atom = MagicMock()
        atom.args = property(lambda self: (_ for _ in ()).throw(AttributeError))
        # MagicMock handles getattr weirdly, so use a class
        class BadAtom:
            @property
            def args(self):
                raise AttributeError("no args")
            sort = None

        detail = _extract_constant_detail(BadAtom())
        assert detail is None
