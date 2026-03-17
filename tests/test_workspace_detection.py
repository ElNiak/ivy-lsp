"""Tests for ivy_lsp.workspace_detection module."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from ivy_lsp.config import reset_config
from ivy_lsp.workspace_detection import (
    WorkspaceConfig,
    _panther_heuristic,
    _read_marker,
    _resolve_git_worktree,
    _walk_down_for_marker,
    _walk_up_for_marker,
    detect_ivy_workspace,
)


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary directory for workspace detection tests."""
    return tmp_path


@pytest.fixture
def isolated_tmp():
    """Temp dir outside the workspace tree (avoids TMPDIR leakage).

    When TMPDIR points inside the ivy-lsp directory (e.g. Claude Code sandbox),
    pytest tmp_path creates dirs inside the workspace tree. The walk-up marker
    search then finds the real .ivyworkspace marker, breaking isolation.
    This fixture explicitly uses /tmp to escape that.
    """
    d = Path(tempfile.mkdtemp(prefix="ivy-ws-test-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ivyworkspace_marker(tmp_workspace):
    """Create a .ivyworkspace marker file in the tmp workspace."""
    marker = {
        "version": 1,
        "include_paths": ["protocol-testing"],
        "exclude_paths": ["test", "doc"],
    }
    marker_path = tmp_workspace / ".ivyworkspace"
    marker_path.write_text(json.dumps(marker))
    return marker_path


class TestReadMarker:
    def test_valid_marker(self, ivyworkspace_marker):
        data = _read_marker(str(ivyworkspace_marker))
        assert data is not None
        assert data["version"] == 1
        assert data["include_paths"] == ["protocol-testing"]

    def test_missing_file(self, tmp_workspace):
        data = _read_marker(str(tmp_workspace / "nonexistent"))
        assert data is None

    def test_invalid_json(self, tmp_workspace):
        bad = tmp_workspace / ".ivyworkspace"
        bad.write_text("not json {{{")
        data = _read_marker(str(bad))
        assert data is None

    def test_non_object_json(self, tmp_workspace):
        bad = tmp_workspace / ".ivyworkspace"
        bad.write_text('"just a string"')
        data = _read_marker(str(bad))
        assert data is None


class TestWalkUpForMarker:
    def test_marker_in_current_dir(self, tmp_workspace, ivyworkspace_marker):
        config = _walk_up_for_marker(str(tmp_workspace))
        assert config is not None
        assert config.detected_by == "marker"
        assert config.include_paths == ["protocol-testing"]

    def test_marker_in_ancestor(self, tmp_workspace, ivyworkspace_marker):
        child = tmp_workspace / "a" / "b" / "c"
        child.mkdir(parents=True)
        config = _walk_up_for_marker(str(child))
        assert config is not None
        assert config.workspace_root == str(tmp_workspace)

    def test_no_marker_found(self):
        with tempfile.TemporaryDirectory() as td:
            config = _walk_up_for_marker(td, max_depth=2)
            assert config is None


class TestWalkDownForMarker:
    def test_marker_in_subdirectory(self, tmp_workspace):
        sub = tmp_workspace / "project" / "ivy"
        sub.mkdir(parents=True)
        marker = {"version": 1, "include_paths": ["src"]}
        (sub / ".ivyworkspace").write_text(json.dumps(marker))
        config = _walk_down_for_marker(str(tmp_workspace))
        assert config is not None
        assert config.workspace_root == str(sub)
        assert config.include_paths == ["src"]

    def test_marker_too_deep(self, tmp_workspace):
        deep = tmp_workspace / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / ".ivyworkspace").write_text('{"version": 1}')
        config = _walk_down_for_marker(str(tmp_workspace), max_depth=2)
        assert config is None

    def test_no_marker(self, tmp_workspace):
        config = _walk_down_for_marker(str(tmp_workspace))
        assert config is None


class TestPantherHeuristic:
    def test_panther_structure_detected(self, tmp_workspace):
        panther_ivy = (
            tmp_workspace
            / "panther"
            / "plugins"
            / "services"
            / "testers"
            / "panther_ivy"
        )
        (panther_ivy / "protocol-testing").mkdir(parents=True)
        config = _panther_heuristic(str(tmp_workspace))
        assert config is not None
        assert config.project_type == "panther"
        assert config.detected_by == "heuristic"
        assert "protocol-testing" in config.include_paths

    def test_inside_panther_ivy(self, tmp_workspace):
        # Simulate CWD being panther_ivy itself
        (tmp_workspace / "protocol-testing").mkdir()
        (tmp_workspace / "panther_ivy.py").write_text("# marker")
        config = _panther_heuristic(str(tmp_workspace))
        assert config is not None
        assert config.project_type == "panther"

    def test_no_panther_structure(self, isolated_tmp):
        config = _panther_heuristic(str(isolated_tmp))
        assert config is None


class TestDetectIvyWorkspace:
    def test_explicit_workspace_overrides_all(self, tmp_workspace):
        config = detect_ivy_workspace(
            start_dir=str(tmp_workspace),
            explicit_workspace=str(tmp_workspace / "my_ws"),
        )
        assert config.detected_by == "explicit"
        assert config.workspace_root == str(tmp_workspace / "my_ws")

    def test_env_workspace_overrides(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("IVY_LSP_WORKSPACE", str(tmp_workspace / "env_ws"))
        reset_config()
        config = detect_ivy_workspace(start_dir=str(tmp_workspace))
        assert config.detected_by == "explicit"
        assert config.workspace_root == str(tmp_workspace / "env_ws")

    def test_env_workspace_hint(self, tmp_workspace, monkeypatch):
        # Clean env to avoid interference
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        sub = tmp_workspace / "ivy-project"
        sub.mkdir()
        marker = {"version": 1, "include_paths": ["models"]}
        (sub / ".ivyworkspace").write_text(json.dumps(marker))
        monkeypatch.setenv("IVY_LSP_WORKSPACE_HINT", "ivy-project")
        reset_config()
        config = detect_ivy_workspace(start_dir=str(tmp_workspace))
        assert config.detected_by == "hint"
        assert config.workspace_root == str(sub)

    def test_marker_walk_up(self, tmp_workspace, ivyworkspace_marker, monkeypatch):
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
        child = tmp_workspace / "sub" / "deep"
        child.mkdir(parents=True)
        config = detect_ivy_workspace(start_dir=str(child))
        assert config.detected_by == "marker"
        assert config.workspace_root == str(tmp_workspace)

    def test_fallback_to_start_dir(self, isolated_tmp, monkeypatch):
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
        config = detect_ivy_workspace(start_dir=str(isolated_tmp))
        assert config.detected_by == "fallback"
        assert config.workspace_root == str(isolated_tmp)

    def test_explicit_include_exclude_paths(self, tmp_workspace):
        config = detect_ivy_workspace(
            start_dir=str(tmp_workspace),
            explicit_workspace=str(tmp_workspace),
            explicit_include_paths=["src"],
            explicit_exclude_paths=["vendor"],
        )
        assert config.include_paths == ["src"]
        assert config.exclude_paths == ["vendor"]

    def test_panther_heuristic_detected(self, isolated_tmp, monkeypatch):
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
        panther_ivy = (
            isolated_tmp
            / "panther"
            / "plugins"
            / "services"
            / "testers"
            / "panther_ivy"
        )
        (panther_ivy / "protocol-testing").mkdir(parents=True)
        config = detect_ivy_workspace(start_dir=str(isolated_tmp))
        assert config.detected_by == "heuristic"
        assert config.project_type == "panther"


class TestResolveGitWorktree:
    def test_worktree_resolves_to_main_tree(self, tmp_workspace):
        """Git worktree .git file should resolve back to main tree."""
        main_repo = tmp_workspace / "main-repo"
        main_git = main_repo / ".git"
        main_git.mkdir(parents=True)

        # Create worktree that points to main repo
        worktree = tmp_workspace / "worktree"
        worktree.mkdir()
        worktree_gitdir = main_git / "worktrees" / "my-worktree"
        worktree_gitdir.mkdir(parents=True)
        (worktree_gitdir / "commondir").write_text("../..")
        (worktree / ".git").write_text(f"gitdir: {worktree_gitdir}")

        result = _resolve_git_worktree(str(worktree))
        assert result == str(main_repo)

    def test_non_repo_returns_none(self, tmp_workspace):
        result = _resolve_git_worktree(str(tmp_workspace))
        assert result is None

    def test_regular_repo_returns_none(self, tmp_workspace):
        (tmp_workspace / ".git").mkdir()
        result = _resolve_git_worktree(str(tmp_workspace))
        assert result is None

    def test_broken_git_file_returns_none(self, tmp_workspace):
        (tmp_workspace / ".git").write_text("not a gitdir line")
        result = _resolve_git_worktree(str(tmp_workspace))
        assert result is None


class TestWorktreeWorkspaceDetection:
    def test_worktree_detects_panther_via_main_tree(self, isolated_tmp, monkeypatch):
        """detect_ivy_workspace should follow worktree link to find PANTHER structure."""
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)

        # Main repo with panther_ivy
        main_repo = isolated_tmp / "main"
        main_git = main_repo / ".git"
        main_git.mkdir(parents=True)
        panther_ivy = (
            main_repo / "panther" / "plugins" / "services" / "testers" / "panther_ivy"
        )
        (panther_ivy / "protocol-testing").mkdir(parents=True)

        # Worktree with empty panther_ivy (simulates uninitialized submodule)
        worktree = isolated_tmp / "worktree"
        worktree.mkdir()
        (
            worktree / "panther" / "plugins" / "services" / "testers" / "panther_ivy"
        ).mkdir(parents=True)
        worktree_gitdir = main_git / "worktrees" / "wt"
        worktree_gitdir.mkdir(parents=True)
        (worktree_gitdir / "commondir").write_text("../..")
        (worktree / ".git").write_text(f"gitdir: {worktree_gitdir}")

        config = detect_ivy_workspace(start_dir=str(worktree))
        assert "worktree" in config.detected_by
        assert config.project_type == "panther"
        assert config.workspace_root == str(panther_ivy)


class TestHintWithHeuristic:
    def test_hint_with_panther_structure_no_marker(self, tmp_workspace, monkeypatch):
        """Hint pointing to a dir with PANTHER structure but no marker should work."""
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        panther_ivy = tmp_workspace / "panther_ivy"
        (panther_ivy / "protocol-testing").mkdir(parents=True)
        (panther_ivy / "panther_ivy.py").write_text("# marker")
        monkeypatch.setenv("IVY_LSP_WORKSPACE_HINT", str(panther_ivy))
        reset_config()
        config = detect_ivy_workspace(start_dir=str(tmp_workspace))
        assert config.detected_by == "hint"
        assert config.project_type == "panther"


class TestWorkspaceConfig:
    def test_defaults(self):
        config = WorkspaceConfig(workspace_root="/tmp/test")
        assert config.include_paths == []
        assert config.exclude_paths == []
        assert config.detected_by == "fallback"
        assert config.project_type is None
