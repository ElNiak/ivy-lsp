"""Test that compiler cache is invalidated when files change.

Task 4: reindex_file() should call compiler_manager.invalidate_dependents()
so that stale compilation artifacts are purged when a source file changes.
"""

import os
from unittest.mock import MagicMock, patch

from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer


def _make_indexer(workspace_root="/fake/workspace"):
    """Create a WorkspaceIndexer with mocked parser and resolver."""
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer, parser, resolver


class TestCompilerCacheInvalidation:
    """reindex_file should call compiler_manager.invalidate_dependents."""

    def test_reindex_file_invalidates_compiler_cache(self):
        """When an analysis pipeline with a compiler manager is set,
        reindex_file must call invalidate_dependents on it."""
        indexer, _, _ = _make_indexer()

        # Set up a mock analysis pipeline with a mock compiler manager
        mock_compiler_manager = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline._compiler_manager = mock_compiler_manager
        indexer.set_analysis_pipeline(mock_pipeline)

        abs_path = os.path.abspath("/fake/workspace/file.ivy")

        with patch.object(indexer, "_index_single_file", return_value=[]):
            indexer.reindex_file("/fake/workspace/file.ivy")

        mock_compiler_manager.invalidate_dependents.assert_called_once_with(
            abs_path, indexer._include_graph
        )

    def test_reindex_file_skips_when_no_pipeline(self):
        """When no analysis pipeline is set, reindex_file should not crash."""
        indexer, _, _ = _make_indexer()
        # _analysis_pipeline is None by default

        with patch.object(indexer, "_index_single_file", return_value=[]):
            # Should not raise
            indexer.reindex_file("/fake/workspace/file.ivy")

    def test_reindex_file_skips_when_no_compiler_manager(self):
        """When the pipeline has no compiler manager, reindex_file should not crash."""
        indexer, _, _ = _make_indexer()

        mock_pipeline = MagicMock()
        mock_pipeline._compiler_manager = None
        indexer.set_analysis_pipeline(mock_pipeline)

        with patch.object(indexer, "_index_single_file", return_value=[]):
            # Should not raise
            indexer.reindex_file("/fake/workspace/file.ivy")

    def test_reindex_file_with_dependents_invalidates_compiler_cache(self):
        """reindex_file_with_dependents should also invalidate the compiler cache
        for every dirty file."""
        indexer, _, _ = _make_indexer()

        mock_compiler_manager = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline._compiler_manager = mock_compiler_manager
        indexer.set_analysis_pipeline(mock_pipeline)

        base = os.path.abspath("/fake/workspace/base.ivy")
        dep = os.path.abspath("/fake/workspace/dep.ivy")

        # Set up include graph: dep includes base, so changing base dirties dep
        indexer._include_graph.get_included_by = MagicMock(
            side_effect=lambda f: [dep] if f == base else []
        )

        with patch.object(indexer, "_index_single_file", return_value=[]):
            indexer.reindex_file_with_dependents("/fake/workspace/base.ivy")

        # Should be called for each dirty file
        calls = mock_compiler_manager.invalidate.call_args_list
        invalidated = {c.args[0] for c in calls}
        assert base in invalidated
        assert dep in invalidated
