"""Tests for textDocument/documentHighlight feature."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import DocumentHighlightKind, Position

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestDocumentHighlightImport:
    def test_import(self):
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        assert compute_document_highlights is not None


class TestBasicHighlight:
    def test_highlights_all_occurrences(self):
        """All occurrences of the word under cursor are highlighted."""
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        source = (
            "#lang ivy1.7\n" "type cid\n" "relation r(X:cid)\n" "action send(dst:cid)\n"
        )
        lines = source.split("\n")
        highlights = compute_document_highlights(lines, Position(line=1, character=5))
        assert len(highlights) == 3
        hl_lines = {h.range.start.line for h in highlights}
        assert hl_lines == {1, 2, 3}

    def test_no_match_returns_empty(self):
        """Cursor on whitespace returns empty list."""
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        lines = ["#lang ivy1.7", "", "type cid"]
        highlights = compute_document_highlights(lines, Position(line=1, character=0))
        assert highlights == []

    def test_definition_marked_as_write(self, tmp_path):
        """The definition occurrence is marked Write, others Read."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        source = "#lang ivy1.7\ntype cid\nrelation r(X:cid)\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        lines = source.split("\n")
        filepath = str(tmp_path / "a.ivy")
        highlights = compute_document_highlights(
            lines,
            Position(line=1, character=5),
            symbols=indexer.get_symbols(filepath),
        )
        kinds = {h.range.start.line: h.kind for h in highlights}
        assert kinds[1] == DocumentHighlightKind.Write
        assert kinds[2] == DocumentHighlightKind.Read


class TestEdgeCases:
    def test_cursor_out_of_bounds(self):
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        lines = ["#lang ivy1.7", "type cid"]
        assert compute_document_highlights(lines, Position(line=99, character=0)) == []

    def test_empty_source(self):
        from ivy_lsp.lsp.document_highlight import compute_document_highlights

        assert compute_document_highlights([], Position(line=0, character=0)) == []
