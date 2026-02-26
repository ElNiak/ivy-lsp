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
