"""Tests for demand-driven deep parsing of shared modules."""

import unittest.mock
from unittest.mock import MagicMock, patch

from lsprotocol.types import SymbolKind

from ivy_lsp.indexer.workspace_indexer import FileIndexStatus, WorkspaceIndexer
from ivy_lsp.parsing.symbols import IvySymbol


def _make_indexer(workspace_root="/fake/workspace"):
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer, parser


class TestDemandDeepParse:
    def test_deep_parse_on_demand_upgrades_symbols(self):
        indexer, parser = _make_indexer()
        filepath = "/fake/quic_types.ivy"
        indexer._deep_index_progress.file_statuses[filepath] = FileIndexStatus(
            filepath=filepath, shallow_indexed=True, deep_parse_attempted=False
        )
        fallback_sym = IvySymbol(
            name="cid", kind=SymbolKind.Variable, range=(0, 0, 1, 0), file_path=filepath
        )
        indexer._symbol_table.add_symbol(fallback_sym)
        ast_sym = IvySymbol(
            name="cid",
            kind=SymbolKind.Class,
            range=(0, 0, 1, 0),
            detail="type cid",
            file_path=filepath,
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.ast = MagicMock()
        parser.parse.return_value = mock_result
        with patch(
            "ivy_lsp.parsing.ast_to_symbols.ast_to_symbols", return_value=[ast_sym]
        ):
            with patch(
                "builtins.open", unittest.mock.mock_open(read_data="type cid\n")
            ):
                upgraded = indexer.deep_parse_on_demand(filepath)
        assert upgraded is True
        symbols = indexer._symbol_table.lookup("cid")
        assert len(symbols) == 1
        assert symbols[0].kind == SymbolKind.Class
        assert symbols[0].detail == "type cid"

    def test_deep_parse_on_demand_skips_already_deep_parsed(self):
        indexer, parser = _make_indexer()
        filepath = "/fake/quic_types.ivy"
        indexer._deep_index_progress.file_statuses[filepath] = FileIndexStatus(
            filepath=filepath,
            shallow_indexed=True,
            deep_parse_attempted=True,
            deep_parse_succeeded=True,
        )
        result = indexer.deep_parse_on_demand(filepath)
        assert result is False
        parser.parse.assert_not_called()

    def test_deep_parse_on_demand_skips_unknown_file(self):
        indexer, parser = _make_indexer()
        result = indexer.deep_parse_on_demand("/fake/unknown.ivy")
        assert result is False
