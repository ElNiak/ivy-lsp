"""Tests for textDocument/foldingRange feature."""

import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestFoldingRangeImport:
    def test_import(self):
        from ivy_lsp.features.folding_range import compute_folding_ranges

        assert compute_folding_ranges is not None


class TestBraceBlockFolding:
    def test_single_brace_block(self):
        """Brace block spanning multiple lines produces a fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = "#lang ivy1.7\n" "\n" "object foo = {\n" "    type this\n" "}\n"
        ranges = compute_folding_ranges(source)
        brace_folds = [r for r in ranges if r.kind == "region"]
        assert len(brace_folds) >= 1
        fold = brace_folds[0]
        assert fold.start_line == 2
        assert fold.end_line == 4

    def test_nested_brace_blocks(self):
        """Nested braces produce nested folds."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = (
            "#lang ivy1.7\n"
            "object outer = {\n"
            "    object inner = {\n"
            "        type this\n"
            "    }\n"
            "}\n"
        )
        ranges = compute_folding_ranges(source)
        brace_folds = [r for r in ranges if r.kind == "region"]
        assert len(brace_folds) == 2
        outer = [f for f in brace_folds if f.start_line == 1]
        assert len(outer) == 1
        assert outer[0].end_line == 5
        inner = [f for f in brace_folds if f.start_line == 2]
        assert len(inner) == 1
        assert inner[0].end_line == 4

    def test_no_fold_for_single_line_brace(self):
        """Single-line brace block does not produce a fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = "#lang ivy1.7\ntype foo = { a, b, c }\n"
        ranges = compute_folding_ranges(source)
        brace_folds = [r for r in ranges if r.kind == "region"]
        assert len(brace_folds) == 0


class TestCommentFolding:
    def test_consecutive_comments(self):
        """Three or more consecutive comment lines produce a comment fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = (
            "#lang ivy1.7\n"
            "\n"
            "# This is a block\n"
            "# of consecutive\n"
            "# comments\n"
            "type cid\n"
        )
        ranges = compute_folding_ranges(source)
        comment_folds = [r for r in ranges if r.kind == "comment"]
        assert len(comment_folds) == 1
        assert comment_folds[0].start_line == 2
        assert comment_folds[0].end_line == 4


class TestIncludeFolding:
    def test_consecutive_includes(self):
        """Consecutive include directives produce an imports fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = (
            "#lang ivy1.7\n"
            "\n"
            "include quic_types\n"
            "include quic_frame\n"
            "include quic_packet\n"
            "\n"
            "type cid\n"
        )
        ranges = compute_folding_ranges(source)
        import_folds = [r for r in ranges if r.kind == "imports"]
        assert len(import_folds) == 1
        assert import_folds[0].start_line == 2
        assert import_folds[0].end_line == 4


class TestUnmatchedBraces:
    def test_unmatched_opening_brace(self):
        """Unclosed brace produces no fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = "#lang ivy1.7\nobject foo = {\n    type this\n"
        ranges = compute_folding_ranges(source)
        brace_folds = [r for r in ranges if r.kind == "region"]
        assert len(brace_folds) == 0

    def test_unmatched_closing_brace(self):
        """Extra closing brace produces no fold."""
        from ivy_lsp.features.folding_range import compute_folding_ranges

        source = "#lang ivy1.7\n}\ntype cid\n"
        ranges = compute_folding_ranges(source)
        brace_folds = [r for r in ranges if r.kind == "region"]
        assert len(brace_folds) == 0


class TestEmptySource:
    def test_empty_string(self):
        from ivy_lsp.features.folding_range import compute_folding_ranges

        assert compute_folding_ranges("") == []
