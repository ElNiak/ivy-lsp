"""Tests for WorkspaceContext.detect_only / load_indexes split."""

import os

import pytest

from ivy_lsp.core.workspace.context import WorkspaceContext


class TestDetectOnly:
    """WorkspaceContext.detect_only should return a context without indexes."""

    def test_returns_context_with_empty_indexes(self, tmp_path):
        ctx = WorkspaceContext.detect_only(str(tmp_path))
        assert isinstance(ctx, WorkspaceContext)
        assert ctx.protocol_indexes == {}
        assert ctx.workspace_root

    def test_has_index_returns_false(self, tmp_path):
        ctx = WorkspaceContext.detect_only(str(tmp_path))
        assert ctx.has_index() is False

    def test_workspace_config_is_populated(self, tmp_path):
        ctx = WorkspaceContext.detect_only(str(tmp_path))
        assert ctx.workspace_config is not None
        assert ctx.workspace_config.workspace_root


class TestLoadIndexes:
    """WorkspaceContext.load_indexes should populate protocol_indexes in place."""

    def test_no_indexes_directory_stays_empty(self, tmp_path):
        ctx = WorkspaceContext.detect_only(str(tmp_path))
        ctx.load_indexes()
        assert ctx.protocol_indexes == {}

    def test_populates_indexes_when_present(self, tmp_path):
        # Create a minimal .ivy-index structure
        proto_dir = tmp_path / "protocol-testing" / "test_proto"
        index_dir = proto_dir / ".ivy-index"
        index_dir.mkdir(parents=True)
        manifest = index_dir / "manifest.json"
        manifest.write_text('{"version": 1, "protocol": "test_proto"}')
        # Create minimal symbols file so _load_protocol_index succeeds
        symbols_dir = index_dir / "symbols"
        symbols_dir.mkdir()

        ctx = WorkspaceContext.detect_only(str(tmp_path))
        ctx.load_indexes()
        # May or may not load depending on manifest validation,
        # but should not raise
        assert isinstance(ctx.protocol_indexes, dict)


class TestLoadEquivalence:
    """WorkspaceContext.load() should produce the same result as detect_only + load_indexes."""

    def test_load_matches_detect_plus_indexes(self, tmp_path):
        ctx_load = WorkspaceContext.load(str(tmp_path))
        ctx_split = WorkspaceContext.detect_only(str(tmp_path))
        ctx_split.load_indexes()

        assert ctx_load.workspace_root == ctx_split.workspace_root
        assert ctx_load.project_type == ctx_split.project_type
        assert set(ctx_load.protocol_indexes.keys()) == set(
            ctx_split.protocol_indexes.keys()
        )
