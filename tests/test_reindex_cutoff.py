"""Tests for reindex_file export-hash cut-off optimization.

When a file is reindexed but its exported symbols have not changed,
the expensive _wire_requirement_graph() and _compute_test_scopes()
calls should be skipped.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from ivy_lsp.core.analysis.test_scope import ExportImportInfo
from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(workspace_root="/fake/workspace"):
    """Create a WorkspaceIndexer with mocked parser and resolver."""
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    resolver.collision_map = {}
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer


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
# TestReindexCutoffOptimization
# ===================================================================


class TestReindexCutoffOptimization:
    """Verify that reindex_file skips re-wiring when exports are unchanged."""

    def test_reindex_skips_rewiring_when_exports_unchanged(self):
        """reindex_file should skip _wire_requirement_graph when exports unchanged."""
        indexer = _make_indexer()
        target = os.path.abspath("/fake/workspace/test.ivy")
        info = _make_export_import_info(
            target, exports=["quic.send"], imports=["tls.hs"]
        )

        # Simulate first index: populate _file_export_imports so a hash is stored.
        # We mock _index_single_file to restore the same ExportImportInfo.
        def fake_index_single_file_same(filepath):
            indexer._file_export_imports[filepath] = info
            return []

        with patch.object(
            indexer, "_index_single_file", side_effect=fake_index_single_file_same
        ):
            # First reindex: exports are new, so wiring MUST happen
            indexer.reindex_file(target)

        # Now reindex again with same exports.
        # This time, _wire_requirement_graph should NOT be called.
        with patch.object(
            indexer, "_index_single_file", side_effect=fake_index_single_file_same
        ):
            with patch.object(indexer, "_wire_requirement_graph") as mock_wire:
                with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                    indexer.reindex_file(target)

        mock_wire.assert_not_called()
        mock_compute.assert_not_called()

    def test_reindex_rewires_when_exports_change(self):
        """reindex_file should call _wire_requirement_graph when exports change."""
        indexer = _make_indexer()
        target = os.path.abspath("/fake/workspace/test.ivy")

        info_v1 = _make_export_import_info(target, exports=["quic.send"])
        info_v2 = _make_export_import_info(target, exports=["quic.send", "quic.recv"])

        def fake_index_v1(filepath):
            indexer._file_export_imports[filepath] = info_v1
            return []

        def fake_index_v2(filepath):
            indexer._file_export_imports[filepath] = info_v2
            return []

        # First reindex: establishes baseline hash
        with patch.object(indexer, "_index_single_file", side_effect=fake_index_v1):
            indexer.reindex_file(target)

        # Second reindex: different exports -> must re-wire
        with patch.object(indexer, "_index_single_file", side_effect=fake_index_v2):
            with patch.object(indexer, "_wire_requirement_graph") as mock_wire:
                with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                    indexer.reindex_file(target)

        mock_wire.assert_called_once()
        mock_compute.assert_called_once()

    def test_reindex_rewires_on_first_index(self):
        """reindex_file should always wire on the very first indexing of a file."""
        indexer = _make_indexer()
        target = os.path.abspath("/fake/workspace/new_file.ivy")

        info = _make_export_import_info(target, exports=["quic.send"])

        def fake_index(filepath):
            indexer._file_export_imports[filepath] = info
            return []

        with patch.object(indexer, "_index_single_file", side_effect=fake_index):
            with patch.object(indexer, "_wire_requirement_graph") as mock_wire:
                with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                    indexer.reindex_file(target)

        mock_wire.assert_called_once()
        mock_compute.assert_called_once()

    def test_reindex_rewires_when_export_import_info_missing(self):
        """If _index_single_file fails to produce ExportImportInfo, always re-wire."""
        indexer = _make_indexer()
        target = os.path.abspath("/fake/workspace/broken.ivy")

        def fake_index_no_exports(filepath):
            # Simulate extraction failure: no entry in _file_export_imports
            return []

        # First call: no exports info -> should re-wire (conservative)
        with patch.object(
            indexer, "_index_single_file", side_effect=fake_index_no_exports
        ):
            with patch.object(indexer, "_wire_requirement_graph") as mock_wire:
                with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                    indexer.reindex_file(target)

        mock_wire.assert_called_once()
        mock_compute.assert_called_once()

    def test_reindex_rewires_when_imports_change(self):
        """Changes in imports (not just exports) should trigger re-wiring."""
        indexer = _make_indexer()
        target = os.path.abspath("/fake/workspace/test.ivy")

        info_v1 = _make_export_import_info(
            target, exports=["quic.send"], imports=["tls.hs"]
        )
        info_v2 = _make_export_import_info(
            target, exports=["quic.send"], imports=["tls.hs", "tls.cert"]
        )

        def fake_index_v1(filepath):
            indexer._file_export_imports[filepath] = info_v1
            return []

        def fake_index_v2(filepath):
            indexer._file_export_imports[filepath] = info_v2
            return []

        # First reindex
        with patch.object(indexer, "_index_single_file", side_effect=fake_index_v1):
            indexer.reindex_file(target)

        # Second reindex: imports changed
        with patch.object(indexer, "_index_single_file", side_effect=fake_index_v2):
            with patch.object(indexer, "_wire_requirement_graph") as mock_wire:
                with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                    indexer.reindex_file(target)

        mock_wire.assert_called_once()
        mock_compute.assert_called_once()

    def test_index_workspace_clears_export_hashes(self):
        """index_workspace() should reset _file_export_hashes."""
        indexer = _make_indexer()
        # Manually populate a hash
        indexer._file_export_hashes["/fake/test.ivy"] = "abc123"
        indexer.index_workspace()
        assert indexer._file_export_hashes == {}

    def test_file_export_hashes_initialized_empty(self):
        """WorkspaceIndexer should have an empty _file_export_hashes dict on init."""
        indexer = _make_indexer()
        assert hasattr(indexer, "_file_export_hashes")
        assert indexer._file_export_hashes == {}
