"""Tests for mirror-scope selective cache invalidation."""

from unittest.mock import MagicMock

from lsprotocol.types import SymbolKind

from ivy_lsp.core.analysis.test_scope import ExportImportInfo
from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
from ivy_lsp.core.parsing.symbols import IvySymbol


def _dummy_sym(name: str, filepath: str = "/fake/dummy.ivy") -> IvySymbol:
    """Create a minimal IvySymbol for cache population tests."""
    return IvySymbol(
        name=name, kind=SymbolKind.Variable, range=(0, 0, 1, 0), file_path=filepath
    )


def _make_indexer(workspace_root="/fake/workspace"):
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    resolver.collision_map = {}
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer


class TestSelectiveInvalidation:
    def test_compute_test_scopes_with_dirty_files_preserves_unaffected_cache(self):
        """When dirty_files is provided, only affected mirror-scope entries are cleared."""
        indexer = _make_indexer()
        indexer._file_export_imports = {
            "/fake/test_a.ivy": ExportImportInfo(
                file="/fake/test_a.ivy",
                exports=["send"],
                imports=["recv"],
                export_lines={"send": 10},
                import_lines={"recv": 20},
            ),
            "/fake/test_b.ivy": ExportImportInfo(
                file="/fake/test_b.ivy",
                exports=["connect"],
                imports=["accept"],
                export_lines={"connect": 10},
                import_lines={"accept": 20},
            ),
        }
        indexer._include_graph.add_edge("/fake/test_a.ivy", "/fake/lib_a.ivy")
        indexer._include_graph.add_edge("/fake/test_b.ivy", "/fake/lib_b.ivy")
        # Initial full scope computation (clears entire cache)
        indexer._compute_test_scopes()
        # Populate cache entries for both scopes
        sym_a = IvySymbol(
            name="x",
            kind=SymbolKind.Variable,
            range=(0, 0, 1, 0),
            file_path="/fake/lib_a.ivy",
        )
        sym_b = IvySymbol(
            name="y",
            kind=SymbolKind.Variable,
            range=(0, 0, 1, 0),
            file_path="/fake/lib_b.ivy",
        )
        indexer._mirror_scope_cache["/fake/lib_a.ivy"] = [sym_a]
        indexer._mirror_scope_cache["/fake/lib_b.ivy"] = [sym_b]
        # Selective invalidation: only lib_a.ivy is dirty
        indexer._compute_test_scopes(dirty_files={"/fake/lib_a.ivy"})
        # lib_a.ivy was in test_a's scope -> cleared
        assert "/fake/lib_a.ivy" not in indexer._mirror_scope_cache
        # lib_b.ivy was NOT in test_a's scope -> preserved
        assert "/fake/lib_b.ivy" in indexer._mirror_scope_cache

    def test_compute_test_scopes_full_clear_without_dirty_files(self):
        """Without dirty_files, full cache clear (backward compatible)."""
        indexer = _make_indexer()
        indexer._mirror_scope_cache["some_key"] = [_dummy_sym("x")]
        indexer._compute_test_scopes()
        assert len(indexer._mirror_scope_cache) == 0

    def test_dirty_file_in_shared_scope_clears_all_scope_members(self):
        """A dirty file clears cache for every file in its test scope closure."""
        indexer = _make_indexer()
        indexer._file_export_imports = {
            "/fake/test.ivy": ExportImportInfo(
                file="/fake/test.ivy",
                exports=["action_a"],
                imports=[],
                export_lines={"action_a": 1},
                import_lines={},
            ),
        }
        indexer._include_graph.add_edge("/fake/test.ivy", "/fake/lib1.ivy")
        indexer._include_graph.add_edge("/fake/test.ivy", "/fake/lib2.ivy")
        indexer._compute_test_scopes()
        # Populate cache for all scope members
        indexer._mirror_scope_cache["/fake/test.ivy"] = [_dummy_sym("t")]
        indexer._mirror_scope_cache["/fake/lib1.ivy"] = [_dummy_sym("l1")]
        indexer._mirror_scope_cache["/fake/lib2.ivy"] = [_dummy_sym("l2")]
        # Dirty only lib1 -> entire scope containing lib1 is cleared
        indexer._compute_test_scopes(dirty_files={"/fake/lib1.ivy"})
        assert "/fake/test.ivy" not in indexer._mirror_scope_cache
        assert "/fake/lib1.ivy" not in indexer._mirror_scope_cache
        assert "/fake/lib2.ivy" not in indexer._mirror_scope_cache

    def test_empty_dirty_files_preserves_all_cache(self):
        """An empty dirty_files set should not clear any cache entries."""
        indexer = _make_indexer()
        indexer._mirror_scope_cache["a"] = [_dummy_sym("a")]
        indexer._mirror_scope_cache["b"] = [_dummy_sym("b")]
        indexer._compute_test_scopes(dirty_files=set())
        assert "a" in indexer._mirror_scope_cache
        assert "b" in indexer._mirror_scope_cache
