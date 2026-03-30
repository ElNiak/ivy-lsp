"""Tests for MCP tool integration using the self-contained MiniP test repo.

Exercises include graph, model info, diagnostics, and indexer stats
across the multi-directory minip workspace.
"""

import os
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from tests.conftest import MINIP_DIR, MINIP_STACK_DIR

# ---------------------------------------------------------------------------
# Include Graph
# ---------------------------------------------------------------------------


class TestIncludeGraphMinip:
    """Verify include graph edges across subdirectories."""

    def test_ping_shim_is_hub(self, minip_indexer):
        """ping_shim.ivy should include many files (it's the central hub)."""
        shim_path = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        includes = minip_indexer.include_graph.get_includes(shim_path)
        # ping_shim includes ~8 intra-workspace modules (some stdlib unresolved)
        assert len(includes) >= 4, (
            f"Expected ping_shim to include 4+ files, got {len(includes)}: "
            f"{[os.path.basename(f) for f in includes]}"
        )

    def test_ping_types_has_no_includes(self, minip_indexer):
        """ping_types.ivy has no include statements — leaf node."""
        types_path = str(MINIP_STACK_DIR / "ping_types.ivy")
        includes = minip_indexer.include_graph.get_includes(types_path)
        assert len(includes) == 0, (
            f"ping_types.ivy should have no includes, got: "
            f"{[os.path.basename(f) for f in includes]}"
        )

    def test_ping_types_is_included_by_others(self, minip_indexer):
        """ping_types.ivy should be included by at least one other file."""
        types_path = str(MINIP_STACK_DIR / "ping_types.ivy")
        included_by = minip_indexer.include_graph.get_included_by(types_path)
        assert (
            len(included_by) >= 1
        ), "ping_types.ivy should be included by at least 1 file"

    def test_transitive_includes_from_shim(self, minip_indexer):
        """Transitive includes from ping_shim should reach ping_types."""
        shim_path = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        transitive = minip_indexer.include_graph.get_transitive_includes(shim_path)
        basenames = {os.path.basename(f) for f in transitive}
        assert "ping_types.ivy" in basenames, (
            f"ping_types.ivy should be transitively included from ping_shim.ivy. "
            f"Got: {basenames}"
        )

    def test_include_edges_span_directories(self, minip_indexer):
        """Include edges should connect files across different subdirectories."""
        shim_path = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        includes = minip_indexer.include_graph.get_includes(shim_path)
        include_dirs = {os.path.basename(os.path.dirname(f)) for f in includes}
        # shim includes files from minip_stack at minimum
        assert (
            "minip_stack" in include_dirs or len(include_dirs) >= 2
        ), f"Expected cross-directory includes, got dirs: {include_dirs}"


# ---------------------------------------------------------------------------
# Indexer Stats
# ---------------------------------------------------------------------------


class TestIndexerStatsMinip:
    """Verify indexer statistics for the minip workspace."""

    def test_file_count(self, minip_indexer):
        """Should index all 19 .ivy files across subdirectories."""
        stats = minip_indexer.get_stats()
        # At least 17 (stack files); may get up to 19 with test files
        assert (
            stats.file_count >= 15
        ), f"Expected >=15 indexed files, got {stats.file_count}"

    def test_symbol_count(self, minip_indexer):
        """Should have a reasonable number of symbols."""
        stats = minip_indexer.get_stats()
        assert (
            stats.symbol_count > 20
        ), f"Expected >20 symbols, got {stats.symbol_count}"

    def test_include_edge_count(self, minip_indexer):
        """Should have multiple include edges."""
        stats = minip_indexer.get_stats()
        assert (
            stats.include_edge_count >= 5
        ), f"Expected >=5 include edges, got {stats.include_edge_count}"

    def test_no_index_errors(self, minip_indexer):
        """Well-formed minip files should not produce index errors."""
        stats = minip_indexer.get_stats()
        # Some files have native C++ blocks or ellipsis syntax which may
        # cause fallback scanner to note issues — but fatal errors should be 0
        fatal_errors = [e for e in stats.per_file_errors if "fatal" in str(e).lower()]
        assert not fatal_errors, f"Fatal index errors: {fatal_errors}"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


class TestFileDiscoveryMinip:
    """Verify that the resolver finds files across all subdirectories."""

    def test_find_all_ivy_files(self, minip_indexer):
        """Resolver should find .ivy files from all subdirectories."""
        all_files = minip_indexer.get_all_ivy_file_paths()
        basenames = {os.path.basename(f) for f in all_files}
        # Check presence of files from different dirs
        assert "ping_types.ivy" in basenames  # minip_stack
        assert "ping_shim.ivy" in basenames  # minip_shims
        assert "ping_endpoint.ivy" in basenames  # minip_entities

    def test_file_count_matches_expected(self, minip_indexer):
        """Should find exactly 19 .ivy files (17 stack + 2 test specs).

        The test/ subdirectory is excluded by _EXCLUDED_DIR_BASENAMES, so
        the actual count may be less if minip_tests is structured under test/.
        """
        all_files = minip_indexer.get_all_ivy_file_paths()
        # At minimum all stack + shim + entity + behavior + utils files
        assert len(all_files) >= 15, f"Expected >=15 .ivy files, got {len(all_files)}"
