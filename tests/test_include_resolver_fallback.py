"""Tests for include_resolver collision guard fallback."""

import os

import pytest

from ivy_lsp.indexer.include_resolver import IncludeResolver


def test_collision_guard_falls_through_to_workspace_root(tmp_path):
    """When layer_id is None and basename collides, still try workspace root."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create a file in workspace root
    target = ws / "random_value.ivy"
    target.write_text("#lang ivy1.7\ntype random_value\n")

    # Create an unmapped requesting file (not in any layer)
    requester = ws / "standalone" / "test.ivy"
    requester.parent.mkdir()
    requester.write_text("#lang ivy1.7\ninclude random_value\n")

    resolver = IncludeResolver(str(ws))
    # Simulate collision state
    resolver._collision_map = {
        "random_value.ivy": [str(target), "/fake/random_value.ivy"]
    }
    resolver._file_to_layer = {
        "some_other_file": "apt"
    }  # non-empty but requester not in it
    resolver._staging_dir = str(tmp_path / "staging")
    os.makedirs(resolver._staging_dir, exist_ok=True)
    staged = os.path.join(resolver._staging_dir, "random_value.ivy")
    os.symlink(str(target), staged)

    result = resolver.resolve("random_value", str(requester))
    assert result is not None
    assert result.endswith("random_value.ivy")


def test_collision_guard_still_blocks_flat_staging_for_collisions(tmp_path):
    """Colliding basename should NOT resolve via flat staging (skips to workspace root)."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create file ONLY in staging, not workspace root
    requester = ws / "test.ivy"
    requester.write_text("#lang ivy1.7\ninclude colliding\n")

    resolver = IncludeResolver(str(ws))
    resolver._collision_map = {
        "colliding.ivy": ["/a/colliding.ivy", "/b/colliding.ivy"]
    }
    resolver._file_to_layer = {"some_file": "apt"}
    resolver._staging_dir = str(tmp_path / "staging")
    os.makedirs(resolver._staging_dir, exist_ok=True)
    staged = os.path.join(resolver._staging_dir, "colliding.ivy")
    with open(staged, "w") as f:
        f.write("#lang ivy1.7\n")

    # Should NOT resolve via flat staging (collision blocked)
    # AND not in workspace root either -> None
    result = resolver.resolve("colliding", str(requester))
    assert result is None


def test_non_colliding_basename_resolves_via_flat_staging(tmp_path):
    """Non-colliding basenames should still resolve from flat staging."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    target = ws / "unique_file.ivy"
    target.write_text("#lang ivy1.7\n")

    requester = ws / "test.ivy"
    requester.write_text("#lang ivy1.7\ninclude unique_file\n")

    resolver = IncludeResolver(str(ws))
    resolver._collision_map = {}  # no collisions
    resolver._file_to_layer = {"some_file": "apt"}
    resolver._staging_dir = str(tmp_path / "staging")
    os.makedirs(resolver._staging_dir, exist_ok=True)
    staged = os.path.join(resolver._staging_dir, "unique_file.ivy")
    os.symlink(str(target), staged)

    result = resolver.resolve("unique_file", str(requester))
    assert result is not None
    assert "unique_file.ivy" in result
