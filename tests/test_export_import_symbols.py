# tests/test_export_import_symbols.py
"""Tests for ExportDecl/ImportDecl symbol extraction in ast_to_symbols."""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol


# ---------------------------------------------------------------------------
# Fake AST types for mocking ivy.ivy_ast
# ---------------------------------------------------------------------------


class _FakeExportDecl:
    """Stand-in for ivy.ivy_ast.ExportDecl."""

    pass


class _FakeExportDef:
    """Stand-in for ExportDef (child of ExportDecl.args)."""

    pass


class _FakeImportDecl:
    """Stand-in for ivy.ivy_ast.ImportDecl."""

    pass


class _FakeImportDef:
    """Stand-in for ImportDef (child of ImportDecl.args)."""

    pass


class _Sentinel:
    """Type that no fake decl is an instance of.

    Used for all non-export/import decl types so isinstance checks
    in _convert_decl fall through to the export/import handlers.
    """

    pass


def _make_lineno(line_1based: int) -> SimpleNamespace:
    """Create a fake lineno with 1-based .line attribute."""
    return SimpleNamespace(line=line_1based)


def _make_fake_ia_module() -> SimpleNamespace:
    """Fake ivy.ivy_ast with all decl types.

    ExportDecl/ImportDecl map to our fakes; all others map to _Sentinel
    so isinstance checks return False for our fake decls.
    """
    return SimpleNamespace(
        ObjectDecl=_Sentinel,
        ActionDecl=_Sentinel,
        TypeDecl=_Sentinel,
        DefinitionDecl=_Sentinel,
        PropertyDecl=_Sentinel,
        AxiomDecl=_Sentinel,
        ConjectureDecl=_Sentinel,
        IsolateDecl=_Sentinel,
        ModuleDecl=_Sentinel,
        AliasDecl=_Sentinel,
        DestructorDecl=_Sentinel,
        ConstructorDecl=_Sentinel,
        ConstantDecl=_Sentinel,
        InstantiateDecl=_Sentinel,
        MixinDecl=_Sentinel,
        VariantDecl=_Sentinel,
        ExportDecl=_FakeExportDecl,
        ImportDecl=_FakeImportDecl,
    )


def _patch_ivy_ast():
    """Context manager patching sys.modules for import ivy.ivy_ast."""
    ia_module = _make_fake_ia_module()
    ivy_mock = SimpleNamespace(ivy_ast=ia_module)
    return patch.dict(
        "sys.modules", {"ivy": ivy_mock, "ivy.ivy_ast": ia_module}
    )


def _make_export_decl(names_and_lines):
    """Build _FakeExportDecl with ExportDef children.

    Args:
        names_and_lines: list of (name, 1_based_line) tuples.
    """
    decl = _FakeExportDecl()
    defs = []
    last_line = 1
    for name, line_1based in names_and_lines:
        defn = _FakeExportDef()
        defn.args = [SimpleNamespace(relname=name)]
        defs.append(defn)
        last_line = line_1based
    decl.args = defs
    decl.lineno = _make_lineno(last_line)
    return decl


def _make_import_decl(names_and_lines):
    """Build _FakeImportDecl with ImportDef children."""
    decl = _FakeImportDecl()
    defs = []
    last_line = 1
    for name, line_1based in names_and_lines:
        defn = _FakeImportDef()
        defn.args = [SimpleNamespace(relname=name)]
        defs.append(defn)
        last_line = line_1based
    decl.args = defs
    decl.lineno = _make_lineno(last_line)
    return decl


def _make_ast(*decls):
    """Build a fake AST object with .decls list."""
    return SimpleNamespace(decls=list(decls))


def _find_symbol(symbols, name, kind=None):
    """Find a symbol by name (and optionally kind) in a flat or nested list."""
    for sym in symbols:
        if sym.name == name and (kind is None or sym.kind == kind):
            return sym
        found = _find_symbol(sym.children, name, kind)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Export symbol tests
# ---------------------------------------------------------------------------


class TestExportSymbolExtraction:
    """ExportDecl should produce Event symbols prefixed with 'export'."""

    def test_single_export(self):
        decl = _make_export_decl([("quic.send", 5)])
        ast = _make_ast(decl)
        source = "\n".join([""] * 4 + ["export quic.send"])

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "export quic.send")
        assert sym is not None, f"Expected 'export quic.send', got {[s.name for s in symbols]}"
        assert sym.kind == SymbolKind.Event
        assert sym.file_path == "test.ivy"

    def test_export_range_is_zero_based(self):
        decl = _make_export_decl([("quic.send", 5)])
        ast = _make_ast(decl)
        source = "\n".join([""] * 4 + ["export quic.send"])

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "export quic.send")
        assert sym is not None
        # Line 5 (1-based) -> line 4 (0-based)
        assert sym.range[0] == 4, f"Expected start line 4, got {sym.range[0]}"

    def test_export_detail(self):
        decl = _make_export_decl([("quic.send", 3)])
        ast = _make_ast(decl)
        source = "\n\n\nexport quic.send"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "export quic.send")
        assert sym is not None
        assert sym.detail == "export quic.send"

    def test_multiple_exports_in_one_decl(self):
        decl = _make_export_decl([("quic.send", 3), ("quic.recv", 3)])
        ast = _make_ast(decl)
        source = "\n\nexport quic.send, quic.recv"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        assert _find_symbol(symbols, "export quic.send") is not None
        assert _find_symbol(symbols, "export quic.recv") is not None

    def test_export_with_rep_fallback(self):
        """When relname is absent, fall back to rep."""
        decl = _FakeExportDecl()
        defn = _FakeExportDef()
        defn.args = [SimpleNamespace(rep="quic.alt_send")]  # no relname
        decl.args = [defn]
        decl.lineno = _make_lineno(2)
        ast = _make_ast(decl)
        source = "\nexport quic.alt_send"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "export quic.alt_send")
        assert sym is not None

    def test_export_no_lineno_defaults_to_zero(self):
        decl = _FakeExportDecl()
        defn = _FakeExportDef()
        defn.args = [SimpleNamespace(relname="quic.send")]
        decl.args = [defn]
        # No lineno attribute
        ast = _make_ast(decl)

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", "export quic.send")

        sym = _find_symbol(symbols, "export quic.send")
        assert sym is not None
        assert sym.range[0] == 0

    def test_export_empty_args(self):
        decl = _FakeExportDecl()
        decl.args = []
        decl.lineno = _make_lineno(1)
        ast = _make_ast(decl)

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", "")

        assert len(symbols) == 0

    def test_export_def_with_no_atom_args(self):
        decl = _FakeExportDecl()
        defn = _FakeExportDef()
        defn.args = []  # empty args on the def
        decl.args = [defn]
        decl.lineno = _make_lineno(1)
        ast = _make_ast(decl)

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", "")

        assert len(symbols) == 0


# ---------------------------------------------------------------------------
# Import symbol tests
# ---------------------------------------------------------------------------


class TestImportSymbolExtraction:
    """ImportDecl should produce Event symbols prefixed with 'import'."""

    def test_single_import(self):
        decl = _make_import_decl([("tls.handshake", 7)])
        ast = _make_ast(decl)
        source = "\n".join([""] * 6 + ["import tls.handshake"])

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "import tls.handshake")
        assert sym is not None
        assert sym.kind == SymbolKind.Event
        assert sym.file_path == "test.ivy"

    def test_import_range_is_zero_based(self):
        decl = _make_import_decl([("tls.handshake", 7)])
        ast = _make_ast(decl)
        source = "\n".join([""] * 6 + ["import tls.handshake"])

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "import tls.handshake")
        assert sym is not None
        # Line 7 (1-based) -> line 6 (0-based)
        assert sym.range[0] == 6

    def test_import_detail(self):
        decl = _make_import_decl([("tls.handshake", 3)])
        ast = _make_ast(decl)
        source = "\n\nimport tls.handshake"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        sym = _find_symbol(symbols, "import tls.handshake")
        assert sym is not None
        assert sym.detail == "import tls.handshake"

    def test_multiple_imports(self):
        decl = _make_import_decl([("tls.handshake", 3), ("tls.close", 3)])
        ast = _make_ast(decl)
        source = "\n\nimport tls.handshake, tls.close"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        assert _find_symbol(symbols, "import tls.handshake") is not None
        assert _find_symbol(symbols, "import tls.close") is not None


# ---------------------------------------------------------------------------
# Mixed export + import tests
# ---------------------------------------------------------------------------


class TestMixedExportImport:
    """AST with both ExportDecl and ImportDecl produces all symbols."""

    def test_exports_and_imports_coexist(self):
        export_decl = _make_export_decl([("quic.send", 3)])
        import_decl = _make_import_decl([("tls.handshake", 5)])
        ast = _make_ast(export_decl, import_decl)
        source = "\n\nexport quic.send\n\nimport tls.handshake"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        assert _find_symbol(symbols, "export quic.send") is not None
        assert _find_symbol(symbols, "import tls.handshake") is not None

    def test_mixin_variant_still_skipped(self):
        """MixinDecl and VariantDecl must still produce no symbols."""
        mixin = _Sentinel()  # isinstance(mixin, ia.MixinDecl) -> True
        export_decl = _make_export_decl([("quic.send", 3)])
        ast = _make_ast(mixin, export_decl)
        source = "\n\nexport quic.send"

        with _patch_ivy_ast():
            from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
            symbols = ast_to_symbols(ast, "test.ivy", source)

        # MixinDecl (_Sentinel instance) produces no symbols
        # Only the export should appear
        names = [s.name for s in symbols]
        assert "export quic.send" in names
        # No other symbols from the mixin
        assert len(symbols) == 1
