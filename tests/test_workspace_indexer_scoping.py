"""Tests for WorkspaceIndexer scoping integration (Task 06)."""
import logging
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from ivy_lsp.analysis.requirement_graph import RequirementGraph
from ivy_lsp.analysis.test_scope import (
    ExportImportInfo,
    ScopedRequirementModel,
    TestScope,
)
from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(workspace_root="/fake/workspace"):
    """Create a WorkspaceIndexer with mocked parser and resolver."""
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer, parser, resolver


def _make_parse_result(success=True, ast=None):
    """Create a mock parse result."""
    result = MagicMock()
    result.success = success
    result.ast = ast
    return result


def _make_export_import_info(filepath, exports=None, imports=None):
    """Create an ExportImportInfo with given exports/imports."""
    exports = exports or []
    imports = imports or []
    return ExportImportInfo(
        file=filepath,
        exports=exports,
        imports=imports,
        export_lines={e: i for i, e in enumerate(exports)},
        import_lines={im: i for i, im in enumerate(imports)},
    )


# ===================================================================
# TestInitializationWithScopedModel
# ===================================================================


class TestInitializationWithScopedModel:
    """Verify WorkspaceIndexer uses ScopedRequirementModel."""

    def test_requirement_graph_is_scoped_model(self):
        indexer, _, _ = _make_indexer()
        assert isinstance(indexer._requirement_graph, ScopedRequirementModel)

    def test_requirement_graph_is_still_a_requirement_graph(self):
        indexer, _, _ = _make_indexer()
        assert isinstance(indexer._requirement_graph, RequirementGraph)

    def test_file_export_imports_dict_exists(self):
        indexer, _, _ = _make_indexer()
        assert hasattr(indexer, "_file_export_imports")
        assert indexer._file_export_imports == {}

    def test_index_workspace_resets_to_fresh_scoped_model(self):
        indexer, _, resolver = _make_indexer()
        resolver.find_all_ivy_files.return_value = []
        indexer._requirement_graph._test_scopes["stale"] = "data"
        indexer.index_workspace()
        assert isinstance(indexer._requirement_graph, ScopedRequirementModel)
        assert indexer._requirement_graph._test_scopes == {}

    def test_index_workspace_clears_file_export_imports(self):
        indexer, _, resolver = _make_indexer()
        resolver.find_all_ivy_files.return_value = []
        indexer._file_export_imports["/old.ivy"] = _make_export_import_info("/old.ivy")
        indexer.index_workspace()
        assert indexer._file_export_imports == {}


# ===================================================================
# TestExportImportExtraction
# ===================================================================


class TestExportImportExtraction:
    """Verify _extract_file_exports_imports() delegates and stores correctly."""

    def test_full_mode_on_successful_parse(self):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=True, ast=MagicMock())
        filepath = "/fake/test.ivy"
        source = "export quic.send\n"
        expected = _make_export_import_info(filepath, exports=["quic.send"])

        with patch(
            "ivy_lsp.indexer.workspace_indexer.extract_exports_imports_full",
            return_value=expected,
        ) as mock_full:
            indexer._extract_file_exports_imports(filepath, result, source)

        mock_full.assert_called_once_with(result.ast, filepath, source)
        assert indexer._file_export_imports[filepath] is expected

    def test_light_mode_on_failed_parse(self):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=False)
        filepath = "/fake/test.ivy"
        source = "export quic.send\n"
        expected = _make_export_import_info(filepath, exports=["quic.send"])

        with patch(
            "ivy_lsp.indexer.workspace_indexer.extract_exports_imports_light",
            return_value=expected,
        ) as mock_light:
            indexer._extract_file_exports_imports(filepath, result, source)

        mock_light.assert_called_once_with(source, filepath)
        assert indexer._file_export_imports[filepath] is expected

    def test_exception_logged_not_raised(self, caplog):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=False)

        with patch(
            "ivy_lsp.indexer.workspace_indexer.extract_exports_imports_light",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING):
                indexer._extract_file_exports_imports("/bad.ivy", result, "bad")

        assert "/bad.ivy" not in indexer._file_export_imports
        assert "Export/import extraction failed" in caplog.text

    def test_called_during_index_single_file(self, tmp_path):
        """After _index_single_file, the file's ExportImportInfo is stored."""
        source = "export quic_send_event\ntype t\n"
        f = tmp_path / "test.ivy"
        f.write_text(source)
        filepath = str(f)

        indexer, parser, _ = _make_indexer(str(tmp_path))
        parser.parse.return_value = _make_parse_result(success=False)

        indexer._index_single_file(filepath)

        assert filepath in indexer._file_export_imports
        info = indexer._file_export_imports[filepath]
        assert "quic_send_event" in info.exports
