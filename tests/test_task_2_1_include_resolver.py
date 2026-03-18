"""Tests for Task 2.1: Include Resolver."""

import os
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestIncludeResolverImport:
    def test_import(self):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        assert IncludeResolver is not None


class TestIncludeResolverSameDir:
    def test_resolve_same_dir(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("#lang ivy1.7\ntype a\n")
        (tmp_path / "b.ivy").write_text("#lang ivy1.7\ninclude a\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("a", str(tmp_path / "b.ivy"))
        assert result is not None
        assert result == str(tmp_path / "a.ivy")

    def test_resolve_nonexistent_returns_none(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "b.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("nonexistent", str(tmp_path / "b.ivy"))
        assert result is None


class TestIncludeResolverWorkspaceRoot:
    def test_resolve_workspace_root(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (tmp_path / "shared.ivy").write_text("#lang ivy1.7\ntype s\n")
        (subdir / "user.ivy").write_text("#lang ivy1.7\ninclude shared\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("shared", str(subdir / "user.ivy"))
        assert result == str(tmp_path / "shared.ivy")


class TestIncludeResolverStdLib:
    def test_resolve_collections(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "test.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("collections", str(tmp_path / "test.ivy"))
        assert result is not None
        assert result.endswith("collections.ivy")
        assert "include" in result

    def test_resolve_order(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "test.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("order", str(tmp_path / "test.ivy"))
        assert result is not None
        assert result.endswith("order.ivy")


class TestIncludeResolverOverride:
    def test_override_std_lib_path(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        fake_std = tmp_path / "fake_std"
        fake_std.mkdir()
        (fake_std / "custom.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path), ivy_include_path=str(fake_std))
        result = resolver.resolve("custom", str(tmp_path / "test.ivy"))
        assert result == str(fake_std / "custom.ivy")


class TestIncludeResolverPriority:
    def test_same_dir_wins_over_workspace_root(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "shared.ivy").write_text("# workspace root version\n")
        (sub / "shared.ivy").write_text("# same dir version\n")
        (sub / "user.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        result = resolver.resolve("shared", str(sub / "user.ivy"))
        assert result == str(sub / "shared.ivy")


class TestFindAllIvyFiles:
    def test_finds_all_files(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("")
        (tmp_path / "b.ivy").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.ivy").write_text("")
        (tmp_path / "d.txt").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        files = resolver.find_all_ivy_files()
        assert len(files) == 3
        names = {os.path.basename(f) for f in files}
        assert names == {"a.ivy", "b.ivy", "c.ivy"}

    def test_empty_dir(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        resolver = IncludeResolver(str(tmp_path))
        assert resolver.find_all_ivy_files() == []

    def test_excludes_build_directory(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "real.ivy").write_text("")
        build = tmp_path / "build" / "lib"
        build.mkdir(parents=True)
        (build / "duplicate.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        files = resolver.find_all_ivy_files()
        names = {os.path.basename(f) for f in files}
        assert names == {"real.ivy"}

    def test_excludes_git_and_pycache(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "src.ivy").write_text("")
        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        (git_dir / "stray.ivy").write_text("")
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "cached.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert files[0].endswith("src.ivy")

    def test_excludes_pytest_temp_dirs(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "main.ivy").write_text("")
        pytest_dir = tmp_path / "pytest-of-user"
        pytest_dir.mkdir()
        (pytest_dir / "a.ivy").write_text("")
        pytest_dir2 = tmp_path / "pytest-123"
        pytest_dir2.mkdir()
        (pytest_dir2 / "b.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert files[0].endswith("main.ivy")


class TestIncludeResolverQuicStack:
    def test_quic_frame_includes_resolve(self):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic_stack = (
            Path(__file__).resolve().parent.parent
            / "protocol-testing"
            / "quic"
            / "quic_stack"
        )
        if not quic_stack.exists():
            pytest.skip("quic_stack not found")
        resolver = IncludeResolver(str(quic_stack))
        frame_file = str(quic_stack / "quic_frame.ivy")
        result = resolver.resolve("quic_stream", frame_file)
        assert result is not None
        assert result.endswith("quic_stream.ivy")
        result = resolver.resolve("collections", frame_file)
        assert result is not None
        assert "include" in result

    def test_find_all_ivy_files_quic_stack(self):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic_stack = (
            Path(__file__).resolve().parent.parent
            / "protocol-testing"
            / "quic"
            / "quic_stack"
        )
        if not quic_stack.exists():
            pytest.skip("quic_stack not found")
        resolver = IncludeResolver(str(quic_stack))
        files = resolver.find_all_ivy_files()
        assert len(files) >= 15


class TestExcludePaths:
    def test_exclude_paths_skips_directory(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "keep.ivy").write_text("")
        excluded = tmp_path / "apt" / "sub"
        excluded.mkdir(parents=True)
        (excluded / "skip.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path), exclude_paths=["apt"])
        files = resolver.find_all_ivy_files()
        names = {os.path.basename(f) for f in files}
        assert names == {"keep.ivy"}

    def test_exclude_paths_nested(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "protocol-testing" / "quic"
        quic.mkdir(parents=True)
        (quic / "model.ivy").write_text("")
        apt = tmp_path / "protocol-testing" / "apt"
        apt.mkdir(parents=True)
        (apt / "model.ivy").write_text("")
        resolver = IncludeResolver(
            str(tmp_path),
            exclude_paths=["protocol-testing/apt"],
        )
        files = resolver.find_all_ivy_files()
        names = {os.path.basename(f) for f in files}
        assert names == {"model.ivy"}
        assert any("quic" in f for f in files)

    def test_exclude_paths_empty_keeps_all(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path), exclude_paths=[])
        files = resolver.find_all_ivy_files()
        assert len(files) == 2

    def test_exclude_submodules_and_test_by_default_basenames(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "real.ivy").write_text("")
        sm = tmp_path / "submodules"
        sm.mkdir()
        (sm / "z3.ivy").write_text("")
        td = tmp_path / "test"
        td.mkdir()
        (td / "lang_test.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        files = resolver.find_all_ivy_files()
        names = {os.path.basename(f) for f in files}
        assert names == {"real.ivy"}


class TestStagingDirectory:
    def test_create_staging_creates_symlinks(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("# file a")
        sub = tmp_path / "stack"
        sub.mkdir()
        (sub / "b.ivy").write_text("# file b")
        resolver = IncludeResolver(str(tmp_path))
        staging = resolver.create_staging_directory()
        assert os.path.isdir(staging)
        assert os.path.islink(os.path.join(staging, "a.ivy"))
        assert os.path.islink(os.path.join(staging, "b.ivy"))
        assert os.path.realpath(os.path.join(staging, "a.ivy")) == str(
            tmp_path / "a.ivy"
        )
        assert os.path.realpath(os.path.join(staging, "b.ivy")) == str(sub / "b.ivy")
        resolver.cleanup_staging()

    def test_staging_excludes_paths(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "keep.ivy").write_text("")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "skip.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path), exclude_paths=["apt"])
        staging = resolver.create_staging_directory()
        assert os.path.exists(os.path.join(staging, "keep.ivy"))
        assert not os.path.exists(os.path.join(staging, "skip.ivy"))
        resolver.cleanup_staging()

    def test_staging_collision_first_wins(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        d1 = tmp_path / "aa"
        d1.mkdir()
        (d1 / "dup.ivy").write_text("# first")
        d2 = tmp_path / "bb"
        d2.mkdir()
        (d2 / "dup.ivy").write_text("# second")
        resolver = IncludeResolver(str(tmp_path))
        staging = resolver.create_staging_directory()
        # sorted walk: aa/ comes before bb/, so first wins
        target = os.path.realpath(os.path.join(staging, "dup.ivy"))
        assert target == str(d1 / "dup.ivy")
        resolver.cleanup_staging()

    def test_find_all_returns_original_paths_when_staged(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        sub = tmp_path / "stack"
        sub.mkdir()
        (sub / "model.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert files[0] == str(sub / "model.ivy")
        resolver.cleanup_staging()

    def test_cleanup_removes_staging(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path))
        staging = resolver.create_staging_directory()
        assert os.path.isdir(staging)
        resolver.cleanup_staging()
        assert not os.path.isdir(staging)

    def test_get_staged_path_returns_symlink(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        staging = resolver.create_staging_directory()
        result = resolver.get_staged_path(str(tmp_path / "a.ivy"))
        assert result == os.path.join(staging, "a.ivy")
        resolver.cleanup_staging()

    def test_get_staged_path_no_staging(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        resolver = IncludeResolver(str(tmp_path))
        assert resolver.get_staged_path("/foo/a.ivy") is None

    def test_get_staged_path_unknown_file(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("#lang ivy1.7\n")
        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()
        assert resolver.get_staged_path("/elsewhere/unknown.ivy") is None
        resolver.cleanup_staging()

    def test_get_staged_path_collision_victim(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        d1 = tmp_path / "aa"
        d1.mkdir()
        (d1 / "dup.ivy").write_text("# first wins")
        d2 = tmp_path / "bb"
        d2.mkdir()
        (d2 / "dup.ivy").write_text("# second loses")
        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()
        # First file (sorted) was staged, should return staging path
        assert resolver.get_staged_path(str(d1 / "dup.ivy")) is not None
        # Second file (collision victim) must return None, not the wrong file
        assert resolver.get_staged_path(str(d2 / "dup.ivy")) is None
        resolver.cleanup_staging()

    def test_resolve_uses_staging_for_disambiguation(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "quic"
        quic.mkdir()
        (quic / "types.ivy").write_text("# quic version")
        (quic / "user.ivy").write_text("include types")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "types.ivy").write_text("# apt version")
        resolver = IncludeResolver(str(tmp_path), exclude_paths=["apt"])
        resolver.create_staging_directory()
        # Resolve from a file NOT in same dir as types.ivy
        result = resolver.resolve("types", str(tmp_path / "other.ivy"))
        assert result is not None
        assert "quic" in result
        assert "apt" not in result
        resolver.cleanup_staging()


class TestIncludePaths:
    def test_include_paths_restricts_to_specified_dirs(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "protocol-testing" / "quic"
        quic.mkdir(parents=True)
        (quic / "model.ivy").write_text("")
        http = tmp_path / "protocol-testing" / "http"
        http.mkdir(parents=True)
        (http / "model.ivy").write_text("")
        (tmp_path / "root.ivy").write_text("")
        resolver = IncludeResolver(
            str(tmp_path),
            include_paths=["protocol-testing/quic"],
        )
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert "quic" in files[0]

    def test_include_paths_multiple(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "quic"
        quic.mkdir()
        (quic / "a.ivy").write_text("")
        http = tmp_path / "http"
        http.mkdir()
        (http / "b.ivy").write_text("")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "c.ivy").write_text("")
        resolver = IncludeResolver(
            str(tmp_path),
            include_paths=["quic", "http"],
        )
        files = resolver.find_all_ivy_files()
        names = {os.path.basename(f) for f in files}
        assert names == {"a.ivy", "b.ivy"}

    def test_include_paths_empty_includes_all(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        (tmp_path / "a.ivy").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.ivy").write_text("")
        resolver = IncludeResolver(str(tmp_path), include_paths=[])
        files = resolver.find_all_ivy_files()
        assert len(files) == 2

    def test_include_and_exclude_combined(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "protocol-testing" / "quic"
        quic.mkdir(parents=True)
        (quic / "model.ivy").write_text("")
        quic_test = quic / "test_stuff"
        quic_test.mkdir()
        (quic_test / "skip.ivy").write_text("")
        apt = tmp_path / "protocol-testing" / "apt"
        apt.mkdir(parents=True)
        (apt / "apt_model.ivy").write_text("")
        resolver = IncludeResolver(
            str(tmp_path),
            include_paths=["protocol-testing/quic"],
            exclude_paths=["protocol-testing/quic/test_stuff"],
        )
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert "model.ivy" in files[0]

    def test_include_paths_with_staging(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        quic = tmp_path / "quic"
        quic.mkdir()
        (quic / "types.ivy").write_text("# quic")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "types.ivy").write_text("# apt")
        resolver = IncludeResolver(
            str(tmp_path),
            include_paths=["quic"],
        )
        staging = resolver.create_staging_directory()
        assert os.path.islink(os.path.join(staging, "types.ivy"))
        target = os.path.realpath(os.path.join(staging, "types.ivy"))
        assert "quic" in target
        files = resolver.find_all_ivy_files()
        assert len(files) == 1
        assert "quic" in files[0]
        resolver.cleanup_staging()


class TestPartitionedStagingIdempotent:
    """Step 1.1/1.2: Calling build_partitioned_staging() twice must not raise Errno 17."""

    def test_partitioned_staging_idempotent(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        # Create workspace with basename collisions (quic/types.ivy vs apt/types.ivy)
        quic = tmp_path / "quic"
        quic.mkdir()
        (quic / "types.ivy").write_text("# quic types")
        (quic / "model.ivy").write_text("# quic model")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "types.ivy").write_text("# apt types")
        (apt / "model.ivy").write_text("# apt model")

        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()

        # Verify collision was detected
        assert "types.ivy" in resolver.collision_map

        # Build test scopes: two tests that need different types.ivy variants
        test_scopes = {
            str(quic / "model.ivy"): frozenset(
                {str(quic / "types.ivy"), str(quic / "model.ivy")}
            ),
            str(apt / "model.ivy"): frozenset(
                {str(apt / "types.ivy"), str(apt / "model.ivy")}
            ),
        }

        # First call — should succeed
        resolver.build_partitioned_staging(test_scopes)

        # Second call — must NOT raise [Errno 17] File exists
        resolver.build_partitioned_staging(test_scopes)

        # Verify partitions are still valid after double-call
        assert len(resolver._partition_staging) > 0
        for part_id, part_dir in resolver._partition_staging.items():
            assert os.path.isdir(part_dir)
            # Verify symlinks exist and are valid
            for entry in os.scandir(part_dir):
                if entry.is_symlink():
                    assert os.path.exists(
                        entry.path
                    ), f"Dangling symlink in {part_id}: {entry.name}"

        resolver.cleanup_staging()


class TestPartitionStaleSymlinkCleanup:
    """Step 1.2: Pre-existing symlinks in partition dirs are cleaned before repopulation."""

    def test_partition_stale_symlink_cleanup(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        # Create workspace with collisions
        quic = tmp_path / "quic"
        quic.mkdir()
        (quic / "types.ivy").write_text("# quic types")
        (quic / "driver.ivy").write_text("# quic driver")
        apt = tmp_path / "apt"
        apt.mkdir()
        (apt / "types.ivy").write_text("# apt types")
        (apt / "driver.ivy").write_text("# apt driver")

        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()

        test_scopes = {
            str(quic / "driver.ivy"): frozenset(
                {str(quic / "types.ivy"), str(quic / "driver.ivy")}
            ),
            str(apt / "driver.ivy"): frozenset(
                {str(apt / "types.ivy"), str(apt / "driver.ivy")}
            ),
        }

        # Build partitioned staging once
        resolver.build_partitioned_staging(test_scopes)

        # Manually inject a stale symlink into one of the partition dirs
        some_part_dir = list(resolver._partition_staging.values())[0]
        stale_link = os.path.join(some_part_dir, "stale_leftover.ivy")
        os.symlink("/nonexistent/path/stale.ivy", stale_link)
        assert os.path.lexists(stale_link), "Stale symlink should exist before cleanup"

        # Rebuild partitioned staging — should clean the stale symlink
        resolver.build_partitioned_staging(test_scopes)

        # The stale symlink should have been removed
        assert not os.path.lexists(
            stale_link
        ), "Stale symlink should have been cleaned by rebuild"

        # Valid symlinks should still exist
        for part_dir in resolver._partition_staging.values():
            entries = list(os.scandir(part_dir))
            assert len(entries) > 0, f"Partition dir {part_dir} should have symlinks"
            for entry in entries:
                assert entry.is_symlink(), f"{entry.name} should be a symlink"

        resolver.cleanup_staging()


class TestLayerAwareFileDiscovery:
    """Tests for v3 layer-aware file discovery and collision classification."""

    def _make_workspace_with_layers(self, tmp_path):
        """Create a workspace with two layers (standard + apt) sharing basenames."""
        from ivy_lsp.workspace_detection import WorkspaceLayer

        quic = tmp_path / "protocol-testing" / "quic" / "quic_stack"
        quic.mkdir(parents=True)
        (quic / "types.ivy").write_text("# standard types")
        (quic / "frame.ivy").write_text("# standard frame")

        apt = tmp_path / "protocol-testing" / "apt" / "apt_protocols" / "quic"
        apt.mkdir(parents=True)
        (apt / "types.ivy").write_text("# apt types")
        (apt / "attack.ivy").write_text("# apt attack")

        layers = [
            WorkspaceLayer(
                id="standard",
                include_paths=["protocol-testing/quic"],
                priority=1,
            ),
            WorkspaceLayer(
                id="apt",
                include_paths=["protocol-testing/apt"],
                priority=2,
            ),
        ]
        return layers

    def test_find_source_files_populates_file_to_layer(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        files = resolver._find_source_files()

        # _file_to_layer should be populated by the layered path
        assert len(resolver._file_to_layer) > 0
        layer_ids = set(resolver._file_to_layer.values())
        assert "standard" in layer_ids
        assert "apt" in layer_ids
        # Should find files from both layers
        assert len(files) == 4

    def test_cross_layer_collision_no_warning(self, tmp_path, caplog):
        import logging

        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)

        with caplog.at_level(
            logging.WARNING, logger="ivy_lsp.indexer.include_resolver"
        ):
            resolver.create_staging_directory()

        # types.ivy exists in both layers — should NOT produce a WARNING
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        intra_layer_warnings = [
            m for m in warning_messages if "Intra-layer collision" in m
        ]
        assert (
            len(intra_layer_warnings) == 0
        ), f"Cross-layer collision should not produce WARNING, got: {intra_layer_warnings}"
        resolver.cleanup_staging()

    def test_intra_layer_collision_warning(self, tmp_path, caplog):
        import logging

        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.workspace_detection import WorkspaceLayer

        # Create a single layer with duplicate basenames
        sub1 = tmp_path / "proto" / "dir1"
        sub1.mkdir(parents=True)
        (sub1 / "dup.ivy").write_text("# version 1")
        sub2 = tmp_path / "proto" / "dir2"
        sub2.mkdir(parents=True)
        (sub2 / "dup.ivy").write_text("# version 2")

        layers = [
            WorkspaceLayer(id="single", include_paths=["proto"], priority=1),
        ]
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)

        with caplog.at_level(
            logging.WARNING, logger="ivy_lsp.indexer.include_resolver"
        ):
            resolver.create_staging_directory()

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        intra_layer_warnings = [
            m for m in warning_messages if "Intra-layer collision" in m
        ]
        assert (
            len(intra_layer_warnings) >= 1
        ), "Intra-layer collision should produce a WARNING"
        resolver.cleanup_staging()

    def test_build_layered_staging_creates_layer_dirs(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        # Should have per-layer staging directories
        assert "standard" in resolver._partition_staging
        assert "apt" in resolver._partition_staging

        # Each layer dir should exist and contain symlinks
        for layer_id, layer_dir in resolver._partition_staging.items():
            assert os.path.isdir(layer_dir), f"Layer dir {layer_id} should exist"
            entries = list(os.scandir(layer_dir))
            assert len(entries) > 0, f"Layer dir {layer_id} should have symlinks"

        # Files should be mapped to their layer partition
        assert len(resolver._file_to_partition) > 0
        resolver.cleanup_staging()

    def test_resolve_uses_layer_staging_not_flat(self, tmp_path):
        """resolve() from a QUIC file returns QUIC types, from APT returns APT types."""
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        quic_frame = str(
            tmp_path / "protocol-testing" / "quic" / "quic_stack" / "frame.ivy"
        )
        apt_attack = str(
            tmp_path
            / "protocol-testing"
            / "apt"
            / "apt_protocols"
            / "quic"
            / "attack.ivy"
        )

        # Resolve types from QUIC file → should get QUIC types
        result_quic = resolver.resolve("types", quic_frame)
        assert result_quic is not None
        assert (
            "quic" in result_quic.lower() and "apt" not in result_quic.lower()
        ), f"Expected QUIC types.ivy, got {result_quic}"

        # Resolve types from APT file → should get APT types
        result_apt = resolver.resolve("types", apt_attack)
        assert result_apt is not None
        assert "apt" in result_apt.lower(), f"Expected APT types.ivy, got {result_apt}"

        # The two should be different files
        assert result_quic != result_apt
        resolver.cleanup_staging()

    def test_resolve_collision_returns_none_without_layer_context(self, tmp_path):
        """Colliding basenames return None when from_file is outside all layers.

        When layers are active but from_file is NOT in any layer,
        colliding basenames must return None (not silently serve wrong file).
        """
        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        # A file not in any layer's scope
        outsider = str(tmp_path / "outsider.ivy")

        # types.ivy is in the collision map — should refuse to resolve via flat staging
        result = resolver.resolve("types", outsider)
        assert result is None, f"Should return None for ambiguous include, got {result}"
        resolver.cleanup_staging()

    def test_staging_collision_debug_with_layers(self, tmp_path, caplog):
        """With layers, flat staging collision logged at DEBUG (not WARNING/ERROR)."""
        import logging

        from ivy_lsp.indexer.include_resolver import IncludeResolver

        layers = self._make_workspace_with_layers(tmp_path)
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)

        with caplog.at_level(logging.DEBUG, logger="ivy_lsp.indexer.include_resolver"):
            resolver.create_staging_directory()

        # Should have DEBUG "layer-handled" messages, NOT WARNING or ERROR
        debug_msgs = [
            r
            for r in caplog.records
            if "Staging collision" in r.message and r.levelno == logging.DEBUG
        ]
        warn_or_error_msgs = [
            r
            for r in caplog.records
            if "Staging collision" in r.message and r.levelno >= logging.WARNING
        ]
        assert len(debug_msgs) >= 1, "Expected DEBUG staging collision messages"
        assert len(warn_or_error_msgs) == 0, (
            f"Expected no WARNING/ERROR staging collisions with layers, got: "
            f"{[r.message for r in warn_or_error_msgs]}"
        )
        resolver.cleanup_staging()

    def test_staging_collision_error_without_layers(self, tmp_path, caplog):
        """Without layers, flat staging collision logged at ERROR."""
        import logging

        from ivy_lsp.indexer.include_resolver import IncludeResolver

        # No layers — two dirs with same basename
        d1 = tmp_path / "aa"
        d1.mkdir()
        (d1 / "dup.ivy").write_text("# first")
        d2 = tmp_path / "bb"
        d2.mkdir()
        (d2 / "dup.ivy").write_text("# second")

        resolver = IncludeResolver(str(tmp_path))

        with caplog.at_level(logging.DEBUG, logger="ivy_lsp.indexer.include_resolver"):
            resolver.create_staging_directory()

        error_msgs = [
            r
            for r in caplog.records
            if "Staging collision" in r.message and r.levelno == logging.ERROR
        ]
        assert len(error_msgs) >= 1, "Expected ERROR staging collision without layers"
        resolver.cleanup_staging()

    def test_intra_layer_collision_is_error(self, tmp_path, caplog):
        """Single layer with two same-name files → logged at ERROR."""
        import logging

        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.workspace_detection import WorkspaceLayer

        sub1 = tmp_path / "proto" / "dir1"
        sub1.mkdir(parents=True)
        (sub1 / "dup.ivy").write_text("# version 1")
        sub2 = tmp_path / "proto" / "dir2"
        sub2.mkdir(parents=True)
        (sub2 / "dup.ivy").write_text("# version 2")

        layers = [
            WorkspaceLayer(id="single", include_paths=["proto"], priority=1),
        ]
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)

        with caplog.at_level(logging.DEBUG, logger="ivy_lsp.indexer.include_resolver"):
            resolver.create_staging_directory()

        error_msgs = [
            r
            for r in caplog.records
            if "Intra-layer collision" in r.message and r.levelno == logging.ERROR
        ]
        assert len(error_msgs) >= 1, "Intra-layer collision should be logged at ERROR"
        resolver.cleanup_staging()
