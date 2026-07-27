"""Tests for Task 1.6: Document Symbols Feature (TDD).

Verifies that ``IvySymbol`` instances are correctly converted to LSP
``DocumentSymbol`` objects and that the feature handler registration works.
"""

import pytest
from lsprotocol import types as lsp
from lsprotocol.types import SymbolKind


class TestIvySymbolToDocumentSymbol:
    """Verify conversion of a single IvySymbol to a DocumentSymbol."""

    def test_basic_conversion(self):
        """A simple IvySymbol maps to DocumentSymbol with matching fields."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="cid",
            kind=SymbolKind.Class,
            range=(2, 0, 2, 8),
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert isinstance(result, lsp.DocumentSymbol)
        assert result.name == "cid"
        assert result.kind == SymbolKind.Class

    def test_range_conversion(self):
        """IvySymbol.range=(2, 0, 2, 10) maps to Range(Position(2,0), Position(2,10))."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="pkt_num",
            kind=SymbolKind.Class,
            range=(2, 0, 2, 10),
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert result.range.start.line == 2
        assert result.range.start.character == 0
        assert result.range.end.line == 2
        assert result.range.end.character == 10

    def test_selection_range_equals_range(self):
        """selection_range should equal range (whole symbol span)."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="foo",
            kind=SymbolKind.Module,
            range=(5, 0, 10, 1),
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert result.selection_range == result.range

    def test_detail_preservation(self):
        """sym.detail='params: x, y' is passed through to DocumentSymbol.detail."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="send",
            kind=SymbolKind.Function,
            range=(5, 0, 7, 1),
            detail="params: x, y",
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert result.detail == "params: x, y"

    def test_detail_none(self):
        """When IvySymbol.detail is None, DocumentSymbol.detail is None."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="cid",
            kind=SymbolKind.Class,
            range=(0, 0, 0, 8),
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert result.detail is None


class TestRecursiveChildrenConversion:
    """Verify that nested IvySymbol children are recursively converted."""

    def test_single_child(self):
        """An IvySymbol with one child produces a DocumentSymbol with one child."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        child = IvySymbol(
            name="zero",
            kind=SymbolKind.Variable,
            range=(3, 4, 3, 20),
        )
        parent = IvySymbol(
            name="bit",
            kind=SymbolKind.Module,
            range=(2, 0, 5, 1),
            children=[child],
        )
        result = ivy_symbol_to_document_symbol(parent)

        assert result.children is not None
        assert len(result.children) == 1
        assert result.children[0].name == "zero"
        assert result.children[0].kind == SymbolKind.Variable

    def test_multiple_children(self):
        """An IvySymbol with multiple children produces matching DocumentSymbol children."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        children = [
            IvySymbol(name="zero", kind=SymbolKind.Variable, range=(3, 4, 3, 20)),
            IvySymbol(name="one", kind=SymbolKind.Variable, range=(4, 4, 4, 19)),
        ]
        parent = IvySymbol(
            name="bit",
            kind=SymbolKind.Module,
            range=(2, 0, 5, 1),
            children=children,
        )
        result = ivy_symbol_to_document_symbol(parent)

        assert result.children is not None
        assert len(result.children) == 2
        assert result.children[0].name == "zero"
        assert result.children[1].name == "one"

    def test_nested_grandchildren(self):
        """Two levels of nesting: parent -> child -> grandchild."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        grandchild = IvySymbol(
            name="val",
            kind=SymbolKind.Variable,
            range=(5, 8, 5, 20),
        )
        child = IvySymbol(
            name="inner",
            kind=SymbolKind.Module,
            range=(4, 4, 6, 5),
            children=[grandchild],
        )
        parent = IvySymbol(
            name="outer",
            kind=SymbolKind.Module,
            range=(2, 0, 8, 1),
            children=[child],
        )
        result = ivy_symbol_to_document_symbol(parent)

        assert result.children is not None
        assert len(result.children) == 1
        inner = result.children[0]
        assert inner.children is not None
        assert len(inner.children) == 1
        assert inner.children[0].name == "val"

    def test_no_children_gives_none(self):
        """An IvySymbol with empty children list produces DocumentSymbol.children=None."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(
            name="cid",
            kind=SymbolKind.Class,
            range=(0, 0, 0, 8),
            children=[],
        )
        result = ivy_symbol_to_document_symbol(sym)

        assert result.children is None


class TestIvySymbolsToDocumentSymbols:
    """Verify batch conversion of IvySymbol lists."""

    def test_batch_conversion(self):
        """A list of IvySymbols is converted to matching DocumentSymbols."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbols_to_document_symbols

        symbols = [
            IvySymbol(name="cid", kind=SymbolKind.Class, range=(0, 0, 0, 8)),
            IvySymbol(name="send", kind=SymbolKind.Function, range=(2, 0, 4, 1)),
        ]
        result = ivy_symbols_to_document_symbols(symbols)

        assert len(result) == 2
        assert result[0].name == "cid"
        assert result[1].name == "send"

    def test_empty_list(self):
        """An empty list produces an empty result."""
        from ivy_lsp.lsp.document_symbols import ivy_symbols_to_document_symbols

        result = ivy_symbols_to_document_symbols([])
        assert result == []

    def test_preserves_order(self):
        """Output order matches input order."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbols_to_document_symbols

        symbols = [
            IvySymbol(name="alpha", kind=SymbolKind.Class, range=(0, 0, 0, 5)),
            IvySymbol(name="beta", kind=SymbolKind.Function, range=(1, 0, 1, 5)),
            IvySymbol(name="gamma", kind=SymbolKind.Variable, range=(2, 0, 2, 5)),
        ]
        result = ivy_symbols_to_document_symbols(symbols)

        assert [ds.name for ds in result] == ["alpha", "beta", "gamma"]


class TestGetDocumentSymbols:
    """Verify the null-safe wrapper function."""

    def test_none_input(self):
        """get_document_symbols(None) returns empty list."""
        from ivy_lsp.lsp.document_symbols import get_document_symbols

        result = get_document_symbols(None)
        assert result == []

    def test_empty_list_input(self):
        """get_document_symbols([]) returns empty list."""
        from ivy_lsp.lsp.document_symbols import get_document_symbols

        result = get_document_symbols([])
        assert result == []

    def test_valid_symbols(self):
        """get_document_symbols with actual symbols returns DocumentSymbols."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import get_document_symbols

        symbols = [
            IvySymbol(name="cid", kind=SymbolKind.Class, range=(0, 0, 0, 8)),
            IvySymbol(name="bit", kind=SymbolKind.Module, range=(2, 0, 5, 1)),
        ]
        result = get_document_symbols(symbols)

        assert len(result) == 2
        assert all(isinstance(ds, lsp.DocumentSymbol) for ds in result)


class TestStatusDocumentSymbol:
    """Verify synthetic status symbols used for degraded documentSymbol responses."""

    def test_status_symbol_shape(self):
        """Status symbol should contain prefixed name, detail, and zero range."""
        from ivy_lsp.lsp.document_symbols import _status_document_symbol

        sym = _status_document_symbol("indexing in progress", "warmup")

        assert isinstance(sym, lsp.DocumentSymbol)
        assert sym.name.startswith("[ivy-lsp] ")
        assert sym.detail == "warmup"
        assert sym.range.start.line == 0
        assert sym.range.end.line == 0


class TestMultipleKinds:
    """Verify that different SymbolKinds are preserved through conversion."""

    def test_class_kind(self):
        """SymbolKind.Class is preserved."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(name="t", kind=SymbolKind.Class, range=(0, 0, 0, 5))
        assert ivy_symbol_to_document_symbol(sym).kind == SymbolKind.Class

    def test_function_kind(self):
        """SymbolKind.Function is preserved."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(name="send", kind=SymbolKind.Function, range=(0, 0, 0, 5))
        assert ivy_symbol_to_document_symbol(sym).kind == SymbolKind.Function

    def test_module_kind(self):
        """SymbolKind.Module is preserved."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(name="foo", kind=SymbolKind.Module, range=(0, 0, 0, 5))
        assert ivy_symbol_to_document_symbol(sym).kind == SymbolKind.Module

    def test_variable_kind(self):
        """SymbolKind.Variable is preserved."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(name="val", kind=SymbolKind.Variable, range=(0, 0, 0, 5))
        assert ivy_symbol_to_document_symbol(sym).kind == SymbolKind.Variable

    def test_namespace_kind(self):
        """SymbolKind.Namespace is preserved."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import ivy_symbol_to_document_symbol

        sym = IvySymbol(name="ns", kind=SymbolKind.Namespace, range=(0, 0, 0, 5))
        assert ivy_symbol_to_document_symbol(sym).kind == SymbolKind.Namespace


class TestZeroRangeFilter:
    """Verify that symbols with (0,0,0,0) range (included-file leaks) are filtered."""

    def test_zero_range_symbols_removed(self):
        """Top-level symbols with range (0,0,0,0) are removed."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import _filter_zero_range

        symbols = [
            IvySymbol(name="real", kind=SymbolKind.Class, range=(5, 0, 10, 1)),
            IvySymbol(name="leaked", kind=SymbolKind.Class, range=(0, 0, 0, 0)),
            IvySymbol(name="also_real", kind=SymbolKind.Function, range=(12, 0, 15, 1)),
        ]
        result = _filter_zero_range(symbols)

        assert len(result) == 2
        assert [s.name for s in result] == ["real", "also_real"]

    def test_zero_range_children_removed(self):
        """Nested children with range (0,0,0,0) are removed recursively."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import _filter_zero_range

        parent = IvySymbol(
            name="parent",
            kind=SymbolKind.Module,
            range=(2, 0, 20, 1),
            children=[
                IvySymbol(
                    name="good_child", kind=SymbolKind.Variable, range=(3, 4, 3, 20)
                ),
                IvySymbol(
                    name="leaked_child", kind=SymbolKind.Variable, range=(0, 0, 0, 0)
                ),
            ],
        )
        result = _filter_zero_range([parent])

        assert len(result) == 1
        assert len(result[0].children) == 1
        assert result[0].children[0].name == "good_child"

    def test_all_zero_range_returns_empty(self):
        """If all symbols have (0,0,0,0) range, result is empty."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import _filter_zero_range

        symbols = [
            IvySymbol(name="a", kind=SymbolKind.Class, range=(0, 0, 0, 0)),
            IvySymbol(name="b", kind=SymbolKind.Class, range=(0, 0, 0, 0)),
        ]
        assert _filter_zero_range(symbols) == []

    def test_empty_input_returns_empty(self):
        """Empty input produces empty output."""
        from ivy_lsp.lsp.document_symbols import _filter_zero_range

        assert _filter_zero_range([]) == []

    def test_no_zero_range_passes_through(self):
        """Symbols with non-zero ranges pass through unchanged."""
        from ivy_lsp.core.parsing.symbols import IvySymbol
        from ivy_lsp.lsp.document_symbols import _filter_zero_range

        symbols = [
            IvySymbol(name="a", kind=SymbolKind.Class, range=(1, 0, 5, 1)),
            IvySymbol(name="b", kind=SymbolKind.Function, range=(6, 0, 10, 1)),
        ]
        result = _filter_zero_range(symbols)
        assert len(result) == 2
        assert [s.name for s in result] == ["a", "b"]


class TestRegisterFeature:
    """Verify that the register function can be imported and called."""

    def test_register_importable(self):
        """The register function exists and is callable."""
        from ivy_lsp.lsp.document_symbols import register

        assert callable(register)

    def test_register_with_server(self):
        """IvyLanguageServer registers the documentSymbol feature on init."""
        from ivy_lsp.lsp.server import IvyLanguageServer

        server = IvyLanguageServer()
        # Feature is registered during __init__; no exception means success.
