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

    def test_extract_one_file_is_picklable(self, quic_workspace):
        """_extract_one_file must be a top-level function that pickle can serialize."""
        import os

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
