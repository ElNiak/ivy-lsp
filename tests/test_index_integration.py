"""Integration tests: IndexBuilder -> WorkspaceContext roundtrip.

These tests exercise the full pipeline:
  1. Build an offline index with IndexBuilder.build_protocol()
  2. Load it back via WorkspaceContext.load()
  3. Verify that the loaded artifacts (symbols, include graphs, scopes,
     staleness) match expectations.

Unlike the unit-level test_index_builder.py and test_workspace_context.py
which test each layer in isolation, these tests confirm that the serialized
output of IndexBuilder is correctly consumed by WorkspaceContext.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from ivy_lsp.core.workspace.context import WorkspaceContext
from ivy_lsp.core.workspace.detection import WorkspaceConfig
from ivy_lsp.index_builder import IndexBuilder

# ---------------------------------------------------------------------------
# Fixture: force Tier 3 (regex) when ivy module unavailable
# ---------------------------------------------------------------------------


def _check_ivy_lexer_available():
    """Check if the PLY lexer actually works (requires ivy module at runtime)."""
    try:
        from ivy_lsp.core.parsing.token_stream import tokenize_ivy

        tokenize_ivy("type t\n", "<test>")
        return True
    except (ImportError, ModuleNotFoundError, Exception):
        return False


_IVY_LEXER_AVAILABLE = _check_ivy_lexer_available()


@pytest.fixture(autouse=True)
def _force_regex_tier_if_no_lexer(monkeypatch):
    """When the ivy module is unavailable, force Tier 3 (regex) extraction.

    Without this, Tier 2 (PLY lexer) reports success but returns empty
    results, causing the builder to produce indexes with no symbols.
    """
    if _IVY_LEXER_AVAILABLE:
        yield
        return

    from ivy_lsp.core.parsing import tiered_extractor as _te_mod

    original_init = _te_mod.TieredExtractor.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._parser_available = False
        self._lexer_available = False

    _te_mod.TieredExtractor.__init__ = patched_init

    import ivy_lsp.core.analysis.light_mode_extractor as _lme_mod

    original_lexer_flag = _lme_mod._LEXER_AVAILABLE
    _lme_mod._LEXER_AVAILABLE = False
    yield
    _te_mod.TieredExtractor.__init__ = original_init
    _lme_mod._LEXER_AVAILABLE = original_lexer_flag


# ---------------------------------------------------------------------------
# Sample .ivy content
# ---------------------------------------------------------------------------

IVY_MAIN = """\
#lang ivy1.7

include types

type connection_id
relation connected(C:connection_id)

export action send_packet
action send_packet = {
    require connected(the_cid);
}
"""

IVY_TYPES = """\
#lang ivy1.7

type pkt_type = {initial, handshake, data}
type stream_id
"""

IVY_MINIP_MAIN = """\
#lang ivy1.7

include minip_types

type msg_id

export action send_msg
action send_msg = {
    require msg_id ~= 0;
}
"""

IVY_MINIP_TYPES = """\
#lang ivy1.7

type msg_kind = {request, response}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path, protocols):
    """Create a temp workspace with protocol dirs and a v3 marker.

    Args:
        tmp_path: pytest tmp_path fixture.
        protocols: dict of {protocol_name: {filename: content}}.

    Returns:
        workspace_root as a string.
    """
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    for proto_name, files in protocols.items():
        proto_dir = ws / "protocol-testing" / proto_name
        proto_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            filepath = proto_dir / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)

    # Write a v3 .ivyworkspace marker so detect_ivy_workspace finds it
    marker = {
        "version": 3,
        "workspace_layers": [{"id": "default", "include_paths": ["protocol-testing"]}],
    }
    (ws / ".ivyworkspace").write_text(json.dumps(marker))

    return str(ws)


def _make_workspace_config(ws_root):
    """Create a minimal WorkspaceConfig for the IndexBuilder."""
    return WorkspaceConfig(
        workspace_root=ws_root,
        detected_by="test",
    )


# ---------------------------------------------------------------------------
# Workspace fixture (clears env vars that would confuse detection)
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure no IVY_LSP_* env vars leak into workspace detection."""
    monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
    monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
    monkeypatch.delenv("IVY_WORKSPACE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# Test 1: Full roundtrip -- build then load
# ---------------------------------------------------------------------------


class TestFullRoundtrip:
    """Build index with IndexBuilder, load via WorkspaceContext, verify."""

    def test_roundtrip_has_index(self, tmp_path, clean_env):
        """After building, WorkspaceContext.load finds the index."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        summary = builder.build_protocol(proto_dir)
        assert summary["status"] == "ok"
        assert summary["files"] == 2

        ctx = WorkspaceContext.load(ws_root)
        assert ctx.has_index() is True
        assert "quic" in ctx.list_protocols()

    def test_roundtrip_symbol_count(self, tmp_path, clean_env):
        """Loaded index has symbols for both files."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        # Both files should have symbol entries
        assert "main.ivy" in idx.symbols
        assert "types.ivy" in idx.symbols

        # Total symbol count should be > 0
        total_symbols = sum(len(syms) for syms in idx.symbols.values())
        assert total_symbols > 0, "Expected at least one symbol in the index"

    def test_roundtrip_include_edges(self, tmp_path, clean_env):
        """Loaded IncludeGraph has the main.ivy -> types.ivy edge."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        includes = idx.includes.get_includes("main.ivy")
        assert (
            "types.ivy" in includes
        ), f"Expected main.ivy -> types.ivy edge, got: {includes}"

    def test_roundtrip_test_scope_exists(self, tmp_path, clean_env):
        """At least one test scope is created for the file with exports."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        # main.ivy has "export action send_packet" -> should produce a scope
        assert (
            len(idx.scopes) >= 1
        ), f"Expected at least 1 test scope, got {len(idx.scopes)}"

        # The scope should be keyed by "main" (basename without .ivy)
        assert (
            "main" in idx.scopes
        ), f"Expected scope key 'main', got keys: {list(idx.scopes.keys())}"

    def test_roundtrip_scope_include_closure(self, tmp_path, clean_env):
        """The test scope's include_closure contains the included file."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        ctx = WorkspaceContext.load(ws_root)
        scope = ctx.get_test_scope("main")
        assert scope is not None

        # The include closure for main.ivy should contain types.ivy
        assert (
            "types.ivy" in scope.include_closure
        ), f"Expected types.ivy in closure, got: {scope.include_closure}"
        # And also the test file itself
        assert "main.ivy" in scope.include_closure

    def test_roundtrip_staleness_fresh(self, tmp_path, clean_env):
        """Immediately after building, the index should be fresh."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        assert idx.staleness.status == "fresh"
        assert idx.staleness.changed_files == 0
        assert idx.staleness.total_files == 2


# ---------------------------------------------------------------------------
# Test 2: Staleness detection after file touch
# ---------------------------------------------------------------------------


class TestStalenessDetection:
    """Build index, touch a file, verify staleness is detected on load."""

    def test_touch_one_file_stale_minor(self, tmp_path, clean_env):
        """Touching one file out of many produces stale_minor."""
        # Need enough files so 1 changed / total < 10%
        # Use 11+ files so that 1/N < 0.10
        files = {"types.ivy": IVY_TYPES}
        for i in range(11):
            files[f"extra_{i}.ivy"] = f"#lang ivy1.7\n\ntype extra_type_{i}\n"

        ws_root = _make_workspace(tmp_path, {"quic": files})
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        # Touch one file with a significantly different mtime
        target = os.path.join(proto_dir, "types.ivy")
        future_time = time.time() + 200
        os.utime(target, (future_time, future_time))

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        assert idx.staleness.status == "stale_minor", (
            f"Expected stale_minor, got {idx.staleness.status} "
            f"(changed={idx.staleness.changed_files}, total={idx.staleness.total_files})"
        )
        assert idx.staleness.changed_files == 1

    def test_touch_many_files_stale_major(self, tmp_path, clean_env):
        """Touching many files produces stale_major."""
        files = {}
        for i in range(5):
            files[f"file_{i}.ivy"] = f"#lang ivy1.7\n\ntype t_{i}\n"

        ws_root = _make_workspace(tmp_path, {"quic": files})
        config = _make_workspace_config(ws_root)
        proto_dir = os.path.join(ws_root, "protocol-testing", "quic")

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        # Touch all 5 files
        future_time = time.time() + 200
        for i in range(5):
            target = os.path.join(proto_dir, f"file_{i}.ivy")
            os.utime(target, (future_time, future_time))

        ctx = WorkspaceContext.load(ws_root)
        idx = ctx.protocol_indexes["quic"]

        assert idx.staleness.status == "stale_major"
        assert idx.staleness.changed_files == 5


# ---------------------------------------------------------------------------
# Test 3: Two independent protocols
# ---------------------------------------------------------------------------


class TestTwoProtocols:
    """Build indexes for two protocols, verify independence."""

    def test_both_protocols_listed(self, tmp_path, clean_env):
        """Both protocols appear in list_protocols()."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
                "minip": {
                    "main.ivy": IVY_MINIP_MAIN,
                    "minip_types.ivy": IVY_MINIP_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "quic"))
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "minip"))

        ctx = WorkspaceContext.load(ws_root)
        protocols = ctx.list_protocols()
        assert "quic" in protocols
        assert "minip" in protocols
        assert len(protocols) == 2

    def test_scopes_are_independent(self, tmp_path, clean_env):
        """Scopes from each protocol are separate."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
                "minip": {
                    "main.ivy": IVY_MINIP_MAIN,
                    "minip_types.ivy": IVY_MINIP_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "quic"))
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "minip"))

        ctx = WorkspaceContext.load(ws_root)

        quic_idx = ctx.protocol_indexes["quic"]
        minip_idx = ctx.protocol_indexes["minip"]

        # Both have the scope key "main" (both have main.ivy with exports)
        # but the scopes should belong to their respective protocol indexes
        assert "main" in quic_idx.scopes
        assert "main" in minip_idx.scopes

        # The include closures should be different
        quic_closure = quic_idx.scopes["main"].include_closure
        minip_closure = minip_idx.scopes["main"].include_closure

        assert "types.ivy" in quic_closure
        assert "types.ivy" not in minip_closure
        assert "minip_types.ivy" in minip_closure
        assert "minip_types.ivy" not in quic_closure

    def test_symbols_are_independent(self, tmp_path, clean_env):
        """Symbols from each protocol are separate."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
                "minip": {
                    "main.ivy": IVY_MINIP_MAIN,
                    "minip_types.ivy": IVY_MINIP_TYPES,
                },
            },
        )
        config = _make_workspace_config(ws_root)

        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "quic"))
        builder.build_protocol(os.path.join(ws_root, "protocol-testing", "minip"))

        ctx = WorkspaceContext.load(ws_root)

        quic_idx = ctx.protocol_indexes["quic"]
        minip_idx = ctx.protocol_indexes["minip"]

        # quic should have types.ivy, not minip_types.ivy
        assert "types.ivy" in quic_idx.symbols
        assert "minip_types.ivy" not in quic_idx.symbols

        # minip should have minip_types.ivy, not types.ivy
        assert "minip_types.ivy" in minip_idx.symbols
        assert "types.ivy" not in minip_idx.symbols


# ---------------------------------------------------------------------------
# Test 4: No .ivy-index directory
# ---------------------------------------------------------------------------


class TestNoIndex:
    """Workspace without .ivy-index/ should report empty."""

    def test_has_index_false(self, tmp_path, clean_env):
        """has_index() returns False when no .ivy-index exists."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                    "types.ivy": IVY_TYPES,
                },
            },
        )
        # Do NOT build the index -- just load
        ctx = WorkspaceContext.load(ws_root)

        assert ctx.has_index() is False

    def test_protocol_indexes_empty(self, tmp_path, clean_env):
        """protocol_indexes is empty when no .ivy-index exists."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                },
            },
        )
        ctx = WorkspaceContext.load(ws_root)

        assert ctx.protocol_indexes == {}

    def test_list_protocols_empty(self, tmp_path, clean_env):
        """list_protocols() returns empty list when no .ivy-index exists."""
        ws_root = _make_workspace(
            tmp_path,
            {
                "quic": {
                    "main.ivy": IVY_MAIN,
                },
            },
        )
        ctx = WorkspaceContext.load(ws_root)

        assert ctx.list_protocols() == []

    def test_empty_workspace_no_protocols(self, tmp_path, clean_env):
        """Completely empty workspace also yields no index."""
        ws_root = _make_workspace(tmp_path, {})
        ctx = WorkspaceContext.load(ws_root)

        assert ctx.has_index() is False
        assert ctx.protocol_indexes == {}
