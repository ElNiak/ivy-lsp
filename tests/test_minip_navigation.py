"""Tests for navigation features using the self-contained MiniP test repo.

Exercises document symbols, workspace symbols, go-to-definition, and
references across the multi-directory minip workspace (minip_stack/,
minip_shims/, minip_entities/, minip_entities_behavior/, minip_utils/).
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
# Document Symbols
# ---------------------------------------------------------------------------


class TestDocumentSymbolsMinip:
    """Verify document symbol extraction from minip files."""

    def test_ping_types_has_type_symbols(self, minip_indexer):
        """ping_types.ivy should produce symbols for its type definitions."""
        filepath = str(MINIP_STACK_DIR / "ping_types.ivy")
        syms = minip_indexer.get_symbols(filepath)
        names = {s.name for s in syms}
        # Core types that must be present
        for expected in ("cid", "pkt_num", "version", "stream_kind"):
            assert (
                expected in names
            ), f"Expected type '{expected}' in ping_types.ivy symbols"

    def test_ping_types_has_objects(self, minip_indexer):
        """ping_types.ivy should produce symbols for bit, role objects."""
        filepath = str(MINIP_STACK_DIR / "ping_types.ivy")
        syms = minip_indexer.get_symbols(filepath)
        names = {s.name for s in syms}
        assert "bit" in names
        assert "role" in names

    def test_ping_frame_has_frame_object(self, minip_indexer):
        """ping_frame.ivy declares object frame (reopened 4x) with variants."""
        filepath = str(MINIP_STACK_DIR / "ping_frame.ivy")
        syms = minip_indexer.get_symbols(filepath)
        names = {s.name for s in syms}
        assert "frame" in names

    def test_ping_application_has_action(self, minip_indexer):
        """ping_application.ivy defines app_send_event action."""
        filepath = str(MINIP_STACK_DIR / "ping_application.ivy")
        syms = minip_indexer.get_symbols(filepath)
        names = {s.name for s in syms}
        assert "app_send_event" in names or "ping_data" in names

    def test_ping_endpoint_has_modules(self, minip_indexer):
        """ping_endpoint.ivy should define endpoint-related symbols."""
        filepath = str(MINIP_DIR / "minip_entities" / "ping_endpoint.ivy")
        syms = minip_indexer.get_symbols(filepath)
        names = {s.name for s in syms}
        assert "ping_endpoint" in names or "endpoint_id" in names


# ---------------------------------------------------------------------------
# Workspace Symbols
# ---------------------------------------------------------------------------


class TestWorkspaceSymbolsMinip:
    """Verify workspace-wide symbol lookup across subdirectories."""

    def test_workspace_has_symbols_from_multiple_dirs(self, minip_indexer):
        """Symbols should come from files in different subdirectories."""
        all_syms = minip_indexer.lookup_all_symbols()
        files = {s.file_path for s in all_syms if s.file_path}
        # Should have files from at least 3 different subdirectories
        dirs = {os.path.basename(os.path.dirname(f)) for f in files}
        assert len(dirs) >= 3, f"Expected symbols from 3+ dirs, got: {dirs}"

    def test_lookup_cid_finds_definition(self, minip_indexer):
        """Looking up 'cid' should find it in ping_types.ivy."""
        results = minip_indexer.lookup_symbol("cid")
        assert len(results) >= 1
        # At least one result should be from ping_types.ivy
        filenames = {os.path.basename(r.filepath) for r in results}
        assert "ping_types.ivy" in filenames

    def test_lookup_frame_finds_object(self, minip_indexer):
        """Looking up 'frame' should find the object in ping_frame.ivy."""
        results = minip_indexer.lookup_symbol("frame")
        assert len(results) >= 1
        filenames = {os.path.basename(r.filepath) for r in results}
        assert "ping_frame.ivy" in filenames

    def test_total_symbol_count_reasonable(self, minip_indexer):
        """The workspace should have a reasonable number of symbols (>20)."""
        all_syms = minip_indexer.lookup_all_symbols()
        assert len(all_syms) > 20, f"Expected >20 symbols, got {len(all_syms)}"


# ---------------------------------------------------------------------------
# Cross-file include resolution
# ---------------------------------------------------------------------------


class TestIncludeResolutionMinip:
    """Verify that cross-directory includes resolve via staging."""

    def test_staging_resolves_cross_dir_include(self, minip_indexer):
        """ping_shim.ivy (in minip_shims/) includes ping_types (in minip_stack/).

        This should resolve via the staging directory.
        """
        shim_path = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        resolved = minip_indexer.resolver.resolve("ping_types", shim_path)
        assert resolved is not None, "ping_types should resolve from ping_shim.ivy"
        assert resolved.endswith("ping_types.ivy")

    def test_staging_resolves_entity_to_shim(self, minip_indexer):
        """ping_client.ivy (in minip_entities/) includes ping_shim (in minip_shims/)."""
        client_path = str(MINIP_DIR / "minip_entities" / "ping_client.ivy")
        resolved = minip_indexer.resolver.resolve("ping_shim", client_path)
        assert resolved is not None, "ping_shim should resolve from ping_client.ivy"
        assert resolved.endswith("ping_shim.ivy")

    def test_staging_resolves_behavior_to_utils(self, minip_indexer):
        """ivy_ping_client_behavior.ivy (behavior/) includes ping_file (utils/)."""
        behavior_path = str(
            MINIP_DIR / "minip_entities_behavior" / "ivy_ping_client_behavior.ivy"
        )
        resolved = minip_indexer.resolver.resolve("ping_file", behavior_path)
        assert resolved is not None, "ping_file should resolve from behavior file"
        assert resolved.endswith("ping_file.ivy")

    # NOTE: Full intra-workspace include resolution test lives in
    # test_minip_analysis.py::TestIncludeResolutionMinip::test_unresolved_are_only_stdlib


# ---------------------------------------------------------------------------
# Transitive scope
# ---------------------------------------------------------------------------


class TestTransitiveScopeMinip:
    """Verify get_symbols_in_scope includes transitive include symbols."""

    def test_shim_sees_types_transitively(self, minip_indexer):
        """ping_shim.ivy includes ping_types — scope should contain 'cid'."""
        shim_path = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        scope_syms = minip_indexer.get_symbols_in_scope(shim_path)
        names = {s.name for s in scope_syms}
        assert "cid" in names, "cid should be in ping_shim.ivy scope via include"
