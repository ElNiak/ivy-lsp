"""Tests for ivy/deepIndexProgress and ivy/testFeatureMatrix handlers."""

import threading
from unittest.mock import MagicMock

from ivy_lsp.features.monitoring import (
    handle_deep_index_progress,
    handle_test_feature_matrix,
)
from ivy_lsp.indexer.workspace_indexer import DeepIndexProgress, FileIndexStatus


class TestDeepIndexProgressHandler:
    def _make_server(self, running=False, total=5, completed=3, current="test3.ivy"):
        server = MagicMock()
        file_statuses = {
            "/ws/test1.ivy": FileIndexStatus(
                filepath="/ws/test1.ivy",
                shallow_indexed=True,
                deep_parse_attempted=True,
                deep_parse_succeeded=True,
            ),
            "/ws/test2.ivy": FileIndexStatus(
                filepath="/ws/test2.ivy",
                shallow_indexed=True,
                deep_parse_attempted=True,
                deep_parse_succeeded=False,
                parse_error="module x not found",
            ),
            "/ws/test3.ivy": FileIndexStatus(
                filepath="/ws/test3.ivy",
                shallow_indexed=True,
            ),
        }
        server.indexer = MagicMock()
        server.indexer.get_deep_index_progress.return_value = {
            "running": running,
            "total_test_files": total,
            "completed_test_files": completed,
            "current_file": current if running else None,
            "started_at": 1700000000.0 if running else None,
            "file_status_count": len(file_statuses),
            "file_statuses": dict(file_statuses),
        }
        return server

    def test_returns_progress_structure(self):
        server = self._make_server(running=True)
        result = handle_deep_index_progress(server, {"includeFileStatuses": True})
        assert result["running"] is True
        assert result["totalTests"] == 5
        assert result["completedTests"] == 3
        assert result["currentFile"] == "test3.ivy"
        assert len(result["fileStatuses"]) == 3

    def test_file_statuses_have_required_fields(self):
        server = self._make_server()
        result = handle_deep_index_progress(server, {"includeFileStatuses": True})
        for fs in result["fileStatuses"]:
            assert "file" in fs
            assert "shallowIndexed" in fs
            assert "deepParseAttempted" in fs
            assert "deepParseSucceeded" in fs
            assert "parseError" in fs

    def test_no_indexer_returns_empty(self):
        server = MagicMock()
        server.indexer = None
        result = handle_deep_index_progress(server, {"includeFileStatuses": True})
        assert result["running"] is False
        assert result["totalTests"] == 0
        assert result["fileStatuses"] == []


class TestTestFeatureMatrixHandler:
    def _make_server(self):
        server = MagicMock()
        from ivy_lsp.analysis.test_scope import ExportImportInfo

        file_statuses = {
            "/ws/test1.ivy": FileIndexStatus(
                filepath="/ws/test1.ivy",
                shallow_indexed=True,
                deep_parse_succeeded=True,
            ),
            "/ws/test2.ivy": FileIndexStatus(
                filepath="/ws/test2.ivy",
                shallow_indexed=True,
                deep_parse_succeeded=False,
            ),
        }
        export_imports = {
            "/ws/test1.ivy": ExportImportInfo(
                file="/ws/test1.ivy",
                exports=["foo"],
            ),
            "/ws/test2.ivy": ExportImportInfo(
                file="/ws/test2.ivy",
                exports=["bar"],
            ),
        }
        server.indexer = MagicMock()
        server.indexer.get_deep_index_progress.return_value = {
            "running": False,
            "total_test_files": 0,
            "completed_test_files": 0,
            "current_file": None,
            "started_at": None,
            "file_status_count": len(file_statuses),
            "file_statuses": dict(file_statuses),
        }
        server.indexer.get_file_export_imports.return_value = dict(export_imports)
        server.full_mode = True
        return server

    def test_returns_entries_for_test_files_only(self):
        server = self._make_server()
        result = handle_test_feature_matrix(server)
        assert "tests" in result
        assert len(result["tests"]) == 2

    def test_deep_parsed_file_has_ready_features(self):
        server = self._make_server()
        result = handle_test_feature_matrix(server)
        test1 = next(t for t in result["tests"] if t["file"] == "/ws/test1.ivy")
        assert test1["features"]["completion"] == "ready"
        assert test1["features"]["diagnostics"] == "ready"

    def test_shallow_only_file_has_degraded_features(self):
        server = self._make_server()
        result = handle_test_feature_matrix(server)
        test2 = next(t for t in result["tests"] if t["file"] == "/ws/test2.ivy")
        assert test2["features"]["completion"] == "degraded"
        assert test2["features"]["diagnostics"] == "unavailable"

    def test_no_indexer_returns_empty(self):
        server = MagicMock()
        server.indexer = None
        result = handle_test_feature_matrix(server)
        assert result["tests"] == []
