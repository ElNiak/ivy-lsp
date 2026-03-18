"""Tests for Task 1.7: Workspace Symbols Feature (TDD).

Verifies that workspace symbol search flattens hierarchical ``IvySymbol``
trees into qualified-name ``FlatSymbol`` lists, supports case-insensitive
substring matching, respects result limits, and converts to LSP
``WorkspaceSymbol`` with correct URIs and ranges.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.features.workspace_symbols import (
    MAX_RESULTS,
    FlatSymbol,
    compute_workspace_symbols,
    flatten_symbols,
    search_symbols,
    to_workspace_symbol,
)
from ivy_lsp.parsing.symbols import IvySymbol


class TestFlatSymbolDataclass:
    """Verify the FlatSymbol dataclass fields."""

    def test_fields(self):
        """FlatSymbol stores qualified_name, kind, file_path, range."""
        fs = FlatSymbol(
            qualified_name="frame.ack",
            kind=lsp.SymbolKind.Module,
            file_path="/tmp/test.ivy",
            range=(0, 0, 0, 10),
        )
        assert fs.qualified_name == "frame.ack"
        assert fs.kind == lsp.SymbolKind.Module
        assert fs.file_path == "/tmp/test.ivy"
        assert fs.range == (0, 0, 0, 10)


class TestFlattenSymbols:
    """Verify recursive flattening of IvySymbol trees."""

    def test_single_symbol(self):
        """A single symbol without children produces one FlatSymbol."""
        sym = IvySymbol(
            name="cid",
            kind=lsp.SymbolKind.Class,
            range=(0, 0, 0, 8),
            file_path="/tmp/test.ivy",
        )
        result = flatten_symbols([sym])
        assert len(result) == 1
        assert result[0].qualified_name == "cid"
        assert result[0].kind == lsp.SymbolKind.Class
        assert result[0].file_path == "/tmp/test.ivy"
        assert result[0].range == (0, 0, 0, 8)

    def test_nested_symbols(self):
        """Nested symbols produce qualified dotted names."""
        inner_child = IvySymbol(
            name="this",
            kind=lsp.SymbolKind.Class,
            range=(3, 4, 3, 20),
            file_path="/tmp/test.ivy",
        )
        child = IvySymbol(
            name="ack",
            kind=lsp.SymbolKind.Module,
            range=(2, 4, 4, 1),
            children=[inner_child],
            file_path="/tmp/test.ivy",
        )
        parent = IvySymbol(
            name="frame",
            kind=lsp.SymbolKind.Module,
            range=(1, 0, 5, 1),
            children=[child],
            file_path="/tmp/test.ivy",
        )
        result = flatten_symbols([parent])
        assert len(result) == 3
        assert result[0].qualified_name == "frame"
        assert result[1].qualified_name == "frame.ack"
        assert result[2].qualified_name == "frame.ack.this"

    def test_empty_list(self):
        """Empty input produces empty output."""
        result = flatten_symbols([])
        assert result == []

    def test_with_prefix(self):
        """A prefix is prepended to all qualified names."""
        sym = IvySymbol(
            name="cid",
            kind=lsp.SymbolKind.Class,
            range=(0, 0, 0, 8),
            file_path="/tmp/test.ivy",
        )
        result = flatten_symbols([sym], prefix="parent")
        assert len(result) == 1
        assert result[0].qualified_name == "parent.cid"


class TestSearchSymbols:
    """Verify case-insensitive substring matching and result limits."""

    def _make_flat(self, name: str) -> FlatSymbol:
        """Helper to create a FlatSymbol with a given name."""
        return FlatSymbol(
            qualified_name=name,
            kind=lsp.SymbolKind.Variable,
            file_path="/tmp/test.ivy",
            range=(0, 0, 0, 0),
        )

    def test_case_insensitive_match(self):
        """Query 'CID' matches qualified_name 'cid' (case-insensitive)."""
        flat = [self._make_flat("cid"), self._make_flat("pkt_num")]
        result = search_symbols(flat, "CID")
        assert len(result) == 1
        assert result[0].qualified_name == "cid"

    def test_empty_query_returns_all(self):
        """An empty query string returns all symbols (up to MAX_RESULTS)."""
        flat = [self._make_flat("a"), self._make_flat("b"), self._make_flat("c")]
        result = search_symbols(flat, "")
        assert len(result) == 3

    def test_no_match_returns_empty(self):
        """A query that matches nothing returns an empty list."""
        flat = [self._make_flat("cid"), self._make_flat("pkt_num")]
        result = search_symbols(flat, "zzz_no_match")
        assert result == []

    def test_result_limit(self):
        """Results are capped at _SEARCH_INTERNAL_LIMIT, not MAX_RESULTS."""
        from ivy_lsp.features.workspace_symbols import _SEARCH_INTERNAL_LIMIT

        flat = [self._make_flat(f"sym_{i}") for i in range(1500)]
        result = search_symbols(flat, "sym")
        assert len(result) == _SEARCH_INTERNAL_LIMIT

    def test_substring_match(self):
        """Substring matching works: 'ack' matches 'frame.ack.range'."""
        flat = [
            self._make_flat("frame.ack.range"),
            self._make_flat("frame.seq"),
        ]
        result = search_symbols(flat, "ack")
        assert len(result) == 1
        assert result[0].qualified_name == "frame.ack.range"


class TestToWorkspaceSymbol:
    """Verify conversion from FlatSymbol to LSP WorkspaceSymbol."""

    def test_basic_conversion(self):
        """FlatSymbol converts to WorkspaceSymbol with file:// URI."""
        fs = FlatSymbol(
            qualified_name="frame.ack",
            kind=lsp.SymbolKind.Module,
            file_path="/tmp/test.ivy",
            range=(1, 0, 5, 1),
        )
        ws = to_workspace_symbol(fs)
        assert isinstance(ws, lsp.WorkspaceSymbol)
        assert ws.name == "frame.ack"
        assert ws.kind == lsp.SymbolKind.Module
        assert ws.location.uri == "file:///tmp/test.ivy"

    def test_range_preserved(self):
        """The LSP Range in the WorkspaceSymbol matches the input range."""
        fs = FlatSymbol(
            qualified_name="cid",
            kind=lsp.SymbolKind.Class,
            file_path="/tmp/test.ivy",
            range=(2, 5, 2, 15),
        )
        ws = to_workspace_symbol(fs)
        r = ws.location.range
        assert r.start.line == 2
        assert r.start.character == 5
        assert r.end.line == 2
        assert r.end.character == 15

    def test_no_file_path(self):
        """A FlatSymbol with no file_path produces an empty URI."""
        fs = FlatSymbol(
            qualified_name="orphan",
            kind=lsp.SymbolKind.Variable,
            file_path=None,
            range=(0, 0, 0, 0),
        )
        ws = to_workspace_symbol(fs)
        assert ws.location.uri == ""


class TestComputeWorkspaceSymbols:
    """Verify compute_workspace_symbols with scope-aware ranking."""

    class _MockIndexer:
        """Minimal indexer returning pre-defined symbols with optional scope."""

        def __init__(self, symbols, scope_files=None):
            self._symbols = symbols
            self._scope_files = scope_files

        def lookup_all_symbols(self):
            return self._symbols

        def get_scope_files_for_file(self, filepath):
            return self._scope_files

    def _make_sym(self, name, file_path):
        return IvySymbol(
            name=name,
            kind=lsp.SymbolKind.Variable,
            range=(0, 0, 0, len(name)),
            file_path=file_path,
        )

    def test_empty_query_with_active_filepath_ranks_active_file_first(self):
        """Empty query + active_filepath promotes active-file symbols."""
        syms = [
            self._make_sym("alpha", "/ws/apt/apt_entities/a.ivy"),
            self._make_sym("beta", "/ws/apt/apt_entities/b.ivy"),
            self._make_sym("cid", "/ws/quic_types.ivy"),
            self._make_sym("delta", "/ws/apt/apt_entities/d.ivy"),
        ]
        scope_files = {"/ws/quic_types.ivy"}
        indexer = self._MockIndexer(syms, scope_files)

        results = compute_workspace_symbols(
            indexer, query="", active_filepath="/ws/quic_types.ivy"
        )

        assert len(results) == 4
        # cid from quic_types.ivy should be ranked first (in-scope)
        assert results[0].name == "cid"

    def test_empty_query_without_active_filepath_returns_insertion_order(self):
        """Empty query without active_filepath returns symbols in flat order."""
        syms = [
            self._make_sym("alpha", "/ws/apt/a.ivy"),
            self._make_sym("beta", "/ws/apt/b.ivy"),
            self._make_sym("cid", "/ws/quic_types.ivy"),
        ]
        indexer = self._MockIndexer(syms, scope_files=None)

        results = compute_workspace_symbols(indexer, query="")

        assert len(results) == 3
        # No scope ranking, original order preserved
        assert results[0].name == "alpha"
        assert results[1].name == "beta"
        assert results[2].name == "cid"

    def test_empty_query_scope_ranking_caps_at_max_results(self):
        """Scope-ranked results still respect MAX_RESULTS limit."""
        syms = [self._make_sym(f"sym_{i}", "/ws/other.ivy") for i in range(150)]
        scope_files = {"/ws/active.ivy"}
        indexer = self._MockIndexer(syms, scope_files)

        results = compute_workspace_symbols(
            indexer, query="", active_filepath="/ws/active.ivy"
        )

        assert len(results) == MAX_RESULTS

    def test_nonempty_query_with_scope_still_filters(self):
        """Non-empty query filters first, then scope-ranks."""
        syms = [
            self._make_sym("alpha", "/ws/other.ivy"),
            self._make_sym("cid", "/ws/quic_types.ivy"),
            self._make_sym("acid", "/ws/apt/apt_entities/a.ivy"),
        ]
        scope_files = {"/ws/quic_types.ivy"}
        indexer = self._MockIndexer(syms, scope_files)

        results = compute_workspace_symbols(
            indexer, query="cid", active_filepath="/ws/quic_types.ivy"
        )

        assert len(results) == 2
        # cid (in scope) should rank before acid (out of scope)
        assert results[0].name == "cid"
        assert results[1].name == "acid"


class TestSearchSymbolsRanking:
    """Verify that exact-name definitions rank above substring matches."""

    def _make_flat(
        self, name: str, kind=lsp.SymbolKind.Variable, file_path="/tmp/test.ivy"
    ) -> FlatSymbol:
        return FlatSymbol(
            qualified_name=name,
            kind=kind,
            file_path=file_path,
            range=(0, 0, 0, 0),
        )

    def test_exact_match_not_lost_when_many_substring_matches(self):
        """Exact match 'cid' must survive when >100 substring matches exist."""
        # 150 APT entity symbols with "cid" in their qualified name
        noise = [self._make_flat(f"apt.entity{i}.acidic_thing") for i in range(150)]
        # The actual cid type definition
        target = self._make_flat(
            "cid", kind=lsp.SymbolKind.Class, file_path="/tmp/quic_types.ivy"
        )
        flat = noise + [target]

        # Use compute_workspace_symbols to test the full pipeline
        class _FakeIndexer:
            def __init__(self, syms):
                self._syms = syms

            def lookup_all_symbols(self):
                return self._syms

            def get_scope_files_for_file(self, path):
                return None

        # Build IvySymbol objects for the indexer
        ivy_syms = []
        for f in flat:
            ivy_syms.append(
                IvySymbol(
                    name=f.qualified_name,
                    kind=f.kind,
                    range=f.range,
                    file_path=f.file_path,
                )
            )

        indexer = _FakeIndexer(ivy_syms)
        results = compute_workspace_symbols(indexer, "cid")
        names = [r.name for r in results]
        assert "cid" in names, "Exact match 'cid' must not be lost to substring matches"
        # It should be ranked first (definition boost)
        assert names[0] == "cid"


class TestRegister:
    """Verify that the register function is importable."""

    def test_register_importable(self):
        """The register function can be imported from the module."""
        from ivy_lsp.features.workspace_symbols import register

        assert callable(register)
