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


class TestSymbolTableByKind:
    """Tests for the _by_kind index and symbols_by_kind() query."""

    def test_symbols_by_kind_returns_matching(self):
        """symbols_by_kind returns all symbols of the requested kind."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        s2 = IvySymbol(
            name="bar", kind=SymbolKind.Variable, range=(2, 0, 3, 0), file_path="/a.ivy"
        )
        s3 = IvySymbol(
            name="baz", kind=SymbolKind.Function, range=(4, 0, 5, 0), file_path="/b.ivy"
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.add_symbol(s3)

        funcs = table.symbols_by_kind(SymbolKind.Function)
        assert len(funcs) == 2
        assert s1 in funcs
        assert s3 in funcs

        vars_ = table.symbols_by_kind(SymbolKind.Variable)
        assert len(vars_) == 1
        assert vars_[0] is s2

    def test_symbols_by_kind_empty_for_missing_kind(self):
        """symbols_by_kind returns an empty list for a kind with no symbols."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        table.add_symbol(s1)
        assert table.symbols_by_kind(SymbolKind.Class) == []

    def test_symbols_by_kind_empty_table(self):
        """symbols_by_kind returns an empty list on a fresh table."""
        table = SymbolTable()
        assert table.symbols_by_kind(SymbolKind.Function) == []

    def test_remove_file_cleans_kind_index(self):
        """remove_file removes symbols from the _by_kind index."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        s2 = IvySymbol(
            name="bar", kind=SymbolKind.Function, range=(2, 0, 3, 0), file_path="/b.ivy"
        )
        s3 = IvySymbol(
            name="baz", kind=SymbolKind.Variable, range=(4, 0, 5, 0), file_path="/a.ivy"
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.add_symbol(s3)

        table.remove_file("/a.ivy")

        # Function kind should only have s2 left
        funcs = table.symbols_by_kind(SymbolKind.Function)
        assert len(funcs) == 1
        assert funcs[0] is s2

        # Variable kind should be empty (s3 was in /a.ivy)
        assert table.symbols_by_kind(SymbolKind.Variable) == []

    def test_remove_file_kind_index_with_duplicate_kinds_across_files(self):
        """Removing one file preserves same-kind symbols from other files."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="send",
            kind=SymbolKind.Function,
            range=(0, 0, 1, 0),
            file_path="/a.ivy",
        )
        s2 = IvySymbol(
            name="recv",
            kind=SymbolKind.Function,
            range=(2, 0, 3, 0),
            file_path="/a.ivy",
        )
        s3 = IvySymbol(
            name="connect",
            kind=SymbolKind.Function,
            range=(4, 0, 5, 0),
            file_path="/b.ivy",
        )
        table.add_symbol(s1)
        table.add_symbol(s2)
        table.add_symbol(s3)

        table.remove_file("/a.ivy")

        funcs = table.symbols_by_kind(SymbolKind.Function)
        assert len(funcs) == 1
        assert funcs[0] is s3

    def test_symbols_by_kind_returns_copy(self):
        """symbols_by_kind returns a copy, not the internal list."""
        table = SymbolTable()
        s1 = IvySymbol(
            name="foo", kind=SymbolKind.Function, range=(0, 0, 1, 0), file_path="/a.ivy"
        )
        table.add_symbol(s1)

        result = table.symbols_by_kind(SymbolKind.Function)
        result.clear()  # Mutate the returned list

        # Internal state should be unaffected
        assert len(table.symbols_by_kind(SymbolKind.Function)) == 1
