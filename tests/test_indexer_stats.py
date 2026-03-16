"""Tests for WorkspaceIndexer stats, reindex, and stale file detection."""

import os
import tempfile
from unittest.mock import MagicMock

import pytest
from lsprotocol.types import SymbolKind

from ivy_lsp.indexer.workspace_indexer import IndexerStats, WorkspaceIndexer
from ivy_lsp.parsing.symbols import IncludeGraph, IvySymbol, SymbolTable


def _make_indexer(workspace_root="/tmp/test"):
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    return WorkspaceIndexer(workspace_root, parser, resolver)


def _make_symbol(name, filepath):
    return IvySymbol(
        name=name,
        kind=SymbolKind.Function,
        range=(0, 0, 0, 0),
        file_path=filepath,
    )


class TestIndexerStats:
    def test_get_stats_returns_dataclass(self):
        indexer = _make_indexer()
        stats = indexer.get_stats()
        assert isinstance(stats, IndexerStats)

    def test_initial_stats_are_zero(self):
        indexer = _make_indexer()
        stats = indexer.get_stats()
        assert stats.file_count == 0
        assert stats.symbol_count == 0
        assert stats.include_edge_count == 0
        assert stats.test_scope_count == 0
        assert stats.per_file_errors == []
        assert stats.stale_files == []

    def test_stats_after_symbols_added(self):
        indexer = _make_indexer()
        # Add symbols directly to the symbol table
        sym1 = _make_symbol("foo", "a.ivy")
        sym2 = _make_symbol("bar", "a.ivy")
        indexer._symbol_table.add_symbol(sym1)
        indexer._symbol_table.add_symbol(sym2)
        stats = indexer.get_stats()
        assert stats.symbol_count == 2
        assert stats.file_count == 1

    def test_stats_include_edges(self):
        indexer = _make_indexer()
        indexer.include_graph.add_edge("a.ivy", "b.ivy")
        indexer.include_graph.add_edge("b.ivy", "c.ivy")
        stats = indexer.get_stats()
        assert stats.include_edge_count == 2

    def test_index_workspace_records_timing(self):
        indexer = _make_indexer()
        indexer.resolver.find_all_ivy_files.return_value = []
        indexer.index_workspace()
        stats = indexer.get_stats()
        assert stats.last_index_duration is not None
        assert stats.last_index_duration >= 0

    def test_stale_file_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.ivy")
            with open(test_file, "w") as f:
                f.write("#lang ivy1.7\n")

            indexer = _make_indexer(workspace_root=tmpdir)
            # Add a symbol for this file so it's tracked
            sym = _make_symbol("x", test_file)
            indexer._symbol_table.add_symbol(sym)

            # File exists: no stale
            stale = indexer.detect_stale_files()
            assert len(stale) == 0

            # Remove the file
            os.remove(test_file)
            stale = indexer.detect_stale_files()
            assert test_file in stale

    def test_per_file_errors_tracked(self):
        indexer = _make_indexer()
        indexer._index_errors = [
            {"uri": "a.ivy", "error": "parse failed"},
        ]
        stats = indexer.get_stats()
        assert len(stats.per_file_errors) == 1
        assert stats.per_file_errors[0]["uri"] == "a.ivy"


class TestReindex:
    def test_reindex_clears_and_reindexes(self):
        indexer = _make_indexer()
        # Add a symbol
        sym = _make_symbol("old", "old.ivy")
        indexer._symbol_table.add_symbol(sym)
        assert indexer.get_stats().symbol_count == 1

        # reindex() resets everything
        indexer.reindex()
        stats = indexer.get_stats()
        assert stats.last_index_duration is not None
        # After reindex with no files found, symbols should be zero
        assert stats.symbol_count == 0
