"""Tests for Task 10: Workspace Symbol Filtering.

Verifies that compute_workspace_symbols() respects the active_workspace
parameter, filtering symbols to only include those from active layers
plus stdlib (ivy/include) files.
"""

from __future__ import annotations

import os

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.parsing.symbols import IvySymbol
from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
from ivy_lsp.features.workspace_symbols import (
    FlatSymbol,
    compute_workspace_symbols,
    flatten_symbols,
)

# ---------------------------------------------------------------------------
# Helpers / Mock objects
# ---------------------------------------------------------------------------


def _make_symbol(name: str, file_path: str) -> IvySymbol:
    return IvySymbol(
        name=name,
        kind=lsp.SymbolKind.Variable,
        range=(0, 0, 0, len(name)),
        file_path=file_path,
    )


class _MockResolver:
    """Minimal resolver with _file_to_layer."""

    def __init__(self, file_to_layer: dict):
        self._file_to_layer = file_to_layer


class _MockIndexer:
    """Minimal indexer with a resolver and optional scope_files."""

    def __init__(self, symbols, file_to_layer=None, scope_files=None):
        self._symbols = symbols
        self.resolver = _MockResolver(file_to_layer or {})
        self._scope_files = scope_files

    def lookup_all_symbols(self):
        return self._symbols

    def get_scope_files_for_file(self, filepath):
        return self._scope_files


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkspaceSymbolsFilterActiveLayers:
    """When workspace is active, only symbols from active layers + stdlib."""

    def test_filter_keeps_active_layer_symbols(self):
        """Symbols whose files are in active layers pass the filter."""
        quic_file = os.path.normpath(os.path.abspath("/ws/quic/quic_types.ivy"))
        apt_file = os.path.normpath(os.path.abspath("/ws/apt/apt_model.ivy"))

        syms = [
            _make_symbol("quic_sym", quic_file),
            _make_symbol("apt_sym", apt_file),
        ]
        file_to_layer = {
            quic_file: "quic",
            apt_file: "apt",
        }
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        assert "quic_sym" in names
        assert "apt_sym" not in names

    def test_filter_excludes_out_of_scope_layers(self):
        """Symbols from layers not in active_layers are filtered out."""
        quic_file = os.path.normpath(os.path.abspath("/ws/quic/quic_packet.ivy"))
        http_file = os.path.normpath(os.path.abspath("/ws/http/http_types.ivy"))
        minip_file = os.path.normpath(os.path.abspath("/ws/minip/minip_types.ivy"))

        syms = [
            _make_symbol("quic_pkt", quic_file),
            _make_symbol("http_req", http_file),
            _make_symbol("minip_conn", minip_file),
        ]
        file_to_layer = {
            quic_file: "quic",
            http_file: "http",
            minip_file: "minip",
        }
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="sym", active_workspace=ws)
        names = [r.name for r in results]
        assert "quic_pkt" not in names  # doesn't match "sym" query
        # Verify filter leaves only quic layer
        all_results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        all_names = [r.name for r in all_results]
        assert "quic_pkt" in all_names
        assert "http_req" not in all_names
        assert "minip_conn" not in all_names

    def test_filter_keeps_multiple_active_layers(self):
        """When multiple layers are active, symbols from all are returned."""
        quic_file = os.path.normpath(os.path.abspath("/ws/quic/quic_types.ivy"))
        tests_file = os.path.normpath(os.path.abspath("/ws/quic_tests/test_client.ivy"))
        apt_file = os.path.normpath(os.path.abspath("/ws/apt/apt_model.ivy"))

        syms = [
            _make_symbol("quic_sym", quic_file),
            _make_symbol("test_sym", tests_file),
            _make_symbol("apt_sym", apt_file),
        ]
        file_to_layer = {
            quic_file: "quic",
            tests_file: "quic_tests",
            apt_file: "apt",
        }
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic", "quic_tests"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        assert "quic_sym" in names
        assert "test_sym" in names
        assert "apt_sym" not in names


class TestWorkspaceSymbolsStdlibAlwaysIncluded:
    """Stdlib symbols (ivy/include) always pass the filter."""

    def test_stdlib_passes_even_when_workspace_active(self):
        """Symbols from files under ivy/include are always kept."""
        stdlib_file = os.path.normpath(
            os.path.abspath("/usr/lib/ivy/include/1.7/order.ivy")
        )
        non_stdlib_file = os.path.normpath(
            os.path.abspath("/ws/other_protocol/foo.ivy")
        )

        syms = [
            _make_symbol("stdlib_order", stdlib_file),
            _make_symbol("other_sym", non_stdlib_file),
        ]
        file_to_layer = {
            non_stdlib_file: "other_protocol",
            # stdlib_file intentionally not in file_to_layer (it's external)
        }
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        # Stdlib is always included
        assert "stdlib_order" in names
        # Out-of-scope protocol file is excluded
        assert "other_sym" not in names

    def test_stdlib_marker_in_path_is_sufficient(self):
        """Any path containing os.sep + 'ivy' + os.sep + 'include' is stdlib."""
        # Use a path with the stdlib marker but unusual prefix
        stdlib_file = os.path.normpath(os.path.abspath("/opt/ivy/include/net.ivy"))
        syms = [_make_symbol("net_sym", stdlib_file)]
        indexer = _MockIndexer(syms, file_to_layer={})

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        assert "net_sym" in names


class TestWorkspaceSymbolsNoFilterWhenCleared:
    """When no workspace is active (cleared), all symbols returned."""

    def test_cleared_workspace_returns_all_symbols(self):
        """Cleared workspace = no filtering, all symbols returned."""
        file_a = os.path.normpath(os.path.abspath("/ws/quic/quic_types.ivy"))
        file_b = os.path.normpath(os.path.abspath("/ws/apt/apt_model.ivy"))
        file_c = os.path.normpath(os.path.abspath("/ws/http/http_types.ivy"))

        syms = [
            _make_symbol("alpha", file_a),
            _make_symbol("beta", file_b),
            _make_symbol("gamma", file_c),
        ]
        file_to_layer = {file_a: "quic", file_b: "apt", file_c: "http"}
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace.cleared()

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        assert "alpha" in names
        assert "beta" in names
        assert "gamma" in names

    def test_none_active_workspace_returns_all_symbols(self):
        """None active_workspace (default) = no filtering."""
        file_a = os.path.normpath(os.path.abspath("/ws/quic/quic_types.ivy"))
        file_b = os.path.normpath(os.path.abspath("/ws/apt/apt_model.ivy"))

        syms = [
            _make_symbol("alpha", file_a),
            _make_symbol("beta", file_b),
        ]
        indexer = _MockIndexer(syms, file_to_layer={file_a: "quic", file_b: "apt"})

        results = compute_workspace_symbols(indexer, query="")
        names = [r.name for r in results]
        assert "alpha" in names
        assert "beta" in names

    def test_no_resolver_on_indexer_falls_back_gracefully(self):
        """If indexer has no resolver, filtering is skipped (no crash)."""

        class _NoResolverIndexer:
            def lookup_all_symbols(self):
                return [_make_symbol("sym_a", "/ws/quic/q.ivy")]

            def get_scope_files_for_file(self, fp):
                return None

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(
            _NoResolverIndexer(), query="", active_workspace=ws
        )
        # No crash; symbol is returned because resolver is missing (fail-open)
        assert len(results) == 1

    def test_workspace_not_set_granularity_none(self):
        """Workspace with granularity='none' does not filter."""
        file_a = os.path.normpath(os.path.abspath("/ws/quic/q.ivy"))
        file_b = os.path.normpath(os.path.abspath("/ws/apt/a.ivy"))

        syms = [_make_symbol("q_sym", file_a), _make_symbol("a_sym", file_b)]
        indexer = _MockIndexer(syms, file_to_layer={file_a: "quic", file_b: "apt"})

        ws = ActiveWorkspace(
            active_group=None,
            active_layers={"quic"},  # layers set but granularity is none
            active_tests=[],
            granularity="none",
            set_by="cleared",
        )

        results = compute_workspace_symbols(indexer, query="", active_workspace=ws)
        names = [r.name for r in results]
        # is_set() returns False when granularity='none'
        assert "q_sym" in names
        assert "a_sym" in names


class TestWorkspaceSymbolsFilterWithQuery:
    """Filtering interacts correctly with query-based search."""

    def test_filter_applied_before_search(self):
        """Filtering removes out-of-scope files before query search."""
        quic_file = os.path.normpath(os.path.abspath("/ws/quic/quic_conn.ivy"))
        apt_file = os.path.normpath(os.path.abspath("/ws/apt/apt_conn.ivy"))

        syms = [
            _make_symbol("quic_conn_id", quic_file),
            _make_symbol("apt_conn_id", apt_file),
        ]
        file_to_layer = {quic_file: "quic", apt_file: "apt"}
        indexer = _MockIndexer(syms, file_to_layer=file_to_layer)

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(indexer, query="conn", active_workspace=ws)
        names = [r.name for r in results]
        # Only quic layer active
        assert "quic_conn_id" in names
        assert "apt_conn_id" not in names

    def test_symbol_with_none_file_path_passes_filter(self):
        """Symbols with no file_path are not filtered out (unlayered)."""
        syms = [_make_symbol("orphan_sym", None)]
        # We need to override the IvySymbol creation since file_path=None
        syms = []

        class _NullFileSymbol:
            name = "orphan_sym"
            kind = lsp.SymbolKind.Variable
            range = (0, 0, 0, 0)
            file_path = None
            children = []

        syms = [_NullFileSymbol()]

        class _NullIndexer:
            def __init__(self):
                self.resolver = _MockResolver({})

            def lookup_all_symbols(self):
                return syms

            def get_scope_files_for_file(self, fp):
                return None

        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )

        results = compute_workspace_symbols(
            _NullIndexer(), query="", active_workspace=ws
        )
        # Orphan (no file_path) should be included (fail-open)
        names = [r.name for r in results]
        assert "orphan_sym" in names
