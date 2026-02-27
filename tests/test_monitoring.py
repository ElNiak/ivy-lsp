"""Tests for monitoring request handlers."""

import pytest
from unittest.mock import MagicMock

from ivy_lsp.features.status import ServerStateTracker
from ivy_lsp.features.monitoring import (
    handle_server_status,
    handle_indexer_stats,
    handle_operation_history,
    handle_include_graph,
    handle_reindex,
    handle_clear_cache,
    handle_feature_status,
)


@pytest.fixture
def mock_server():
    server = MagicMock()
    server.state_tracker = ServerStateTracker()
    server._full_mode = True
    server._indexer = MagicMock()
    server._indexer.get_stats.return_value = MagicMock(
        file_count=10,
        symbol_count=100,
        include_edge_count=5,
        test_scope_count=2,
        per_file_errors=[],
        stale_files=[],
        last_index_time="2026-01-01T00:00:00+00:00",
        last_index_duration=1.5,
    )
    server._indexer._include_graph = MagicMock()
    server._indexer._include_graph._includes = {
        "a.ivy": {"b.ivy"},
        "b.ivy": {"c.ivy"},
    }
    server._indexer._include_graph._included_by = {
        "b.ivy": {"a.ivy"},
        "c.ivy": {"b.ivy"},
    }
    server._indexer.get_symbols.return_value = [MagicMock(), MagicMock()]
    return server


class TestServerStatus:
    def test_returns_mode_and_version(self, mock_server):
        result = handle_server_status(mock_server)
        assert result["mode"] == "full"
        assert "version" in result
        assert "uptimeSeconds" in result
        assert result["indexingState"] == "idle"

    def test_light_mode(self, mock_server):
        mock_server._full_mode = False
        result = handle_server_status(mock_server)
        assert result["mode"] == "light"

    def test_includes_active_operations(self, mock_server):
        mock_server.state_tracker.operation_tracker.record_start(
            "verify", "test.ivy"
        )
        result = handle_server_status(mock_server)
        assert len(result["activeOperations"]) == 1
        assert result["activeOperations"][0]["type"] == "verify"


class TestIndexerStats:
    def test_returns_stats(self, mock_server):
        result = handle_indexer_stats(mock_server)
        assert result["fileCount"] == 10
        assert result["symbolCount"] == 100
        assert result["includeEdgeCount"] == 5
        assert result["testScopeCount"] == 2

    def test_no_indexer_returns_zeros(self, mock_server):
        mock_server._indexer = None
        result = handle_indexer_stats(mock_server)
        assert result["fileCount"] == 0


class TestOperationHistory:
    def test_returns_history(self, mock_server):
        tracker = mock_server.state_tracker.operation_tracker
        op_id = tracker.record_start("verify", "test.ivy")
        tracker.record_end(op_id, success=True, message="OK", duration=2.0)
        result = handle_operation_history(mock_server)
        assert len(result["operations"]) == 1
        assert result["operations"][0]["type"] == "verify"
        assert result["operations"][0]["success"] is True

    def test_empty_history(self, mock_server):
        result = handle_operation_history(mock_server)
        assert result["operations"] == []


class TestIncludeGraph:
    def test_returns_nodes_and_edges(self, mock_server):
        result = handle_include_graph(mock_server)
        assert len(result["nodes"]) > 0
        assert len(result["edges"]) > 0

    def test_no_indexer_returns_empty(self, mock_server):
        mock_server._indexer = None
        result = handle_include_graph(mock_server)
        assert result["nodes"] == []
        assert result["edges"] == []


class TestReindex:
    def test_triggers_reindex(self, mock_server):
        result = handle_reindex(mock_server)
        assert result["success"] is True
        mock_server._indexer.reindex.assert_called_once()

    def test_no_indexer_returns_error(self, mock_server):
        mock_server._indexer = None
        result = handle_reindex(mock_server)
        assert result["success"] is False


class TestClearCache:
    def test_clears_and_reindexes(self, mock_server):
        mock_server._indexer._staging_dir = "/tmp/staging"
        result = handle_clear_cache(mock_server)
        assert result["success"] is True

    def test_no_indexer_returns_error(self, mock_server):
        mock_server._indexer = None
        result = handle_clear_cache(mock_server)
        assert result["success"] is False


class TestFeatureStatus:
    """Tests for the ivy/featureStatus endpoint handler."""

    def _make_server(self, full_mode=True, has_indexer=True, has_pipeline=True,
                     indexing_state="idle", has_parser=True):
        """Build a mock server with configurable feature availability."""
        server = MagicMock()
        server.state_tracker = ServerStateTracker()
        server._full_mode = full_mode
        if indexing_state == "indexing":
            server.state_tracker.set_indexing()
        elif indexing_state == "error":
            server.state_tracker.set_index_error("index failed")
        server._parser = MagicMock() if has_parser else None
        if has_indexer:
            server._indexer = MagicMock()
            server._indexer._requirement_graph = MagicMock()
        else:
            server._indexer = None
        if has_pipeline:
            server._semantic_model = MagicMock()
            server._semantic_model.node_count.return_value = 10
            server._semantic_model.edge_count.return_value = 5
            pipeline = MagicMock()
            pipeline.get_pipeline_state.return_value = {
                "tier1FileCount": 3, "tier2FileCount": 2, "tier3FileCount": 0,
                "tier3Running": False, "semanticNodeCount": 10,
                "semanticEdgeCount": 5, "semanticModelReady": True,
            }
            server._analysis_pipeline = pipeline
        else:
            server._semantic_model = None
            server._analysis_pipeline = None
        return server

    def test_returns_features_and_pipeline(self):
        server = self._make_server()
        result = handle_feature_status(server)
        assert "features" in result
        assert "analysisPipeline" in result
        assert isinstance(result["features"], list)
        assert len(result["features"]) >= 4

    def test_all_ready_in_full_mode_with_indexer(self):
        server = self._make_server(full_mode=True, has_indexer=True)
        result = handle_feature_status(server)
        statuses = {f["id"]: f["status"] for f in result["features"]}
        assert statuses["codeLens"] == "ready"
        assert statuses["diagnostics"] == "ready"
        assert statuses["navigation"] == "ready"

    def test_light_mode_degrades_diagnostics(self):
        server = self._make_server(full_mode=False)
        result = handle_feature_status(server)
        statuses = {f["id"]: f["status"] for f in result["features"]}
        assert statuses["diagnostics"] == "degraded"

    def test_no_indexer_disables_codelens_and_navigation(self):
        server = self._make_server(has_indexer=False)
        result = handle_feature_status(server)
        statuses = {f["id"]: f["status"] for f in result["features"]}
        assert statuses["codeLens"] == "unavailable"
        assert statuses["navigation"] == "unavailable"

    def test_indexing_in_progress_shows_loading(self):
        server = self._make_server(indexing_state="indexing")
        result = handle_feature_status(server)
        statuses = {f["id"]: f["status"] for f in result["features"]}
        assert statuses["codeLens"] == "loading"
        assert statuses["navigation"] == "loading"

    def test_no_pipeline_disables_semantic_and_rfc(self):
        server = self._make_server(has_pipeline=False)
        result = handle_feature_status(server)
        statuses = {f["id"]: f["status"] for f in result["features"]}
        assert statuses["semanticAnalysis"] == "unavailable"
        assert statuses["rfcCoverage"] == "unavailable"

    def test_pipeline_state_forwarded(self):
        server = self._make_server()
        result = handle_feature_status(server)
        ps = result["analysisPipeline"]
        assert ps["tier1FileCount"] == 3
        assert ps["semanticModelReady"] is True

    def test_each_feature_has_required_fields(self):
        server = self._make_server()
        result = handle_feature_status(server)
        for f in result["features"]:
            assert "id" in f
            assert "name" in f
            assert "status" in f
            assert f["status"] in ("ready", "degraded", "unavailable", "loading")
            assert "reason" in f
