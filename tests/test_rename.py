"""Tests for textDocument/rename feature."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import Position

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestRenameImport:
    def test_import(self):
        from ivy_lsp.features.rename import compute_rename

        assert compute_rename is not None


class TestComputeRename:
    def test_rename_single_file(self, tmp_path):
        """Rename updates all occurrences in a single file."""
        from ivy_lsp.features.rename import compute_rename
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = "#lang ivy1.7\ntype cid\nrelation r(X:cid)\naction send(dst:cid)\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        lines = source.split("\n")
        filepath = str(tmp_path / "a.ivy")
        # Cursor on "cid" at line 1
        edit = compute_rename(
            indexer, filepath, Position(line=1, character=5), lines, "conn_id"
        )
        assert edit is not None
        # Should have changes for the file
        changes = edit.changes
        assert changes is not None
        file_uri = Path(filepath).as_uri()
        assert file_uri in changes
        edits = changes[file_uri]
        assert len(edits) == 3  # type cid, r(X:cid), send(dst:cid)

    def test_rename_across_files(self, tmp_path):
        """Rename updates occurrences across multiple files."""
        from ivy_lsp.features.rename import compute_rename
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (tmp_path / "main.ivy").write_text(
            "#lang ivy1.7\ninclude types\nrelation r(X:cid)\n"
        )
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        source = "#lang ivy1.7\ntype cid\n"
        lines = source.split("\n")
        filepath = str(tmp_path / "types.ivy")
        edit = compute_rename(
            indexer, filepath, Position(line=1, character=5), lines, "conn_id"
        )
        assert edit is not None
        assert edit.changes is not None
        # Both files should have edits
        assert len(edit.changes) == 2

    def test_rename_no_word_returns_none(self):
        """Cursor on whitespace returns None."""
        from ivy_lsp.features.rename import compute_rename

        lines = ["#lang ivy1.7", "", "type cid"]
        edit = compute_rename(
            None, "", Position(line=1, character=0), lines, "new_name"
        )
        assert edit is None

    def test_rename_no_matches_returns_none(self, tmp_path):
        """When no file contains the target word, return None."""
        from unittest.mock import MagicMock

        from ivy_lsp.features.rename import compute_rename

        mock_indexer = MagicMock()
        (tmp_path / "a.ivy").write_text("#lang ivy1.7\ntype pkt\n")
        mock_indexer.resolver.find_all_ivy_files.return_value = [
            str(tmp_path / "a.ivy")
        ]
        lines = ["#lang ivy1.7", "type cid"]
        edit = compute_rename(
            mock_indexer, "", Position(line=1, character=5), lines, "conn_id"
        )
        assert edit is None

    def test_rename_replaces_correct_text(self, tmp_path):
        """Verify the replacement text is the new name."""
        from ivy_lsp.features.rename import compute_rename
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = "#lang ivy1.7\ntype cid\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        lines = source.split("\n")
        filepath = str(tmp_path / "a.ivy")
        edit = compute_rename(
            indexer, filepath, Position(line=1, character=5), lines, "conn_id"
        )
        file_uri = Path(filepath).as_uri()
        for text_edit in edit.changes[file_uri]:
            assert text_edit.new_text == "conn_id"


class TestRenameErrorHandling:
    def test_rename_handles_unreadable_file(self, tmp_path):
        """OSError when reading a file is gracefully skipped."""
        from unittest.mock import MagicMock

        from ivy_lsp.features.rename import compute_rename

        mock_indexer = MagicMock()
        (tmp_path / "readable.ivy").write_text("#lang ivy1.7\ntype cid\n")
        # unreadable.ivy intentionally not created — triggers FileNotFoundError
        mock_indexer.resolver.find_all_ivy_files.return_value = [
            str(tmp_path / "readable.ivy"),
            str(tmp_path / "unreadable.ivy"),
        ]

        lines = ["#lang ivy1.7", "type cid"]
        edit = compute_rename(
            mock_indexer,
            str(tmp_path / "readable.ivy"),
            Position(line=1, character=5),
            lines,
            "conn_id",
        )
        assert edit is not None
        assert len(edit.changes) == 1

    def test_rename_handles_non_utf8_file(self, tmp_path):
        """Non-UTF-8 file is skipped without crashing."""
        from unittest.mock import MagicMock

        from ivy_lsp.features.rename import compute_rename

        mock_indexer = MagicMock()
        (tmp_path / "good.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (tmp_path / "bad.ivy").write_bytes(b"#lang ivy1.7\ntype \xff\xfe\n")
        mock_indexer.resolver.find_all_ivy_files.return_value = [
            str(tmp_path / "good.ivy"),
            str(tmp_path / "bad.ivy"),
        ]

        lines = ["#lang ivy1.7", "type cid"]
        edit = compute_rename(
            mock_indexer,
            str(tmp_path / "good.ivy"),
            Position(line=1, character=5),
            lines,
            "conn_id",
        )
        assert edit is not None


class TestRenameNotBlocking:
    """H4: rename handler must use run_in_executor for file I/O."""

    def test_rename_handler_is_async_with_executor(self):
        """Structural: verify the handler awaits run_in_executor."""
        import inspect

        from ivy_lsp.features import rename as rename_mod

        source = inspect.getsource(rename_mod.register)
        assert (
            "run_in_executor" in source
        ), "rename handler must use run_in_executor to avoid blocking event loop"
