"""Tests for SymbolTable.remove_file in-place removal."""

from lsprotocol.types import SymbolKind

from ivy_lsp.core.parsing.symbols import IvySymbol, SymbolTable


class TestSymbolTableRemoveFile:
    def test_remove_file_clears_by_name(self):
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        s2 = IvySymbol(
            name="bar", kind=SymbolKind.Variable, range=(0, 0, 1, 0), file_path="/b.ivy"
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.remove_file("/a.ivy")
        assert table.lookup("foo") == []
        assert table.lookup("bar") == [s2]

    def test_remove_file_clears_by_file(self):
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        s2 = IvySymbol(
            name="bar", kind=SymbolKind.Variable, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.remove_file("/a.ivy")
        assert table.symbols_in_file("/a.ivy") == []
        assert table.all_symbols() == []

    def test_remove_file_clears_all_list(self):
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        s2 = IvySymbol(
            name="bar", kind=SymbolKind.Variable, range=(0, 0, 1, 0), file_path="/b.ivy"
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.remove_file("/a.ivy")
        assert len(table.all_symbols()) == 1
        assert table.all_symbols()[0].name == "bar"

    def test_remove_file_no_op_for_unknown_file(self):
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        table.add_symbol(s1)
        table.remove_file("/nonexistent.ivy")
        assert len(table.all_symbols()) == 1

    def test_remove_file_handles_duplicate_names_across_files(self):
        """Two files define 'send' — removing one file preserves the other."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="send",
            kind=SymbolKind.Function,
            range=(0, 0, 1, 0),
            file_path="/a.ivy",
        )
        s2 = IvySymbol(
            name="send",
            kind=SymbolKind.Function,
            range=(5, 0, 6, 0),
            file_path="/b.ivy",
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.remove_file("/a.ivy")
        results = table.lookup("send")
        assert len(results) == 1
        assert results[0].file_path == "/b.ivy"
