"""Tests for deep index progress tracking data structures."""

import threading
import time
from unittest.mock import MagicMock, patch

from ivy_lsp.indexer.workspace_indexer import (
    DeepIndexProgress,
    FileIndexStatus,
    WorkspaceIndexer,
)


class TestFileIndexStatus:
    def test_defaults(self):
        s = FileIndexStatus(filepath="/tmp/a.ivy")
        assert s.filepath == "/tmp/a.ivy"
        assert s.shallow_indexed is False
        assert s.deep_parse_attempted is False
        assert s.deep_parse_succeeded is False
        assert s.last_indexed_at is None
        assert s.parse_error is None

    def test_shallow_indexed(self):
        s = FileIndexStatus(
            filepath="/tmp/a.ivy",
            shallow_indexed=True,
            last_indexed_at=time.time(),
        )
        assert s.shallow_indexed is True
        assert s.last_indexed_at is not None

    def test_deep_parse_with_error(self):
        s = FileIndexStatus(
            filepath="/tmp/a.ivy",
            deep_parse_attempted=True,
            deep_parse_succeeded=False,
            parse_error="module foo not found",
        )
        assert s.deep_parse_attempted is True
        assert not s.deep_parse_succeeded
        assert s.parse_error == "module foo not found"


class TestDeepIndexProgress:
    def test_defaults(self):
        p = DeepIndexProgress()
        assert p.total_test_files == 0
        assert p.completed_test_files == 0
        assert p.current_file is None
        assert p.started_at is None
        assert p.file_statuses == {}

    def test_tracking_progress(self):
        p = DeepIndexProgress()
        p.total_test_files = 3
        p.started_at = time.time()
        p.current_file = "/tmp/test1.ivy"
        p.file_statuses["/tmp/test1.ivy"] = FileIndexStatus(
            filepath="/tmp/test1.ivy",
            shallow_indexed=True,
        )
        assert p.total_test_files == 3
        assert len(p.file_statuses) == 1


class TestWorkspaceIndexerProgressTracking:
    def _make_indexer(self, tmp_path):
        parser = MagicMock()
        parser.parse.return_value = MagicMock(success=False, ast=None, errors=[])
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = []
        return WorkspaceIndexer(str(tmp_path), parser, resolver)

    def test_has_progress_and_lock(self, tmp_path):
        idx = self._make_indexer(tmp_path)
        assert hasattr(idx, "_deep_index_progress")
        assert isinstance(idx._deep_index_progress, DeepIndexProgress)
        assert hasattr(idx, "_progress_lock")

    def test_shallow_index_updates_file_status(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        parser = MagicMock()
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = [str(f)]
        resolver.resolve.return_value = None

        idx = WorkspaceIndexer(str(tmp_path), parser, resolver)
        idx._fast_index_all_files()

        status = idx._deep_index_progress.file_statuses.get(str(f))
        assert status is not None
        assert status.shallow_indexed is True
        assert status.last_indexed_at is not None

    def test_deep_index_updates_progress(self, tmp_path):
        f = tmp_path / "test.ivy"
        f.write_text("#lang ivy1.7\nexport action foo\n")

        mock_result = MagicMock(success=True, ast=MagicMock(), errors=[])
        parser = MagicMock()
        parser.parse.return_value = mock_result
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = [str(f)]
        resolver.resolve.return_value = None

        idx = WorkspaceIndexer(str(tmp_path), parser, resolver)
        from ivy_lsp.analysis.test_scope import ExportImportInfo

        idx._file_export_imports[str(f)] = ExportImportInfo(
            file=str(f),
            exports=["foo"],
        )

        with patch("ivy_lsp.parsing.ast_to_symbols.ast_to_symbols", return_value=[]):
            idx._deep_index_from_tests()

        progress = idx._deep_index_progress
        assert progress.total_test_files == 1
        assert progress.completed_test_files == 1
        assert progress.current_file is None  # Reset after loop
        status = progress.file_statuses.get(str(f))
        assert status is not None
        assert status.deep_parse_attempted is True
        assert status.deep_parse_succeeded is True
