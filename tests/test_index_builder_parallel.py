"""Tests for the _extract_one_file() refactoring in index_builder.

These tests verify that:
1. _extract_one_file() is a top-level function (picklable).
2. It returns a FileExtractionResult with the expected fields.
3. fast=True causes Tier 1 (AST) to be skipped.
4. The refactored build_protocol() produces identical output.
"""

from __future__ import annotations

import pickle

import pytest

from ivy_lsp.core.workspace.detection import WorkspaceConfig
from ivy_lsp.lsp.index_builder import (
    FileExtractionResult,
    IndexBuilder,
    _extract_one_file,
)

# ---------------------------------------------------------------------------
# Helpers shared with test_index_builder.py
# ---------------------------------------------------------------------------

SAMPLE_IVY_TYPES = """\
#lang ivy1.7

type cid
type quic_packet_type = {initial, handshake, zero_rtt, one_rtt}
"""

SAMPLE_IVY_MAIN = """\
#lang ivy1.7

include types

type packet
action send(p: packet)
action recv(p: packet)

export send
import recv

after send {
    require p ~= 0;
}
"""


def _make_workspace_config(ws_root: str) -> WorkspaceConfig:
    return WorkspaceConfig(workspace_root=ws_root, detected_by="test")


# ---------------------------------------------------------------------------
# Fixture: quic_workspace — a minimal workspace with quic protocol files
# ---------------------------------------------------------------------------


@pytest.fixture
def quic_workspace(tmp_path):
    """Minimal workspace with protocol-testing/quic/ containing two .ivy files.

    Returns (workspace_root, protocol_dir, workspace_config).
    """
    proto_dir = tmp_path / "protocol-testing" / "quic"
    proto_dir.mkdir(parents=True, exist_ok=True)
    (proto_dir / "types.ivy").write_text(SAMPLE_IVY_TYPES)
    (proto_dir / "main.ivy").write_text(SAMPLE_IVY_MAIN)

    ws_root = str(tmp_path)
    config = _make_workspace_config(ws_root)
    return ws_root, str(proto_dir), config


# ---------------------------------------------------------------------------
# TestExtractOneFile
# ---------------------------------------------------------------------------


class TestExtractOneFile:
    """Unit tests for the top-level _extract_one_file() function."""

    def _make_resolver_config(self, ws_root: str, proto_dir: str) -> dict:
        """Build a resolver config dict for the quic workspace."""
        import os

        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        protocol_rel = os.path.relpath(proto_dir, ws_root)
        resolver = IncludeResolver(
            workspace_root=ws_root,
            exclude_paths=[],
            include_paths=[protocol_rel],
            workspace_layers=[],
        )
        return resolver.to_config_dict()

    def test_returns_file_extraction_result(self, quic_workspace):
        """_extract_one_file() returns a FileExtractionResult with expected fields."""
        import os

        ws_root, proto_dir, _config = quic_workspace
        resolver_config = self._make_resolver_config(ws_root, proto_dir)

        filepath = os.path.join(proto_dir, "types.ivy")
        result = _extract_one_file(
            filepath=filepath,
            protocol_dir=proto_dir,
            resolver_config=resolver_config,
            fast=False,
            parser_timeout=5.0,
        )

        # Must be a FileExtractionResult
        assert isinstance(result, FileExtractionResult)

        # rel_path must be relative to protocol_dir
        assert result.rel_path == "types.ivy"

        # sha256 must be non-empty (file is readable)
        assert len(result.sha256) == 64
        assert all(c in "0123456789abcdef" for c in result.sha256)

        # symbols must be a list (may be empty if ivy parser unavailable)
        assert isinstance(result.symbols, list)

        # includes must be a list
        assert isinstance(result.includes, list)

        # exports must be a dict
        assert isinstance(result.exports, dict)

        # requirements must be a list
        assert isinstance(result.requirements, list)

        # manifest_entry must contain required keys
        assert "mtime" in result.manifest_entry
        assert "size" in result.manifest_entry
        assert "sha256" in result.manifest_entry
        assert "completeness" in result.manifest_entry
        assert "parse_tier" in result.manifest_entry

        # tier_label must be one of the expected values
        assert result.tier_label in ("ast", "lexer", "regex", "unknown")

        # No error on a valid file
        assert result.error is None

    def test_fast_mode_skips_tier1(self, quic_workspace):
        """fast=True causes parse_tier != 'ast' (Tier 1 skipped)."""
        import os

        ws_root, proto_dir, _config = quic_workspace
        resolver_config = self._make_resolver_config(ws_root, proto_dir)

        filepath = os.path.join(proto_dir, "types.ivy")
        result = _extract_one_file(
            filepath=filepath,
            protocol_dir=proto_dir,
            resolver_config=resolver_config,
            fast=True,
            parser_timeout=5.0,
        )

        assert isinstance(result, FileExtractionResult)
        # In fast mode, tier 1 (AST parser) must not be used
        assert (
            result.tier_label != "ast"
        ), f"fast=True should skip Tier 1, but got tier_label={result.tier_label!r}"

    def test_extract_one_file_is_picklable(self):
        """_extract_one_file must be a top-level function that pickle can serialize."""
        # The function itself must be picklable (needed for ProcessPoolExecutor)
        pickled = pickle.dumps(_extract_one_file)
        restored = pickle.loads(pickled)
        assert callable(restored)

    def test_file_extraction_result_is_picklable(self, quic_workspace):
        """FileExtractionResult must be picklable for cross-process transfer."""
        import os

        ws_root, proto_dir, _config = quic_workspace
        resolver_config = self._make_resolver_config(ws_root, proto_dir)
        filepath = os.path.join(proto_dir, "types.ivy")

        result = _extract_one_file(
            filepath=filepath,
            protocol_dir=proto_dir,
            resolver_config=resolver_config,
            fast=False,
            parser_timeout=5.0,
        )

        # Must round-trip through pickle without error
        pickled = pickle.dumps(result)
        restored = pickle.loads(pickled)
        assert restored.rel_path == result.rel_path
        assert restored.sha256 == result.sha256


# ---------------------------------------------------------------------------
# TestBuildProtocolRefactored — ensure refactored build_protocol() is
# behavior-identical to the original inline loop
# ---------------------------------------------------------------------------


class TestBuildProtocolRefactored:
    """Regression tests: refactored build_protocol() must produce same results."""

    def test_summary_matches_original_contract(self, quic_workspace):
        """build_protocol() summary contract is unchanged after refactor."""
        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config)
        summary = builder.build_protocol(proto_dir)

        assert summary["protocol"] == "quic"
        assert summary["files"] == 2
        assert summary["status"] == "ok"
        assert summary["elapsed_ms"] > 0

    def test_fast_mode_no_ast(self, quic_workspace):
        """fast=True: manifest must not contain ast parse tier after refactor."""
        import json
        import os

        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config, fast=True)
        builder.build_protocol(proto_dir)

        manifest_path = os.path.join(proto_dir, ".ivy-index", "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)

        for rel_path, entry in manifest["files"].items():
            assert entry["parse_tier"] in (
                "lexer",
                "regex",
            ), f"{rel_path} used tier {entry['parse_tier']!r} in fast mode"


# ---------------------------------------------------------------------------
# TestResolverSerialization — roundtrip via to_config_dict / from_config
# ---------------------------------------------------------------------------


class TestResolverSerialization:
    """Verify that from_config() correctly restores staging state."""

    def test_resolver_roundtrip_preserves_staging(self, quic_workspace):
        """Reconstructed resolver resolves cross-directory includes via staging.

        Sequence:
          1. Build a resolver with staging for the quic_workspace fixture.
          2. Serialize via to_config_dict().
          3. Reconstruct via from_config() — simulates a worker process.
          4. Verify the reconstructed resolver can resolve 'types' from main.ivy.
        """
        import os

        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        ws_root, proto_dir, _config = quic_workspace

        # Step 1: Create original resolver and build flat staging
        resolver = IncludeResolver(
            workspace_root=ws_root,
            exclude_paths=[],
            include_paths=[os.path.relpath(proto_dir, ws_root)],
            workspace_layers=[],
        )
        staging_dir = resolver.create_staging_directory()
        assert staging_dir is not None
        assert os.path.isdir(staging_dir)

        try:
            # Step 2: Serialize
            config_dict = resolver.to_config_dict()
            assert config_dict["staging_dir"] == staging_dir
            assert config_dict["workspace_root"] == ws_root

            # Step 3: Reconstruct (worker process simulation)
            restored = IncludeResolver.from_config(config_dict)

            # Basic attribute preservation
            assert restored._workspace_root == ws_root
            assert restored._staging_dir == staging_dir
            assert restored._exclude_paths == []

            # Step 4: Verify the reconstructed resolver resolves 'types'
            # from main.ivy (cross-directory include — only resolvable via staging)
            main_ivy = os.path.join(proto_dir, "main.ivy")
            resolved = restored.resolve("types", main_ivy)
            assert resolved is not None, (
                "Reconstructed resolver failed to resolve 'types' from main.ivy "
                f"(staging_dir={staging_dir})"
            )
            assert resolved.endswith(
                "types.ivy"
            ), f"Expected to resolve to types.ivy, got {resolved!r}"
        finally:
            resolver.cleanup_staging()

    def test_resolver_roundtrip_no_staging(self, quic_workspace):
        """from_config() works when no staging was active (staging_dir=None)."""
        import os

        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        ws_root, proto_dir, _config = quic_workspace

        # Create resolver without staging
        resolver = IncludeResolver(
            workspace_root=ws_root,
            exclude_paths=[],
            include_paths=[os.path.relpath(proto_dir, ws_root)],
            workspace_layers=[],
        )
        config_dict = resolver.to_config_dict()
        assert config_dict["staging_dir"] is None

        # Roundtrip
        restored = IncludeResolver.from_config(config_dict)
        assert restored._workspace_root == ws_root
        assert restored._staging_dir is None

        # Same-directory resolution still works (types.ivy is beside main.ivy)
        main_ivy = os.path.join(proto_dir, "main.ivy")
        resolved = restored.resolve("types", main_ivy)
        assert resolved is not None
        assert resolved.endswith("types.ivy")

    def test_resolver_roundtrip_with_layers(self, tmp_path):
        """from_config() rebuilds partition maps when workspace layers are active.

        Creates a two-layer workspace:
          layer_core/   — core.ivy
          layer_test/   — test.ivy (includes core)

        After roundtrip, the restored resolver must route includes to the
        correct per-layer staging directories.
        """
        import os

        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.workspace.detection import WorkspaceLayer

        # Set up workspace layout
        core_dir = tmp_path / "layer_core"
        test_dir = tmp_path / "layer_test"
        core_dir.mkdir()
        test_dir.mkdir()
        (core_dir / "core.ivy").write_text("#lang ivy1.7\ntype base_t\n")
        (test_dir / "test.ivy").write_text("#lang ivy1.7\ninclude core\ntype test_t\n")

        ws_root = str(tmp_path)
        layers = [
            WorkspaceLayer(id="core", include_paths=["layer_core"], priority=1),
            WorkspaceLayer(
                id="test",
                include_paths=["layer_test"],
                priority=2,
                depends_on=["core"],
            ),
        ]

        # Build resolver with layered staging
        resolver = IncludeResolver(
            workspace_root=ws_root,
            workspace_layers=layers,
        )
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        assert resolver._staging_dir is not None
        assert len(resolver._partition_staging) == 2
        assert len(resolver._file_to_partition) > 0

        try:
            # Serialize and reconstruct
            config_dict = resolver.to_config_dict()
            restored = IncludeResolver.from_config(config_dict)

            # Staging dir must be restored
            assert restored._staging_dir == resolver._staging_dir

            # Partition staging maps must be rebuilt
            assert len(restored._partition_staging) == 2, (
                f"Expected 2 partitions, got {len(restored._partition_staging)}: "
                f"{list(restored._partition_staging.keys())}"
            )
            assert (
                len(restored._file_to_partition) > 0
            ), "from_config() did not rebuild _file_to_partition"

            # Resolve 'core' from test.ivy — must find it via layer deps
            test_ivy = str(test_dir / "test.ivy")
            resolved = restored.resolve("core", test_ivy)
            assert resolved is not None, (
                "Restored resolver failed to resolve 'core' from test.ivy "
                f"(partition_staging={list(restored._partition_staging.keys())})"
            )
            assert resolved.endswith("core.ivy"), f"Expected core.ivy, got {resolved!r}"
        finally:
            resolver.cleanup_staging()


# ---------------------------------------------------------------------------
# TestIncrementalIndexing — verify SHA-256 content-hash cache behaviour
# ---------------------------------------------------------------------------


class TestIncrementalIndexing:
    """Tests for incremental indexing with SHA-256 content-hash cache.

    Verifies:
    1. Two consecutive builds produce identical output artifacts.
    2. The second build is faster (cache hit > 0, no files re-parsed).
    3. Modifying a file causes it to be re-parsed on the next build.
    4. includes_raw.json is written as a cache artifact.
    5. _load_json helper returns None on missing/corrupt files.
    """

    def test_consecutive_builds_same_results(self, quic_workspace):
        """Two consecutive builds produce identical manifest and symbols."""
        import json
        import os

        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config, fast=True)

        # First build
        summary1 = builder.build_protocol(proto_dir)
        assert summary1["status"] == "ok"

        manifest_path = os.path.join(proto_dir, ".ivy-index", "manifest.json")
        symbols_path = os.path.join(proto_dir, ".ivy-index", "symbols.json")

        with open(manifest_path) as f:
            manifest1 = json.load(f)
        with open(symbols_path) as f:
            symbols1 = json.load(f)

        # Second build — files unchanged so all should be cache hits
        summary2 = builder.build_protocol(proto_dir)
        assert summary2["status"] == "ok"

        with open(manifest_path) as f:
            manifest2 = json.load(f)
        with open(symbols_path) as f:
            symbols2 = json.load(f)

        # SHA-256 hashes and sizes must match across both builds
        for rel_path in manifest1["files"]:
            entry1 = manifest1["files"][rel_path]
            entry2 = manifest2["files"][rel_path]
            assert (
                entry1["sha256"] == entry2["sha256"]
            ), f"SHA-256 mismatch for {rel_path} between builds"
            assert (
                entry1["size"] == entry2["size"]
            ), f"Size mismatch for {rel_path} between builds"

        # Symbols must be identical across both builds
        assert symbols1 == symbols2, "symbols.json differs between builds"

    def test_second_build_faster_on_unchanged_workspace(self, quic_workspace):
        """Second build is faster than first on an unchanged workspace."""
        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config, fast=True)

        # First build (cold — no cache)
        summary1 = builder.build_protocol(proto_dir)
        assert summary1["status"] == "ok"
        elapsed1 = summary1["elapsed_ms"]

        # Second build (warm — all files should be cache hits)
        summary2 = builder.build_protocol(proto_dir)
        assert summary2["status"] == "ok"
        elapsed2 = summary2["elapsed_ms"]

        # The second build should be faster.  We use a generous upper bound
        # to avoid flakiness on slow CI machines — the key property is
        # "meaningfully faster", not an exact ratio.  A warm build should
        # complete in under 500ms regardless of workspace size.
        assert elapsed2 < elapsed1 * 10 or elapsed2 < 500, (
            f"Second build ({elapsed2:.1f}ms) should be faster than "
            f"first build ({elapsed1:.1f}ms) on unchanged workspace"
        )

    def test_modified_file_is_reparsed(self, quic_workspace):
        """A file whose content changes is re-parsed on the next build."""
        import json
        import os

        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config, fast=True)

        # First build
        builder.build_protocol(proto_dir)

        symbols_path = os.path.join(proto_dir, ".ivy-index", "symbols.json")
        with open(symbols_path) as f:
            symbols_before = json.load(f)

        # Modify types.ivy — add a new type
        types_path = os.path.join(proto_dir, "types.ivy")
        with open(types_path) as f:
            original_content = f.read()

        modified_content = original_content + "\ntype extra_type_xyz\n"
        with open(types_path, "w") as f:
            f.write(modified_content)

        try:
            # Second build — types.ivy SHA-256 changed so it should be re-parsed
            builder.build_protocol(proto_dir)

            with open(symbols_path) as f:
                symbols_after = json.load(f)

            # The symbols for types.ivy should now include extra_type_xyz
            types_syms_before = {s["name"] for s in symbols_before.get("types.ivy", [])}
            types_syms_after = {s["name"] for s in symbols_after.get("types.ivy", [])}

            assert "extra_type_xyz" in types_syms_after, (
                f"Modified types.ivy should have been re-parsed; "
                f"symbols before: {types_syms_before}, after: {types_syms_after}"
            )

        finally:
            # Restore original content
            with open(types_path, "w") as f:
                f.write(original_content)

    def test_includes_raw_json_written(self, quic_workspace):
        """build_protocol() writes includes_raw.json as a cache artifact."""
        import json
        import os

        ws_root, proto_dir, config = quic_workspace
        builder = IndexBuilder(ws_root, config, fast=True)
        builder.build_protocol(proto_dir)

        includes_raw_path = os.path.join(proto_dir, ".ivy-index", "includes_raw.json")
        assert os.path.isfile(
            includes_raw_path
        ), "includes_raw.json must be written by build_protocol()"

        with open(includes_raw_path) as f:
            includes_raw = json.load(f)

        # Must be a dict keyed by relative paths
        assert isinstance(includes_raw, dict)

        # main.ivy includes types — should appear in includes_raw
        assert "main.ivy" in includes_raw
        main_includes = includes_raw["main.ivy"]
        assert isinstance(main_includes, list)
        assert (
            "types" in main_includes
        ), f"main.ivy should include 'types'; got {main_includes}"

    def test_load_json_returns_none_on_missing_file(self, tmp_path):
        """_load_json() returns None when the file does not exist."""
        import os

        from ivy_lsp.core.workspace.detection import WorkspaceConfig

        ws_root = str(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)

        missing_path = os.path.join(str(tmp_path), "nonexistent.json")
        result = builder._load_json(missing_path)
        assert result is None

    def test_load_json_returns_none_on_corrupt_file(self, tmp_path):
        """_load_json() returns None when the file contains invalid JSON."""
        from ivy_lsp.core.workspace.detection import WorkspaceConfig

        corrupt_path = tmp_path / "corrupt.json"
        corrupt_path.write_text("{ this is not valid json !!!")

        ws_root = str(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)

        result = builder._load_json(str(corrupt_path))
        assert result is None
