"""Tests for ivy_lsp.core.workspace.detection module."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from ivy_lsp.core.workspace.detection import (
    WorkspaceConfig,
    WorkspaceLayer,
    _discover_protocols,
    _panther_heuristic,
    _read_marker,
    _resolve_git_worktree,
    _walk_down_for_marker,
    _walk_up_for_marker,
    detect_ivy_workspace,
)
from ivy_lsp.infra.config import reset_config


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
    Tries /tmp first; falls back to default tempdir if /tmp is not writable.
    """
    try:
        d = Path(tempfile.mkdtemp(prefix="ivy-ws-test-", dir="/tmp"))
    except OSError:
        d = Path(tempfile.mkdtemp(prefix="ivy-ws-test-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ivyworkspace_marker(tmp_workspace):
    """Create a v3 .ivyworkspace marker file in the tmp workspace."""
    marker = {
        "version": 3,
        "workspace_layers": [{"id": "default", "include_paths": ["protocol-testing"]}],
        "exclude_paths": ["test", "doc"],
    }
    marker_path = tmp_workspace / ".ivyworkspace"
    marker_path.write_text(json.dumps(marker))
    return marker_path


class TestReadMarker:
    def test_valid_marker(self, ivyworkspace_marker):
        data = _read_marker(str(ivyworkspace_marker))
        assert data is not None
        assert data["version"] == 3
        assert len(data["workspace_layers"]) == 1

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
        marker = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": ["src"]}],
        }
        (sub / ".ivyworkspace").write_text(json.dumps(marker))
        config = _walk_down_for_marker(str(tmp_workspace))
        assert config is not None
        assert config.workspace_root == str(sub)
        assert config.include_paths == ["src"]

    def test_marker_too_deep(self, tmp_workspace):
        deep = tmp_workspace / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / ".ivyworkspace").write_text(
            json.dumps({"version": 3, "workspace_layers": []})
        )
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
        pt = panther_ivy / "protocol-testing"
        pt.mkdir(parents=True)

        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
        config = _panther_heuristic(str(tmp_workspace))
        assert config is not None
        assert config.project_type == "panther"
        assert config.detected_by == "heuristic"
        assert any("protocol-testing" in p for p in config.include_paths)

    def test_inside_panther_ivy(self, tmp_workspace):
        # Simulate CWD being panther_ivy itself
        pt = tmp_workspace / "protocol-testing"
        pt.mkdir()
        (tmp_workspace / "panther_ivy.py").write_text("# marker")

        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
        config = _panther_heuristic(str(tmp_workspace))
        assert config is not None
        assert config.project_type == "panther"

    def test_no_panther_structure(self, isolated_tmp):
        config = _panther_heuristic(str(isolated_tmp))
        assert config is None

    def test_no_markers_returns_none(self, tmp_workspace):
        """PANTHER structure without per-protocol markers returns None."""
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
        marker = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": ["models"]}],
        }
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
        # Skip if TMPDIR fallback landed inside a real workspace tree
        if _walk_up_for_marker(str(isolated_tmp)):
            pytest.skip("TMPDIR is inside a workspace tree")
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
        # Skip if TMPDIR fallback landed inside a real workspace tree
        if _walk_up_for_marker(str(isolated_tmp)):
            pytest.skip("TMPDIR is inside a workspace tree")
        panther_ivy = (
            isolated_tmp
            / "panther"
            / "plugins"
            / "services"
            / "testers"
            / "panther_ivy"
        )
        pt = panther_ivy / "protocol-testing"
        pt.mkdir(parents=True)

        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
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
        # Skip if TMPDIR fallback landed inside a real workspace tree
        if _walk_up_for_marker(str(isolated_tmp)):
            pytest.skip("TMPDIR is inside a workspace tree")

        # Main repo with panther_ivy
        main_repo = isolated_tmp / "main"
        main_git = main_repo / ".git"
        main_git.mkdir(parents=True)
        panther_ivy = (
            main_repo / "panther" / "plugins" / "services" / "testers" / "panther_ivy"
        )
        pt = panther_ivy / "protocol-testing"
        pt.mkdir(parents=True)

        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')

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
    def test_hint_with_panther_structure_and_markers(self, tmp_workspace, monkeypatch):
        """Hint pointing to a dir with PANTHER structure and protocol markers should work."""
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        panther_ivy = tmp_workspace / "panther_ivy"
        pt = panther_ivy / "protocol-testing"
        pt.mkdir(parents=True)
        (panther_ivy / "panther_ivy.py").write_text("# marker")

        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
        monkeypatch.setenv("IVY_LSP_WORKSPACE_HINT", str(panther_ivy))
        reset_config()
        config = detect_ivy_workspace(start_dir=str(tmp_workspace))
        assert config.detected_by == "hint"
        assert config.project_type == "panther"


class TestExplicitWorkspaceWithMarker:
    def test_explicit_workspace_reads_marker(self, tmp_workspace):
        """Explicit workspace with .ivyworkspace should read marker (MCP mode fix)."""
        marker = {
            "version": 3,
            "workspace_layers": [
                {
                    "id": "standard",
                    "include_paths": [
                        "protocol-testing/quic",
                        "protocol-testing/minip",
                    ],
                },
                {"id": "apt", "include_paths": ["protocol-testing/apt"], "priority": 2},
            ],
            "exclude_paths": ["test", "doc"],
        }
        (tmp_workspace / ".ivyworkspace").write_text(json.dumps(marker))
        config = detect_ivy_workspace(
            start_dir="/tmp",
            explicit_workspace=str(tmp_workspace),
        )
        assert config.detected_by == "explicit+marker"
        assert "protocol-testing/quic" in config.include_paths
        assert "protocol-testing/minip" in config.include_paths
        assert "protocol-testing/apt" in config.include_paths
        assert config.exclude_paths == ["test", "doc"]
        assert len(config.workspace_layers) == 2
        assert config.workspace_layers[0].id == "standard"

    def test_explicit_workspace_no_marker_falls_back(self, tmp_workspace):
        """Explicit workspace without .ivyworkspace should return empty paths (existing behavior)."""
        config = detect_ivy_workspace(
            start_dir="/tmp",
            explicit_workspace=str(tmp_workspace),
        )
        assert config.detected_by == "explicit"
        assert config.include_paths == []
        assert config.exclude_paths == []

    def test_explicit_cli_paths_override_marker(self, tmp_workspace):
        """Explicit CLI include/exclude paths should override marker-derived ones."""
        marker = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": ["from-marker"]}],
            "exclude_paths": ["marker-exclude"],
        }
        (tmp_workspace / ".ivyworkspace").write_text(json.dumps(marker))
        config = detect_ivy_workspace(
            start_dir="/tmp",
            explicit_workspace=str(tmp_workspace),
            explicit_include_paths=["cli-include"],
            explicit_exclude_paths=["cli-exclude"],
        )
        assert config.detected_by == "explicit+marker"
        assert config.include_paths == ["cli-include"]
        assert config.exclude_paths == ["cli-exclude"]
        # Layers should still be populated from marker
        assert len(config.workspace_layers) == 1


class TestWorkspaceConfig:
    def test_defaults(self):
        config = WorkspaceConfig(workspace_root="/tmp/test")
        assert config.include_paths == []
        assert config.exclude_paths == []
        assert config.detected_by == "fallback"
        assert config.project_type is None


class TestV2Rejection:
    def test_v2_marker_returns_none(self, tmp_workspace):
        """v2 .ivyworkspace should be gracefully ignored (return None)."""
        marker = {"version": 2, "include_paths": ["protocol-testing"]}
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker))
        result = _walk_up_for_marker(str(tmp_workspace))
        assert result is None

    def test_v1_marker_returns_none(self, tmp_workspace):
        """v1 .ivyworkspace should be gracefully ignored (return None)."""
        marker = {"version": 1}
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker))
        result = _walk_up_for_marker(str(tmp_workspace))
        assert result is None


class TestV3LayerParsing:
    def test_v3_layers_parsed(self, tmp_workspace):
        """v3 .ivyworkspace should parse workspace_layers correctly."""
        marker = {
            "version": 3,
            "workspace_layers": [
                {"id": "standard", "include_paths": ["quic", "minip"], "priority": 1},
                {"id": "apt", "include_paths": ["apt"], "priority": 2},
            ],
            "exclude_paths": ["test"],
        }
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker))
        config = _walk_up_for_marker(str(tmp_workspace))
        assert config is not None
        assert len(config.workspace_layers) == 2
        assert config.workspace_layers[0].id == "standard"
        assert config.workspace_layers[0].include_paths == ["quic", "minip"]
        assert config.workspace_layers[1].id == "apt"
        assert config.workspace_layers[1].priority == 2
        # Flattened include_paths from all layers
        assert "quic" in config.include_paths
        assert "minip" in config.include_paths
        assert "apt" in config.include_paths

    def test_v3_empty_layers(self, tmp_workspace):
        """v3 with empty workspace_layers should still work."""
        marker = {"version": 3, "workspace_layers": []}
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker))
        config = _walk_up_for_marker(str(tmp_workspace))
        assert config is not None
        assert config.workspace_layers == []


class TestPantherHeuristicV3:
    pass


# ---------------------------------------------------------------------------
# Task 2: workspace_groups, protocol_id, workspace_root_offset
# ---------------------------------------------------------------------------


class TestWorkspaceConfigNewFields:
    def test_workspace_config_has_workspace_groups(self):
        """WorkspaceConfig must have workspace_groups defaulting to empty dict."""
        config = WorkspaceConfig(workspace_root="/tmp/test")
        assert hasattr(config, "workspace_groups")
        assert config.workspace_groups == {}

    def test_workspace_config_has_protocol_id(self):
        """WorkspaceConfig must have protocol_id defaulting to None."""
        config = WorkspaceConfig(workspace_root="/tmp/test")
        assert hasattr(config, "protocol_id")
        assert config.protocol_id is None

    def test_workspace_config_has_workspace_root_offset(self):
        """WorkspaceConfig must have workspace_root_offset defaulting to None."""
        config = WorkspaceConfig(workspace_root="/tmp/test")
        assert hasattr(config, "workspace_root_offset")
        assert config.workspace_root_offset is None


class TestApplyMarkerNewFields:
    def test_apply_marker_parses_workspace_groups(self, tmp_workspace):
        """_apply_marker must parse workspace_groups from JSON."""
        from ivy_lsp.core.workspace.detection import _apply_marker

        marker_data = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": ["src"]}],
            "workspace_groups": {
                "quic": ["protocol-testing/quic"],
                "minip": ["protocol-testing/minip"],
            },
        }
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker_data))

        config = _apply_marker(str(marker_path), marker_data)

        assert config is not None
        assert config.workspace_groups == {
            "quic": ["protocol-testing/quic"],
            "minip": ["protocol-testing/minip"],
        }

    def test_apply_marker_parses_protocol_id(self, tmp_workspace):
        """_apply_marker must parse protocol_id from JSON."""
        from ivy_lsp.core.workspace.detection import _apply_marker

        marker_data = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": ["src"]}],
            "protocol_id": "quic",
            "workspace_root_offset": "../..",
        }
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker_data))

        config = _apply_marker(str(marker_path), marker_data)

        assert config is not None
        assert config.protocol_id == "quic"

    def test_workspace_root_offset_resolves_correctly(self, tmp_workspace):
        """workspace_root_offset must shift workspace_root relative to marker dir."""
        import os

        from ivy_lsp.core.workspace.detection import _apply_marker

        # Simulate: marker lives at tmp/protocol-testing/quic/.ivyworkspace
        # offset "../.." should resolve to tmp/ (the panther_ivy root)
        protocol_dir = tmp_workspace / "protocol-testing" / "quic"
        protocol_dir.mkdir(parents=True)
        offset = "../.."
        expected_root = os.path.normpath(str(protocol_dir) + "/" + offset)

        marker_data = {
            "version": 3,
            "workspace_layers": [{"id": "quic", "include_paths": ["."]}],
            "protocol_id": "quic",
            "workspace_root_offset": offset,
        }
        marker_path = protocol_dir / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker_data))

        config = _apply_marker(str(marker_path), marker_data)

        assert config is not None
        assert config.workspace_root == expected_root
        assert config.workspace_root_offset == offset

    def test_apply_marker_optional_fields(self, tmp_workspace):
        """Existing markers without optional fields must parse correctly with defaults."""
        from ivy_lsp.core.workspace.detection import _apply_marker

        marker_data = {
            "version": 3,
            "workspace_layers": [
                {"id": "standard", "include_paths": ["protocol-testing/quic"]}
            ],
            "exclude_paths": ["test"],
        }
        marker_path = tmp_workspace / ".ivyworkspace"
        marker_path.write_text(json.dumps(marker_data))

        config = _apply_marker(str(marker_path), marker_data)

        assert config is not None
        assert config.workspace_groups == {}
        assert config.protocol_id is None
        assert config.workspace_root_offset is None
        # Existing fields unaffected
        assert config.workspace_root == str(tmp_workspace)
        assert "protocol-testing/quic" in config.include_paths
        assert config.exclude_paths == ["test"]


# ---------------------------------------------------------------------------
# Task 13: _discover_protocols dynamic discovery
# ---------------------------------------------------------------------------


class TestDiscoverProtocols:
    def test_discover_protocols_from_markers(self, tmp_path):
        """Protocols with .ivyworkspace markers are discovered; those without are not."""
        pt = tmp_path / "protocol-testing"
        pt.mkdir()
        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
        (pt / "apt").mkdir()
        (pt / "apt" / ".ivyworkspace").write_text('{"version": 3}')
        (pt / "no_marker").mkdir()  # No marker — should not be discovered

        protocols = _discover_protocols(str(pt))
        assert "quic" in protocols
        assert "apt" in protocols
        assert "no_marker" not in protocols

    def test_discover_protocols_sorted(self, tmp_path):
        """Discovered protocol list is sorted alphabetically."""
        pt = tmp_path / "protocol-testing"
        pt.mkdir()
        for name in ["zzz", "aaa", "mmm"]:
            (pt / name).mkdir()
            (pt / name / ".ivyworkspace").write_text('{"version": 3}')

        protocols = _discover_protocols(str(pt))
        assert protocols == sorted(protocols)

    def test_discover_protocols_nonexistent_dir(self, tmp_path):
        """Non-existent protocol-testing dir returns empty list."""
        protocols = _discover_protocols(str(tmp_path / "no-such-dir"))
        assert protocols == []

    def test_discover_protocols_empty_dir(self, tmp_path):
        """Empty protocol-testing dir returns empty list."""
        pt = tmp_path / "protocol-testing"
        pt.mkdir()
        protocols = _discover_protocols(str(pt))
        assert protocols == []

    def test_discover_protocols_files_ignored(self, tmp_path):
        """Regular files (not dirs) are not returned, even if named like protocols."""
        pt = tmp_path / "protocol-testing"
        pt.mkdir()
        (pt / "README.md").write_text("not a protocol dir")
        protocols = _discover_protocols(str(pt))
        assert protocols == []


class TestPantherHeuristicDynamicDiscovery:
    def test_heuristic_uses_discovered_protocols(self, tmp_path):
        """PANTHER heuristic discovers protocols via per-protocol markers."""
        panther_ivy = (
            tmp_path / "panther" / "plugins" / "services" / "testers" / "panther_ivy"
        )
        pt = panther_ivy / "protocol-testing"
        pt.mkdir(parents=True)
        (pt / "quic").mkdir()
        (pt / "quic" / ".ivyworkspace").write_text('{"version": 3}')
        (pt / "bgp").mkdir()
        (pt / "bgp" / ".ivyworkspace").write_text('{"version": 3}')
        (pt / "no_marker").mkdir()  # No marker — must be excluded

        config = _panther_heuristic(str(tmp_path))
        assert config is not None
        assert config.project_type == "panther"
        assert any("protocol-testing/quic" in p for p in config.include_paths)
        assert any("protocol-testing/bgp" in p for p in config.include_paths)
        assert not any("no_marker" in p for p in config.include_paths)
