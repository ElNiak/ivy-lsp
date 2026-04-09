"""Tests for incremental T1/T2 analysis logic."""

import os

import pytest

from ivy_lsp.core.parsing.symbols import IncludeGraph
from ivy_lsp.core.workspace.context import FileChange, StalenessInfo
from ivy_lsp.infra.utils.hashing import file_sha256
from ivy_lsp.lsp.bulk_orchestrator import (
    _expand_dirty_set,
    _validate_changes,
    expand_with_budget,
)


def test_validate_changes_removed_file_is_dirty(tmp_path):
    """Removed files are always dirty (no hash to check)."""
    staleness = StalenessInfo(
        status="stale_minor",
        changed_files=1,
        total_files=10,
        file_changes=[FileChange("gone.ivy", "removed", "abc123")],
    )
    dirty = _validate_changes(staleness, str(tmp_path))
    assert dirty == {os.path.join(str(tmp_path), "gone.ivy")}


def test_validate_changes_hash_match_is_clean(tmp_path):
    """File with same SHA-256 is hash-clean despite mtime change."""
    f = tmp_path / "a.ivy"
    f.write_text("#lang ivy1.7\ntype t")
    real_sha = file_sha256(str(f))

    staleness = StalenessInfo(
        status="stale_minor",
        changed_files=1,
        total_files=10,
        file_changes=[FileChange("a.ivy", "modified", real_sha)],
    )
    dirty = _validate_changes(staleness, str(tmp_path))
    assert dirty == set()


def test_validate_changes_hash_mismatch_is_dirty(tmp_path):
    """File with different SHA-256 is hash-dirty."""
    f = tmp_path / "a.ivy"
    f.write_text("#lang ivy1.7\ntype t")

    staleness = StalenessInfo(
        status="stale_minor",
        changed_files=1,
        total_files=10,
        file_changes=[FileChange("a.ivy", "modified", "wrong_hash")],
    )
    dirty = _validate_changes(staleness, str(tmp_path))
    assert dirty == {str(f)}


def test_validate_changes_missing_cached_hash_is_dirty(tmp_path):
    """File with no cached SHA-256 is always dirty."""
    f = tmp_path / "a.ivy"
    f.write_text("#lang ivy1.7")

    staleness = StalenessInfo(
        status="stale_minor",
        changed_files=1,
        total_files=10,
        file_changes=[FileChange("a.ivy", "modified", None)],
    )
    dirty = _validate_changes(staleness, str(tmp_path))
    assert dirty == {str(f)}


def test_expand_dirty_set_no_dependents():
    """Leaf file with no reverse dependents stays as-is."""
    graph = IncludeGraph()
    graph.add_edge("conn.ivy", "types.ivy")
    dirty = {"conn.ivy"}
    assert _expand_dirty_set(dirty, graph) == {"conn.ivy"}


def test_expand_dirty_set_expands_transitively():
    """Changing types.ivy cascades to conn.ivy and test.ivy."""
    graph = IncludeGraph()
    graph.add_edge("conn.ivy", "types.ivy")
    graph.add_edge("test.ivy", "conn.ivy")
    dirty = {"types.ivy"}
    assert _expand_dirty_set(dirty, graph) == {"types.ivy", "conn.ivy", "test.ivy"}


def test_expand_with_budget_under_budget():
    """When cascade is under budget, returns expanded set."""
    graph = IncludeGraph()
    graph.add_edge("conn.ivy", "types.ivy")
    dirty = {"types.ivy"}
    result = expand_with_budget(dirty, graph, 10, budget_ratio=0.5)
    assert result is not None
    assert result == {"types.ivy", "conn.ivy"}


def test_expand_with_budget_exceeded():
    """When cascade exceeds 50%, returns None to signal fallback."""
    graph = IncludeGraph()
    for i in range(9):
        graph.add_edge(f"f{i}.ivy", f"f{i+1}.ivy")
    dirty = {"f9.ivy"}
    total_files = 10
    result = expand_with_budget(dirty, graph, total_files, budget_ratio=0.5)
    assert result is None
