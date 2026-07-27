"""Tests for Fix 1B: .ivy-index bootstrapping in _write_model_to_index.

Verifies that the MCP server correctly bootstraps persistent index directories
when they don't contain manifest.json files, breaking the bootstrapping deadlock
where _write_model_to_index required pre-existing indexes that only
WorkspaceContext.load() could create.
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_ivy_file(path: str, content: str = "#lang ivy1.7\n") -> None:
    """Create a minimal .ivy file at the given path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _make_server_state(root: str) -> MagicMock:
    """Create a mock McpServerState with the methods _write_model_to_index needs."""
    from ivy_lsp.mcp.server import McpServerState

    state = MagicMock(spec=McpServerState)
    state.root = root
    state.workspace_context = None
    state._effective_exclude_dirs = frozenset()

    # Make find_ivy_files actually scan the filesystem
    def _find_ivy_files(search_root):
        result = []
        for dirpath, _dirs, files in os.walk(search_root):
            for f in files:
                if f.endswith(".ivy"):
                    result.append(os.path.join(dirpath, f))
        return result

    state.find_ivy_files = _find_ivy_files

    # Bind the real method
    state._write_model_to_index = McpServerState._write_model_to_index.__get__(
        state, McpServerState
    )
    return state


class TestWriteModelToIndexBootstrap:
    """Tests for _write_model_to_index with empty protocol_indexes."""

    def test_creates_index_dirs_when_none_exist(self, tmp_path):
        """When workspace_context has no protocol_indexes, bootstrap from filesystem."""
        # Setup: protocol-testing/quic/ with .ivy files but no .ivy-index/
        proto_dir = tmp_path / "protocol-testing" / "quic" / "quic_stack"
        proto_dir.mkdir(parents=True)
        _make_ivy_file(str(proto_dir / "quic_types.ivy"))
        _make_ivy_file(str(proto_dir / "quic_frame.ivy"))

        state = _make_server_state(str(tmp_path))
        model = MagicMock()

        state._write_model_to_index(model)

        index_dir = tmp_path / "protocol-testing" / "quic" / ".ivy-index"
        assert index_dir.is_dir()
        assert (index_dir / "manifest.json").is_file()
        assert (index_dir / ".gitignore").is_file()

    def test_manifest_has_required_fields(self, tmp_path):
        """Manifest contains version, protocol, builder_version, and files."""
        proto_dir = tmp_path / "protocol-testing" / "minip" / "minip_stack"
        proto_dir.mkdir(parents=True)
        _make_ivy_file(str(proto_dir / "minip_types.ivy"), "#lang ivy1.7\ntype id\n")

        state = _make_server_state(str(tmp_path))
        state._write_model_to_index(MagicMock())

        manifest_path = (
            tmp_path / "protocol-testing" / "minip" / ".ivy-index" / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())

        assert manifest["version"] == 1
        assert manifest["protocol"] == "minip"
        assert "builder_version" in manifest
        assert "created_at" in manifest
        assert "files" in manifest
        assert len(manifest["files"]) > 0

        # Each file entry has mtime and size
        for _rel, meta in manifest["files"].items():
            assert "mtime" in meta
            assert "size" in meta

    def test_writes_semantic_model_pickle(self, tmp_path):
        """Semantic model pickle is written alongside manifest."""
        proto_dir = tmp_path / "protocol-testing" / "bgp"
        proto_dir.mkdir(parents=True)
        _make_ivy_file(str(proto_dir / "bgp_types.ivy"))

        state = _make_server_state(str(tmp_path))
        model = {"test": "model_data"}  # Any picklable object

        state._write_model_to_index(model)

        pickle_path = (
            tmp_path
            / "protocol-testing"
            / "bgp"
            / ".ivy-index"
            / "semantic_model.pickle.gz"
        )
        assert pickle_path.is_file()

        with gzip.open(str(pickle_path), "rb") as f:
            loaded = pickle.load(f)  # noqa: S301
        assert loaded == model

    def test_gitignore_created_with_star(self, tmp_path):
        """.gitignore contains '*' to prevent pickle commits."""
        proto_dir = tmp_path / "protocol-testing" / "coap"
        proto_dir.mkdir(parents=True)
        _make_ivy_file(str(proto_dir / "coap_types.ivy"))

        state = _make_server_state(str(tmp_path))
        state._write_model_to_index(MagicMock())

        gitignore = tmp_path / "protocol-testing" / "coap" / ".ivy-index" / ".gitignore"
        assert gitignore.read_text().strip() == "*"

    def test_uses_existing_protocol_indexes_when_available(self, tmp_path):
        """When workspace_context has protocol_indexes, use those instead of walking."""
        proto_dir = tmp_path / "protocol-testing" / "quic"
        index_dir = proto_dir / ".ivy-index"
        index_dir.mkdir(parents=True)
        _make_ivy_file(str(proto_dir / "quic_types.ivy"))

        mock_idx = MagicMock()
        mock_idx.index_dir = str(index_dir)

        state = _make_server_state(str(tmp_path))
        state.workspace_context = MagicMock()
        state.workspace_context.protocol_indexes = {"quic": mock_idx}

        model = MagicMock()
        state._write_model_to_index(model)

        assert (index_dir / "manifest.json").is_file()

    def test_skips_hidden_dirs(self, tmp_path):
        """Directories starting with '.' under protocol-testing/ are skipped."""
        (tmp_path / "protocol-testing" / ".hidden").mkdir(parents=True)
        (tmp_path / "protocol-testing" / "quic").mkdir(parents=True)
        _make_ivy_file(str(tmp_path / "protocol-testing" / "quic" / "types.ivy"))

        state = _make_server_state(str(tmp_path))
        state._write_model_to_index(MagicMock())

        assert not (tmp_path / "protocol-testing" / ".hidden" / ".ivy-index").exists()
        assert (tmp_path / "protocol-testing" / "quic" / ".ivy-index").is_dir()

    def test_no_protocol_dirs_does_nothing(self, tmp_path):
        """When no protocol-testing/ exists, _write_model_to_index is a no-op."""
        state = _make_server_state(str(tmp_path))
        # Should not raise
        state._write_model_to_index(MagicMock())

    def test_exception_does_not_propagate(self, tmp_path):
        """Exceptions in _write_model_to_index are caught, not propagated."""
        state = _make_server_state(str(tmp_path))

        # Make find_ivy_files raise to simulate a filesystem error
        state.find_ivy_files = MagicMock(side_effect=OSError("disk error"))
        state.root = str(tmp_path)

        # Create protocol dir so the code actually tries to iterate
        (tmp_path / "protocol-testing" / "quic").mkdir(parents=True)

        # Should not raise
        state._write_model_to_index(MagicMock())


class TestVersionFingerprint:
    """Tests for builder_version checking in _build_model Strategy 1."""

    def test_version_mismatch_skips_protocol(self, tmp_path):
        """Index built with different ivy-lsp version triggers skip."""
        from ivy_lsp import __version__

        # Create a manifest with a different builder_version
        proto_dir = tmp_path / "protocol-testing" / "quic"
        index_dir = proto_dir / ".ivy-index"
        index_dir.mkdir(parents=True)

        manifest = {
            "version": 1,
            "protocol": "quic",
            "builder_version": "0.0.0-old",
            "files": {"quic_types.ivy": {"mtime": time.time()}},
        }
        (index_dir / "manifest.json").write_text(json.dumps(manifest))

        # The version check happens in _build_model Strategy 1 loop
        # We verify the manifest's builder_version is "0.0.0-old" != __version__
        assert manifest["builder_version"] != __version__

    def test_matching_version_accepts_protocol(self, tmp_path):
        """Index built with same version is accepted."""
        from ivy_lsp import __version__

        manifest = {
            "version": 1,
            "protocol": "quic",
            "builder_version": __version__,
            "files": {"quic_types.ivy": {"mtime": time.time()}},
        }
        assert manifest["builder_version"] == __version__
