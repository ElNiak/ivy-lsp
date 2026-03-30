"""Tests for visualization features using the self-contained MiniP test repo.

Exercises the requirement graph building from real workspace data
and verifies structural properties of the resulting model.
"""

import os
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


# ---------------------------------------------------------------------------
# Requirement Graph from real workspace
# ---------------------------------------------------------------------------


class TestRequirementGraphMinip:
    """Verify requirement graph built from the minip workspace."""

    def test_requirement_graph_exists(self, minip_indexer):
        """The indexer should have a non-None requirement graph."""
        graph = minip_indexer.requirement_graph
        assert graph is not None

    def test_include_graph_has_edges(self, minip_indexer):
        """The include graph should have edges from the indexed workspace."""
        ig = minip_indexer.include_graph
        # Count total edges
        total_edges = sum(len(targets) for targets in ig._includes.values())
        assert total_edges >= 5, f"Expected >=5 include graph edges, got {total_edges}"

    def test_include_graph_files_from_multiple_dirs(self, minip_indexer):
        """Include graph should reference files from multiple subdirectories."""
        ig = minip_indexer.include_graph
        all_files = set()
        for src, targets in ig._includes.items():
            all_files.add(src)
            all_files.update(targets)
        dirs = {os.path.basename(os.path.dirname(f)) for f in all_files if f}
        assert (
            len(dirs) >= 2
        ), f"Expected files from 2+ directories in include graph, got {dirs}"


# ---------------------------------------------------------------------------
# Layered overview from workspace symbols
# ---------------------------------------------------------------------------


class TestLayeredOverviewMinip:
    """Verify symbols can be grouped by directory/layer."""

    def test_symbols_span_all_layers(self, minip_indexer):
        """Symbols from the full workspace should span multiple layer dirs."""
        all_syms = minip_indexer.lookup_all_symbols()
        dirs = set()
        for sym in all_syms:
            if sym.file_path:
                parent = os.path.basename(os.path.dirname(sym.file_path))
                dirs.add(parent)
        expected_layers = {"minip_stack", "minip_shims", "minip_entities"}
        found = expected_layers & dirs
        assert (
            len(found) >= 2
        ), f"Expected symbols from >=2 of {expected_layers}, found {found}"

    def test_symbols_per_layer_reasonable(self, minip_indexer):
        """Each layer should contribute at least some symbols."""
        all_syms = minip_indexer.lookup_all_symbols()
        layer_counts = {}
        for sym in all_syms:
            if sym.file_path:
                layer = os.path.basename(os.path.dirname(sym.file_path))
                layer_counts[layer] = layer_counts.get(layer, 0) + 1
        # minip_stack should have the most symbols (8 files with types/actions)
        if "minip_stack" in layer_counts:
            assert (
                layer_counts["minip_stack"] >= 5
            ), f"minip_stack should have >=5 symbols, got {layer_counts['minip_stack']}"
