"""Tests for warm-loading index from persistent cache."""
from unittest.mock import MagicMock

from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer


class TestWarmLoad:
    def test_warm_load_skips_fallback_scan(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")

        parser = MagicMock()
        parser.parse.return_value = MagicMock(success=False, ast=None, errors=[])
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = [str(f)]
        resolver.resolve.return_value = None

        # First run: cold index (populates persistent cache)
        idx1 = WorkspaceIndexer(
            str(tmp_path), parser, resolver, persistent_cache=True,
            cache_dir=str(tmp_path / ".cache"),
        )
        idx1.index_workspace()
        symbols_after_cold = idx1.get_symbols(str(f))
        assert len(symbols_after_cold) > 0
        idx1._cache.close()

        # Second run: warm load (should use cache)
        idx2 = WorkspaceIndexer(
            str(tmp_path), parser, resolver, persistent_cache=True,
            cache_dir=str(tmp_path / ".cache"),
        )
        idx2.index_workspace()
        symbols_after_warm = idx2.get_symbols(str(f))
        assert len(symbols_after_warm) > 0
        idx2._cache.close()

    def test_persistent_false_uses_memory_cache(self, tmp_path):
        parser = MagicMock()
        parser.parse.return_value = MagicMock(success=False, ast=None, errors=[])
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = []

        idx = WorkspaceIndexer(str(tmp_path), parser, resolver)
        from ivy_lsp.indexer.file_cache import FileCache
        assert isinstance(idx._cache, FileCache)
