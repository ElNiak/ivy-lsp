"""Tests for deep index progress tracking data structures."""
import time

from ivy_lsp.indexer.workspace_indexer import FileIndexStatus, DeepIndexProgress


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
            filepath="/tmp/test1.ivy", shallow_indexed=True,
        )
        assert p.total_test_files == 3
        assert len(p.file_statuses) == 1
