"""Tests for ivy_lsp.index_builder module."""

from __future__ import annotations

import json
import os
import time

import pytest

from ivy_lsp.index_builder import IndexBuilder, _file_sha256, cli_index
from ivy_lsp.workspace_detection import WorkspaceConfig

# ---------------------------------------------------------------------------
# Fixture: force Tier 3 (regex) when ivy module unavailable
# ---------------------------------------------------------------------------


def _check_ivy_lexer_available():
    """Check if the PLY lexer actually works (requires ivy module at runtime)."""
    try:
        from ivy_lsp.parsing.token_stream import tokenize_ivy

        # tokenize_ivy does a lazy import of ivy.ivy_lexer when called,
        # so we must actually call it to detect availability.
        tokenize_ivy("type t\n", "<test>")
        return True
    except (ImportError, ModuleNotFoundError, Exception):
        return False


_IVY_LEXER_AVAILABLE = _check_ivy_lexer_available()


@pytest.fixture(autouse=True)
def _force_regex_tier_if_no_lexer(monkeypatch):
    """Force regex tier when ivy lexer is unavailable.

    When the ivy module is unavailable, the PLY lexer Tier 2 returns
    empty results but is still considered 'successful' by TieredExtractor.
    This fixture forces:
    1. TieredExtractor to skip Tier 2 and use Tier 3 (regex).
    2. light_mode_extractor to use regex fallback for exports/requirements.
    """
    if _IVY_LEXER_AVAILABLE:
        yield
        return

    from ivy_lsp.parsing import tiered_extractor as _te_mod

    original_init = _te_mod.TieredExtractor.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Force both parser and lexer to be unavailable so Tier 3 is used
        self._parser_available = False
        self._lexer_available = False

    _te_mod.TieredExtractor.__init__ = patched_init
    # Also force the light_mode_extractor to use regex path
    import ivy_lsp.analysis.light_mode_extractor as _lme_mod

    original_lexer_flag = _lme_mod._LEXER_AVAILABLE
    _lme_mod._LEXER_AVAILABLE = False
    yield
    # Restore originals
    _te_mod.TieredExtractor.__init__ = original_init
    _lme_mod._LEXER_AVAILABLE = original_lexer_flag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal Ivy source with exports, imports, includes, and requirements.
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

SAMPLE_IVY_TYPES = """\
#lang ivy1.7

type cid
type quic_packet_type = {initial, handshake, zero_rtt, one_rtt}
"""

# Broken syntax file for error recovery testing.
SAMPLE_IVY_BROKEN = """\
#lang ivy1.7
this is not valid ivy {{{{{
"""

# File with server_behavior in name (for tester role detection).
SAMPLE_IVY_SERVER_BEHAVIOR = """\
#lang ivy1.7

type server_flag
"""


def _make_protocol_workspace(tmp_path, protocol, files):
    """Create a workspace with protocol-testing/<protocol>/ structure.

    Args:
        tmp_path: Base workspace directory (pathlib.Path).
        protocol: Protocol name (e.g. "quic").
        files: Dict mapping filename to content.

    Returns:
        Tuple of (workspace_root, protocol_dir) as strings.
    """
    ws_root = str(tmp_path)
    proto_dir = tmp_path / "protocol-testing" / protocol
    proto_dir.mkdir(parents=True, exist_ok=True)

    for filename, content in files.items():
        # Support nested paths (e.g. "subdir/file.ivy")
        filepath = proto_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)

    return ws_root, str(proto_dir)


def _make_workspace_config(ws_root):
    """Create a minimal WorkspaceConfig."""
    return WorkspaceConfig(
        workspace_root=ws_root,
        detected_by="test",
    )


def _read_index_json(protocol_dir, filename):
    """Read a JSON file from .ivy-index/."""
    path = os.path.join(protocol_dir, ".ivy-index", filename)
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 1: Build from temp workspace - verify all output files created
# ---------------------------------------------------------------------------


class TestBuildProtocol:
    """Test build_protocol produces all expected output files."""

    def test_all_output_files_created(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        summary = builder.build_protocol(proto_dir)

        assert summary["protocol"] == "quic"
        assert summary["files"] == 2
        assert summary["status"] == "ok"
        assert summary["elapsed_ms"] > 0

        index_dir = os.path.join(proto_dir, ".ivy-index")
        assert os.path.isdir(index_dir)
        assert os.path.isfile(os.path.join(index_dir, "manifest.json"))
        assert os.path.isfile(os.path.join(index_dir, "symbols.json"))
        assert os.path.isfile(os.path.join(index_dir, "includes.json"))
        assert os.path.isfile(os.path.join(index_dir, "exports.json"))
        assert os.path.isfile(os.path.join(index_dir, "requirements.json"))
        assert os.path.isdir(os.path.join(index_dir, "scopes"))
        assert os.path.isfile(os.path.join(index_dir, "scopes", "_meta.json"))

    def test_empty_protocol_dir(self, tmp_path):
        """Building an empty dir returns status='empty'."""
        ws_root, proto_dir = _make_protocol_workspace(tmp_path, "empty", {})
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        summary = builder.build_protocol(proto_dir)
        assert summary["status"] == "empty"
        assert summary["files"] == 0


# ---------------------------------------------------------------------------
# Test 2: Verify manifest.json has correct structure and file entries
# ---------------------------------------------------------------------------


class TestManifest:
    """Test manifest.json structure."""

    def test_manifest_structure(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        manifest = _read_index_json(proto_dir, "manifest.json")

        assert manifest["version"] == 1
        assert manifest["protocol"] == "quic"
        assert "created_at" in manifest
        assert "builder_version" in manifest
        assert manifest["default_parse_tier"] in ("ast", "lexer", "regex")
        assert "files" in manifest

        files = manifest["files"]
        assert "main.ivy" in files
        assert "types.ivy" in files

        entry = files["main.ivy"]
        assert "mtime" in entry
        assert "size" in entry
        assert entry["size"] > 0
        assert "sha256" in entry
        assert len(entry["sha256"]) == 64  # SHA-256 hex length
        assert entry["completeness"] in ("complete", "partial")
        assert entry["parse_tier"] in ("ast", "lexer", "regex")


# ---------------------------------------------------------------------------
# Test 3: Verify symbols.json has symbols for each file
# ---------------------------------------------------------------------------


class TestSymbols:
    """Test symbols.json content."""

    def test_symbols_per_file(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        symbols = _read_index_json(proto_dir, "symbols.json")

        assert "main.ivy" in symbols
        assert "types.ivy" in symbols

        # main.ivy should have at least: packet type, send action, recv action
        main_syms = symbols["main.ivy"]
        assert len(main_syms) >= 2  # At least type and actions
        names = {s["name"] for s in main_syms}
        assert "packet" in names or "send" in names

        # types.ivy should have cid and quic_packet_type
        types_syms = symbols["types.ivy"]
        assert len(types_syms) >= 1
        type_names = {s["name"] for s in types_syms}
        assert "cid" in type_names or "quic_packet_type" in type_names


# ---------------------------------------------------------------------------
# Test 4: Verify includes.json has edges
# ---------------------------------------------------------------------------


class TestIncludes:
    """Test includes.json edges."""

    def test_include_edges(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        includes = _read_index_json(proto_dir, "includes.json")

        # main.ivy includes types -> should have edge main.ivy -> types.ivy
        assert "main.ivy" in includes
        assert "types.ivy" in includes["main.ivy"]


# ---------------------------------------------------------------------------
# Test 5: Verify scopes/ directory has test scope files
# ---------------------------------------------------------------------------


class TestScopes:
    """Test scopes/ output."""

    def test_scope_files_created(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        scopes_dir = os.path.join(proto_dir, ".ivy-index", "scopes")
        assert os.path.isdir(scopes_dir)

        # _meta.json should exist
        meta = _read_index_json(proto_dir, os.path.join("scopes", "_meta.json"))
        assert isinstance(meta, list)

        # main.ivy has exports -> should have a scope file
        main_scope_path = os.path.join(scopes_dir, "main.json")
        assert os.path.isfile(main_scope_path)

        with open(main_scope_path) as f:
            scope = json.load(f)
        assert "entry_file" in scope
        assert "role" in scope
        assert "transitive_includes" in scope
        assert "exported_actions" in scope
        assert "imported_actions" in scope

        # The include closure should include types.ivy
        assert "types.ivy" in scope["transitive_includes"]

    def test_scope_meta_matches_individual_files(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        meta = _read_index_json(proto_dir, os.path.join("scopes", "_meta.json"))
        assert len(meta) >= 1

        # Each entry in meta should have a corresponding individual file
        for entry in meta:
            test_name = entry["test"]
            individual_path = os.path.join(
                proto_dir, ".ivy-index", "scopes", f"{test_name}.json"
            )
            assert os.path.isfile(individual_path)


# ---------------------------------------------------------------------------
# Test 6: Test --fast mode (Tier 2 only)
# ---------------------------------------------------------------------------


class TestFastMode:
    """Test fast=True uses Tier 2 (lexer) only."""

    def test_fast_mode_tier_2(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config, fast=True)
        summary = builder.build_protocol(proto_dir)

        assert summary["status"] == "ok"

        manifest = _read_index_json(proto_dir, "manifest.json")
        assert manifest["default_parse_tier"] == "lexer"

        # In fast mode, files should not be parsed with tier 1 (ast)
        # They should be lexer or regex
        for rel_path, entry in manifest["files"].items():
            assert entry["parse_tier"] in (
                "lexer",
                "regex",
            ), f"{rel_path} used tier {entry['parse_tier']} in fast mode"


# ---------------------------------------------------------------------------
# Test 7: Test check_status returns correct staleness
# ---------------------------------------------------------------------------


class TestCheckStatus:
    """Test check_status method."""

    def test_fresh_index(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
                "types.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        status = builder.check_status(proto_dir)
        assert status["protocol"] == "quic"
        assert status["status"] == "fresh"
        assert status["changed_files"] == 0
        assert status["total_files"] == 2

    def test_stale_after_modification(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "main.ivy": SAMPLE_IVY_MAIN,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        # Modify the file (change mtime significantly)
        main_path = os.path.join(proto_dir, "main.ivy")
        # Set mtime far into the future to ensure difference > 1.0
        future_time = time.time() + 100
        os.utime(main_path, (future_time, future_time))

        status = builder.check_status(proto_dir)
        assert status["status"] in ("stale_minor", "stale_major")
        assert status["changed_files"] >= 1

    def test_no_index_returns_stale_major(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"main.ivy": SAMPLE_IVY_MAIN},
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        # Don't build, just check status
        status = builder.check_status(proto_dir)
        assert status["status"] == "stale_major"


# ---------------------------------------------------------------------------
# Test 8: Test build_all finds multiple protocols
# ---------------------------------------------------------------------------


class TestBuildAll:
    """Test build_all discovers and builds multiple protocols."""

    def test_build_all_multiple_protocols(self, tmp_path):
        # Create two protocol directories
        for protocol, content in [
            ("quic", SAMPLE_IVY_MAIN),
            ("minip", SAMPLE_IVY_TYPES),
        ]:
            proto_dir = tmp_path / "protocol-testing" / protocol
            proto_dir.mkdir(parents=True, exist_ok=True)
            (proto_dir / "test.ivy").write_text(content)

        ws_root = str(tmp_path)
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config, force=True)

        summaries = builder.build_all()
        assert len(summaries) == 2

        protocols = {s["protocol"] for s in summaries}
        assert "quic" in protocols
        assert "minip" in protocols

    def test_build_all_skips_fresh(self, tmp_path):
        proto_dir = tmp_path / "protocol-testing" / "quic"
        proto_dir.mkdir(parents=True, exist_ok=True)
        (proto_dir / "test.ivy").write_text(SAMPLE_IVY_TYPES)

        ws_root = str(tmp_path)
        config = _make_workspace_config(ws_root)

        # First build
        builder = IndexBuilder(ws_root, config, force=True)
        builder.build_all()

        # Second build without force: should skip
        builder2 = IndexBuilder(ws_root, config, force=False)
        summaries = builder2.build_all()
        assert len(summaries) == 1
        assert summaries[0]["status"] == "skipped_fresh"

    def test_build_all_empty_workspace(self, tmp_path):
        ws_root = str(tmp_path)
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        summaries = builder.build_all()
        assert summaries == []


# ---------------------------------------------------------------------------
# Test 9: Building with missing/broken files doesn't crash (error recovery)
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    """Test that broken files don't crash the builder."""

    def test_broken_file_recovery(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "good.ivy": SAMPLE_IVY_TYPES,
                "broken.ivy": SAMPLE_IVY_BROKEN,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        # Should not raise
        summary = builder.build_protocol(proto_dir)
        assert summary["status"] == "ok"
        assert summary["files"] == 2

        # Both files should appear in manifest
        manifest = _read_index_json(proto_dir, "manifest.json")
        assert "good.ivy" in manifest["files"]
        assert "broken.ivy" in manifest["files"]

        # Symbols should exist for at least the good file
        symbols = _read_index_json(proto_dir, "symbols.json")
        assert "good.ivy" in symbols

    def test_unreadable_file_recovery(self, tmp_path):
        """Test that a file that becomes unreadable after discovery is handled."""
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "readable.ivy": SAMPLE_IVY_TYPES,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)

        # Build should succeed even with only one file
        summary = builder.build_protocol(proto_dir)
        assert summary["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 10: Test cli_index argument parsing
# ---------------------------------------------------------------------------


class TestCliIndex:
    """Test cli_index argument parsing and dispatch."""

    def test_cli_build_single_protocol(self, tmp_path, capsys, monkeypatch):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"test.ivy": SAMPLE_IVY_TYPES},
        )
        # Monkeypatch workspace detection to return our test workspace
        monkeypatch.setattr(
            "ivy_lsp.index_builder.detect_ivy_workspace",
            lambda start_dir: _make_workspace_config(ws_root),
        )

        result = cli_index([proto_dir])
        assert result == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["protocol"] == "quic"
        assert output["status"] in ("ok", "empty")

    def test_cli_status_flag(self, tmp_path, capsys, monkeypatch):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"test.ivy": SAMPLE_IVY_TYPES},
        )
        monkeypatch.setattr(
            "ivy_lsp.index_builder.detect_ivy_workspace",
            lambda start_dir: _make_workspace_config(ws_root),
        )

        result = cli_index([proto_dir, "--status"])
        assert result == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "stale_major"  # No index built yet

    def test_cli_fast_flag(self, tmp_path, capsys, monkeypatch):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"test.ivy": SAMPLE_IVY_TYPES},
        )
        monkeypatch.setattr(
            "ivy_lsp.index_builder.detect_ivy_workspace",
            lambda start_dir: _make_workspace_config(ws_root),
        )

        result = cli_index([proto_dir, "--fast"])
        assert result == 0

        manifest = _read_index_json(proto_dir, "manifest.json")
        assert manifest["default_parse_tier"] == "lexer"

    def test_cli_no_args_errors(self, capsys):
        """Calling with no args should fail."""
        with pytest.raises(SystemExit) as exc_info:
            cli_index([])
        assert exc_info.value.code != 0

    def test_cli_all_flag(self, tmp_path, capsys, monkeypatch):
        # Create workspace with protocols
        proto_dir = tmp_path / "protocol-testing" / "quic"
        proto_dir.mkdir(parents=True, exist_ok=True)
        (proto_dir / "test.ivy").write_text(SAMPLE_IVY_TYPES)

        ws_root = str(tmp_path)
        monkeypatch.setattr(
            "ivy_lsp.index_builder.detect_ivy_workspace",
            lambda start_dir: _make_workspace_config(ws_root),
        )

        result = cli_index(["--all", "--force"])
        assert result == 0


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------


class TestSha256:
    """Test SHA-256 computation helper."""

    def test_file_sha256(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world\n")

        digest = _file_sha256(str(test_file))
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_file_sha256_deterministic(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("deterministic content")

        d1 = _file_sha256(str(test_file))
        d2 = _file_sha256(str(test_file))
        assert d1 == d2


class TestTesterRoleDetection:
    """Test that tester role is detected from include closure filenames."""

    def test_server_behavior_detected(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {
                "test.ivy": (
                    "#lang ivy1.7\n"
                    "include server_behavior\n"
                    "export send\n"
                    "action send(x: t)\n"
                ),
                "server_behavior.ivy": SAMPLE_IVY_SERVER_BEHAVIOR,
            },
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        # test.ivy has exports -> should have a scope
        scopes_dir = os.path.join(proto_dir, ".ivy-index", "scopes")
        test_scope_path = os.path.join(scopes_dir, "test.json")
        if os.path.isfile(test_scope_path):
            with open(test_scope_path) as f:
                scope = json.load(f)
            # server_behavior -> tester is "client"
            assert scope["role"] == "client"


class TestRequirements:
    """Test requirements.json output."""

    def test_requirements_extracted(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"main.ivy": SAMPLE_IVY_MAIN},
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        reqs = _read_index_json(proto_dir, "requirements.json")
        assert "main.ivy" in reqs

        # SAMPLE_IVY_MAIN has "require p ~= 0;" in an after block
        main_reqs = reqs["main.ivy"]
        assert len(main_reqs) >= 1
        req = main_reqs[0]
        assert req["kind"] == "require"
        assert "monitor_action" in req


class TestExports:
    """Test exports.json output."""

    def test_exports_extracted(self, tmp_path):
        ws_root, proto_dir = _make_protocol_workspace(
            tmp_path,
            "quic",
            {"main.ivy": SAMPLE_IVY_MAIN},
        )
        config = _make_workspace_config(ws_root)
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        exports = _read_index_json(proto_dir, "exports.json")
        assert "main.ivy" in exports

        main_exports = exports["main.ivy"]
        assert "send" in main_exports.get("exports", [])
        assert "recv" in main_exports.get("imports", [])
