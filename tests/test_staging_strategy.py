"""Tests for StagingStrategy ABC and StagingResult."""

import os

import pytest

from ivy_lsp.core.staging.flat import FlatStagingStrategy
from ivy_lsp.core.staging.strategy import StagingResult, StagingStrategy


class TestStagingResult:
    def test_create(self):
        result = StagingResult(
            staging_dir="/tmp/staging",
            staged_files={"types.ivy": "/ws/types.ivy"},
            collision_map={"frame.ivy": ["/ws/a/frame.ivy", "/ws/b/frame.ivy"]},
        )
        assert result.staging_dir == "/tmp/staging"
        assert result.staged_files["types.ivy"] == "/ws/types.ivy"
        assert len(result.collision_map) == 1

    def test_empty(self):
        result = StagingResult.empty()
        assert result.staging_dir is None
        assert result.staged_files == {}
        assert result.collision_map == {}


class TestStagingStrategyABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            StagingStrategy()


class TestFlatStagingStrategy:
    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a workspace with .ivy files."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype t\n")
        (tmp_path / "frame.ivy").write_text("#lang ivy1.7\ntype frame\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "conn.ivy").write_text("#lang ivy1.7\ntype conn\n")
        return tmp_path

    @pytest.fixture
    def workspace_with_collisions(self, tmp_path):
        """Workspace where two dirs have files with the same basename."""
        (tmp_path / "proto_a").mkdir()
        (tmp_path / "proto_a" / "types.ivy").write_text("# proto_a types\n")
        (tmp_path / "proto_b").mkdir()
        (tmp_path / "proto_b" / "types.ivy").write_text("# proto_b types\n")
        (tmp_path / "proto_a" / "unique_a.ivy").write_text("# unique_a\n")
        return tmp_path

    def test_prepare_creates_staging_dir(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
            str(workspace / "sub" / "conn.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        assert result.staging_dir is not None
        assert os.path.isdir(result.staging_dir)
        strategy.cleanup()

    def test_prepare_creates_symlinks(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        assert os.path.islink(os.path.join(result.staging_dir, "types.ivy"))
        assert os.path.islink(os.path.join(result.staging_dir, "frame.ivy"))
        assert result.staged_files["types.ivy"] == str(workspace / "types.ivy")
        strategy.cleanup()

    def test_prepare_detects_collisions(self, workspace_with_collisions):
        ws = workspace_with_collisions
        strategy = FlatStagingStrategy()
        source_files = [
            str(ws / "proto_a" / "types.ivy"),
            str(ws / "proto_b" / "types.ivy"),
            str(ws / "proto_a" / "unique_a.ivy"),
        ]
        result = strategy.prepare(source_files, str(ws))
        assert "types.ivy" in result.collision_map
        assert len(result.collision_map["types.ivy"]) == 2
        # First sorted path wins
        assert result.staged_files["types.ivy"] == str(ws / "proto_a" / "types.ivy")
        strategy.cleanup()

    def test_resolve_finds_staged_file(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [str(workspace / "types.ivy")]
        strategy.prepare(source_files, str(workspace))
        resolved = strategy.resolve("types", str(workspace / "frame.ivy"))
        assert resolved is not None
        assert resolved.endswith("types.ivy")
        strategy.cleanup()

    def test_resolve_returns_none_for_unknown(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [str(workspace / "types.ivy")]
        strategy.prepare(source_files, str(workspace))
        assert strategy.resolve("nonexistent", str(workspace / "frame.ivy")) is None
        strategy.cleanup()

    def test_is_active(self, workspace):
        strategy = FlatStagingStrategy()
        assert not strategy.is_active
        strategy.prepare([str(workspace / "types.ivy")], str(workspace))
        assert strategy.is_active
        strategy.cleanup()
        assert not strategy.is_active

    def test_cleanup_removes_dir(self, workspace):
        strategy = FlatStagingStrategy()
        result = strategy.prepare([str(workspace / "types.ivy")], str(workspace))
        staging_dir = result.staging_dir
        strategy.cleanup()
        assert not os.path.exists(staging_dir)

    def test_prepare_empty_file_list(self, workspace):
        strategy = FlatStagingStrategy()
        result = strategy.prepare([], str(workspace))
        assert result.staging_dir is not None
        assert result.staged_files == {}
        assert result.collision_map == {}
        strategy.cleanup()

    def test_prepare_no_collisions(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        assert result.collision_map == {}
        strategy.cleanup()
