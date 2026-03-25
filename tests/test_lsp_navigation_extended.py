"""Extended LSP navigation tests (goToDefinition, findReferences, hover, documentSymbol).

Tests the core LSP feature functions directly (not through the server),
covering scenarios from the testing plan Parts 1A-1E.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.parsing.symbols import IvySymbol  # noqa: E402
from ivy_lsp.features.definition import (  # noqa: E402
    _DECL_RE,
    _INCLUDE_RE,
    goto_definition,
)
from ivy_lsp.features.document_symbols import (  # noqa: E402
    get_document_symbols,
    ivy_symbol_to_document_symbol,
)
from ivy_lsp.features.hover import format_hover_content  # noqa: E402
from ivy_lsp.features.references import find_references  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(lookup_results=None, resolve_result=None, all_files=None):
    """Create a mock indexer with configurable symbol lookup."""
    indexer = MagicMock()
    indexer.lookup_symbol.return_value = lookup_results or []
    indexer.resolver.resolve.return_value = resolve_result
    indexer.resolver.find_all_ivy_files.return_value = all_files or []
    return indexer


class _FakeSymbolResult:
    """Mimics the result object returned by indexer.lookup_symbol."""

    def __init__(self, filepath, start_line=0, start_char=0, end_line=0, end_char=5):
        self.filepath = filepath
        self.range = (start_line, start_char, end_line, end_char)


# ---------------------------------------------------------------------------
# goToDefinition
# ---------------------------------------------------------------------------


class TestGoToDefinitionSimple:
    def test_goto_def_simple_type(self):
        """lookup_symbol returns one result -> single Location."""
        result = _FakeSymbolResult("/a/types.ivy", 2, 5, 2, 8)
        indexer = _make_indexer(lookup_results=[result])
        source = "#lang ivy1.7\n\nrelation r(X:cid)\n".split("\n")
        pos = lsp.Position(line=2, character=14)  # cursor on "cid"

        loc = goto_definition(indexer, "/a/conn.ivy", pos, source)
        assert isinstance(loc, lsp.Location)

    def test_goto_def_dotted_name_fallback(self):
        """Dotted name lookup fails, falls back to last component."""
        result = _FakeSymbolResult("/a/conn.ivy", 5, 0, 5, 4)
        indexer = _make_indexer()
        # First call with "quic_stack.send" returns empty, second with "send" returns result
        indexer.lookup_symbol.side_effect = [[], [result]]
        source = "#lang ivy1.7\n\nquic_stack.send\n".split("\n")
        pos = lsp.Position(line=2, character=12)  # on "send" part

        loc = goto_definition(indexer, "/a/test.ivy", pos, source)
        assert loc is not None
        assert indexer.lookup_symbol.call_count == 2
        # Second call should be with "send" (last component)
        assert indexer.lookup_symbol.call_args_list[1][0][0] == "send"

    def test_goto_def_include_line(self, tmp_path):
        """Cursor on include module name -> navigate to included file."""
        target_file = tmp_path / "quic_types.ivy"
        target_file.write_text("#lang ivy1.7\ntype cid\n")
        indexer = _make_indexer(resolve_result=str(target_file))
        source = "#lang ivy1.7\n\ninclude quic_types\n".split("\n")
        pos = lsp.Position(line=2, character=10)  # on "quic_types"

        loc = goto_definition(indexer, str(tmp_path / "conn.ivy"), pos, source)
        assert isinstance(loc, lsp.Location)
        assert loc.range.start.line == 0

    def test_goto_def_self_declaration(self):
        """Cursor on a declaration keyword -> self-location returned."""
        indexer = _make_indexer()
        source = "#lang ivy1.7\n\ntype cid\n".split("\n")
        pos = lsp.Position(line=2, character=5)  # on "cid" in "type cid"

        loc = goto_definition(indexer, "/a/types.ivy", pos, source)
        assert isinstance(loc, lsp.Location)
        assert loc.range.start.line == 2

    def test_goto_def_nonexistent_symbol(self):
        """No symbol found, not a declaration -> returns None."""
        indexer = _make_indexer()
        source = "#lang ivy1.7\n\nrequire nonexistent_xyz;\n".split("\n")
        pos = lsp.Position(line=2, character=10)

        loc = goto_definition(indexer, "/a/test.ivy", pos, source)
        assert loc is None

    def test_goto_def_multiple_definitions(self):
        """Multiple definitions -> returns list of Locations."""
        r1 = _FakeSymbolResult("/a/types1.ivy", 2, 5, 2, 8)
        r2 = _FakeSymbolResult("/a/types2.ivy", 3, 5, 3, 8)
        indexer = _make_indexer(lookup_results=[r1, r2])
        source = "#lang ivy1.7\n\nrelation r(X:cid)\n".split("\n")
        pos = lsp.Position(line=2, character=14)

        loc = goto_definition(indexer, "/a/conn.ivy", pos, source)
        assert isinstance(loc, list)
        assert len(loc) == 2

    def test_goto_def_empty_word(self):
        """Cursor on whitespace -> None."""
        indexer = _make_indexer()
        source = "#lang ivy1.7\n\n   \n".split("\n")
        pos = lsp.Position(line=2, character=1)

        loc = goto_definition(indexer, "/a/test.ivy", pos, source)
        assert loc is None


# ---------------------------------------------------------------------------
# findReferences
# ---------------------------------------------------------------------------


class TestFindReferences:
    def test_refs_all_occurrences(self, tmp_path):
        """Symbol used on 3 lines -> 3 locations."""
        f = tmp_path / "test.ivy"
        f.write_text(
            "#lang ivy1.7\ntype cid\nrelation r(X:cid)\naction send(dst:cid)\n"
        )
        indexer = _make_indexer(all_files=[str(f)])
        source = f.read_text().split("\n")
        pos = lsp.Position(line=1, character=5)  # on "cid" in "type cid"

        locs = find_references(indexer, str(f), pos, source)
        assert len(locs) == 3

    def test_refs_exclude_declaration(self, tmp_path):
        """include_declaration=False -> skips cursor position."""
        f = tmp_path / "test.ivy"
        f.write_text("#lang ivy1.7\ntype cid\nrelation r(X:cid)\n")
        indexer = _make_indexer(all_files=[str(f)])
        source = f.read_text().split("\n")
        pos = lsp.Position(line=1, character=5)  # on "cid" in "type cid"

        locs_all = find_references(
            indexer, str(f), pos, source, include_declaration=True
        )
        locs_no_decl = find_references(
            indexer, str(f), pos, source, include_declaration=False
        )
        assert len(locs_no_decl) < len(locs_all)

    def test_refs_cross_file(self, tmp_path):
        """Symbol in two files -> both found."""
        f1 = tmp_path / "types.ivy"
        f1.write_text("#lang ivy1.7\ntype cid\n")
        f2 = tmp_path / "conn.ivy"
        f2.write_text("#lang ivy1.7\nrelation r(X:cid)\n")
        indexer = _make_indexer(all_files=[str(f1), str(f2)])
        source = f1.read_text().split("\n")
        pos = lsp.Position(line=1, character=5)

        locs = find_references(indexer, str(f1), pos, source)
        # cid appears in both files
        assert len(locs) >= 2

    def test_refs_word_boundary(self, tmp_path):
        """'cid' does NOT match 'acid' or 'cider'."""
        f = tmp_path / "test.ivy"
        f.write_text("#lang ivy1.7\ntype cid\ntype acid\ntype cider\n")
        indexer = _make_indexer(all_files=[str(f)])
        source = f.read_text().split("\n")
        pos = lsp.Position(line=1, character=5)

        locs = find_references(indexer, str(f), pos, source)
        assert len(locs) == 1  # only "cid", not "acid" or "cider"

    def test_refs_empty_word(self, tmp_path):
        """Cursor on whitespace -> empty list."""
        f = tmp_path / "test.ivy"
        f.write_text("#lang ivy1.7\n   \ntype cid\n")
        indexer = _make_indexer(all_files=[str(f)])
        source = f.read_text().split("\n")
        pos = lsp.Position(line=1, character=1)  # on whitespace

        locs = find_references(indexer, str(f), pos, source)
        assert locs == []


# ---------------------------------------------------------------------------
# hover
# ---------------------------------------------------------------------------


class TestHoverFormatting:
    def test_hover_type_declaration(self):
        """Type symbol -> markdown with 'type cid'."""
        sym = IvySymbol(
            name="cid",
            kind=lsp.SymbolKind.Class,
            range=(2, 0, 2, 8),
            detail="",
        )
        result = format_hover_content(sym)
        assert result is not None
        assert "type cid" in result

    def test_hover_action_with_params(self):
        """Action with params -> shows param signature."""
        sym = IvySymbol(
            name="send",
            kind=lsp.SymbolKind.Method,
            range=(5, 0, 5, 20),
            detail="(src:cid, dst:cid)",
        )
        result = format_hover_content(sym)
        assert result is not None
        assert "action send(src:cid, dst:cid)" in result

    def test_hover_enum_type(self):
        """Enum type -> shows {variants}."""
        sym = IvySymbol(
            name="stream_kind",
            kind=lsp.SymbolKind.Class,
            range=(2, 0, 2, 30),
            detail="enum:unidir, bidir",
        )
        result = format_hover_content(sym)
        assert result is not None
        assert "{unidir, bidir}" in result

    def test_hover_with_filepath(self):
        """Symbol with file_path -> 'Defined in: filename'."""
        sym = IvySymbol(
            name="cid",
            kind=lsp.SymbolKind.Class,
            range=(2, 0, 2, 8),
            detail="",
        )
        sym.file_path = "/a/b/types.ivy"
        result = format_hover_content(sym)
        assert result is not None
        assert "Defined in: types.ivy" in result

    def test_hover_none_symbol(self):
        """None symbol -> None."""
        assert format_hover_content(None) is None

    def test_hover_relation(self):
        """Relation symbol -> 'relation name(params)'."""
        sym = IvySymbol(
            name="connected",
            kind=lsp.SymbolKind.Function,
            range=(8, 0, 8, 30),
            detail="relation (X:cid, Y:cid)",
        )
        result = format_hover_content(sym)
        assert result is not None
        assert "relation" in result
        assert "connected" in result


# ---------------------------------------------------------------------------
# documentSymbol
# ---------------------------------------------------------------------------


class TestDocumentSymbolConversion:
    def test_doc_symbols_with_children(self):
        """IvySymbol with children -> DocumentSymbol with children."""
        child = IvySymbol(
            name="zero",
            kind=lsp.SymbolKind.Variable,
            range=(3, 4, 3, 20),
            detail="",
        )
        parent = IvySymbol(
            name="bit",
            kind=lsp.SymbolKind.Module,
            range=(2, 0, 5, 1),
            detail="",
            children=[child],
        )
        doc_sym = ivy_symbol_to_document_symbol(parent)
        assert doc_sym.name == "bit"
        assert doc_sym.children is not None
        assert len(doc_sym.children) == 1
        assert doc_sym.children[0].name == "zero"

    def test_doc_symbols_no_children(self):
        """IvySymbol without children -> children=None."""
        sym = IvySymbol(
            name="cid",
            kind=lsp.SymbolKind.Class,
            range=(2, 0, 2, 8),
            detail="",
        )
        doc_sym = ivy_symbol_to_document_symbol(sym)
        assert doc_sym.name == "cid"
        assert doc_sym.children is None

    def test_doc_symbols_all_kinds(self):
        """Various SymbolKinds convert without error."""
        kinds = [
            lsp.SymbolKind.Class,
            lsp.SymbolKind.Function,
            lsp.SymbolKind.Module,
            lsp.SymbolKind.Variable,
            lsp.SymbolKind.Property,
            lsp.SymbolKind.Namespace,
        ]
        for kind in kinds:
            sym = IvySymbol(
                name=f"sym_{kind.value}",
                kind=kind,
                range=(0, 0, 0, 10),
                detail="",
            )
            doc_sym = ivy_symbol_to_document_symbol(sym)
            assert doc_sym.name == f"sym_{kind.value}"

    def test_get_document_symbols_empty(self):
        """None/empty input -> empty list."""
        assert get_document_symbols(None) == []
        assert get_document_symbols([]) == []


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


class TestRegexPatterns:
    def test_include_re_basic(self):
        assert _INCLUDE_RE.match("include types") is not None

    def test_include_re_with_indent(self):
        m = _INCLUDE_RE.match("  include order")
        assert m is not None
        assert m.group(1) == "order"

    def test_include_re_non_include(self):
        assert _INCLUDE_RE.match("action send") is None

    def test_decl_re_all_keywords(self):
        for kw in [
            "action",
            "relation",
            "function",
            "individual",
            "type",
            "module",
            "object",
            "isolate",
        ]:
            m = _DECL_RE.match(f"{kw} my_name")
            assert m is not None, f"Failed for keyword: {kw}"
            assert m.group(1) == "my_name"

    def test_decl_re_non_declaration(self):
        assert _DECL_RE.match("require x > 0") is None
        assert _DECL_RE.match("ensure true") is None
