"""Tests for textDocument/selectionRange feature."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import Position

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestSelectionRangeImport:
    def test_import(self):
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        assert compute_selection_ranges is not None


class TestSelectionChain:
    def test_word_then_line_then_block(self):
        """Selection expands: word -> line -> brace block -> file."""
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        source = "#lang ivy1.7\n" "object foo = {\n" "    type this\n" "}\n"
        lines = source.split("\n")
        # Cursor on "this" in line 2
        results = compute_selection_ranges(lines, [Position(line=2, character=9)])
        assert len(results) == 1
        sr = results[0]

        # Innermost: word "this"
        assert sr.range.start.line == 2
        assert sr.range.start.character == 9
        assert sr.range.end.character == 13

        # Next: full line
        assert sr.parent is not None
        assert sr.parent.range.start.line == 2
        assert sr.parent.range.start.character == 0

        # Next: brace block (lines 1-3)
        assert sr.parent.parent is not None
        block = sr.parent.parent
        assert block.range.start.line == 1
        assert block.range.end.line == 3

        # Outermost: whole file
        assert block.parent is not None
        assert block.parent.range.start.line == 0

    def test_cursor_on_whitespace(self):
        """Cursor on empty line: line -> file."""
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        lines = ["#lang ivy1.7", "", "type cid"]
        results = compute_selection_ranges(lines, [Position(line=1, character=0)])
        assert len(results) == 1
        sr = results[0]
        # Line range
        assert sr.range.start.line == 1
        # Parent is file
        assert sr.parent is not None
        assert sr.parent.range.start.line == 0

    def test_multiple_positions(self):
        """Multiple positions return one SelectionRange each."""
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        lines = ["#lang ivy1.7", "type cid", "type pkt"]
        results = compute_selection_ranges(
            lines,
            [Position(line=1, character=5), Position(line=2, character=5)],
        )
        assert len(results) == 2


class TestEdgeCases:
    def test_empty_source_lines(self):
        """Empty source_lines should not crash."""
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        results = compute_selection_ranges([], [Position(line=0, character=0)])
        assert len(results) == 1
        sr = results[0]
        assert sr.range.start.line == 0

    def test_dotted_identifier(self):
        """Cursor on a dotted identifier highlights the last component."""
        from ivy_lsp.lsp.ui.selection_range import compute_selection_ranges

        lines = ["#lang ivy1.7", "relation frame.ack"]
        results = compute_selection_ranges(lines, [Position(line=1, character=15)])
        assert len(results) == 1
        sr = results[0]
        # Innermost should be a word range on the last component
        assert sr.range.start.line == 1
        # After fix, should select just "ack" (15-18), not "frame.ack" (9-18)
        assert sr.range.start.character == 15
        assert sr.range.end.character == 18
