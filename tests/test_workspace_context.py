"""Tests for ivy_lsp.core.workspace.context module."""

from __future__ import annotations

import gzip
import json
import os
import pickle
import time

import pytest

from ivy_lsp.core.workspace.context import (
    FileChange,
    ProtocolIndex,
    StalenessInfo,
    WorkspaceContext,
    _load_json,
    _load_pickle,
    _load_scopes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(tmp_path, protocol, files_meta=None):
    """Create a minimal .ivy-index directory with manifest.json.

    Args:
        tmp_path: Base workspace directory.
        protocol: Protocol name (e.g. "quic").
        files_meta: Optional dict of {rel_path: {"mtime": float}} for staleness.

    Returns:
        Path to the .ivy-index directory.
    """
    proto_dir = tmp_path / "protocol-testing" / protocol
    index_dir = proto_dir / ".ivy-index"
    index_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "protocol": protocol,
        "version": 1,
        "created": time.time(),
    }
    if files_meta is not None:
        manifest["files"] = files_meta

    (index_dir / "manifest.json").write_text(json.dumps(manifest))
    return index_dir


def _make_ivy_file(tmp_path, protocol, filename, content="#lang ivy1.7\ntype t\n"):
    """Create an .ivy file under protocol-testing/<protocol>/."""
    proto_dir = tmp_path / "protocol-testing" / protocol
    proto_dir.mkdir(parents=True, exist_ok=True)
    ivy_file = proto_dir / filename
    ivy_file.write_text(content)
    return ivy_file


def _make_symbols_json(index_dir, symbols_data):
    """Write a symbols.json file."""
    (index_dir / "symbols.json").write_text(json.dumps(symbols_data))


def _make_includes_json(index_dir, edges):
    """Write an includes.json file."""
    (index_dir / "includes.json").write_text(json.dumps(edges))


def _make_exports_json(index_dir, exports_data):
    """Write an exports.json file."""
    (index_dir / "exports.json").write_text(json.dumps(exports_data))


def _make_scope_file(index_dir, test_name, scope_data):
    """Write a scopes/<test>.json file."""
    scopes_dir = index_dir / "scopes"
    scopes_dir.mkdir(exist_ok=True)
    (scopes_dir / f"{test_name}.json").write_text(json.dumps(scope_data))


def _make_scopes_meta(index_dir, entries):
    """Write a scopes/_meta.json file."""
    scopes_dir = index_dir / "scopes"
    scopes_dir.mkdir(exist_ok=True)
    (scopes_dir / "_meta.json").write_text(json.dumps(entries))


def _make_pickle_gz(path, obj):
    """Write a gzipped pickle file."""
    with gzip.open(str(path), "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def _scope_dict(
    test_name, entry_file, includes=None, exports=None, imports=None, role="unknown"
):
    """Create a scope dict matching TestScope.from_dict() format."""
    return {
        "test": test_name,
        "entry_file": entry_file,
        "role": role,
        "transitive_includes": includes or [],
        "exported_actions": exports or [],
        "imported_actions": imports or [],
        "file_count": len(includes or []),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Create a minimal workspace with a .ivyworkspace marker (v3).

    This avoids detect_ivy_workspace falling through to PANTHER heuristic
    or the fallback detector.
    """
    marker = {
        "version": 3,
        "workspace_layers": [{"id": "default", "include_paths": ["protocol-testing"]}],
    }
    (tmp_path / ".ivyworkspace").write_text(json.dumps(marker))
    # Ensure no env vars leak into detection
    monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
    monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Test: Load with valid .ivy-index
# ---------------------------------------------------------------------------


class TestLoadWithValidIndex:
    """Test 1: Load with valid .ivy-index — verify protocol_indexes populated."""

    def test_load_single_protocol(self, ws):
        """A workspace with one protocol index loads correctly."""
        index_dir = _make_manifest(ws, "quic")
        _make_symbols_json(
            index_dir,
            {"quic_types.ivy": [{"name": "cid", "kind": 5, "range": [0, 0, 0, 3]}]},
        )
        _make_includes_json(index_dir, {"conn.ivy": ["quic_types.ivy"]})

        ctx = WorkspaceContext.load(str(ws))

        assert ctx.has_index()
        assert "quic" in ctx.protocol_indexes
        idx = ctx.protocol_indexes["quic"]
        assert idx.protocol == "quic"
        assert "quic_types.ivy" in idx.symbols
        assert len(idx.symbols["quic_types.ivy"]) == 1
        assert idx.includes.get_includes("conn.ivy") == {"quic_types.ivy"}

    def test_load_with_exports(self, ws):
        """Exports JSON loads as ExportImportInfo objects."""
        index_dir = _make_manifest(ws, "quic")
        _make_exports_json(
            index_dir,
            {
                "test_client.ivy": {
                    "file": "test_client.ivy",
                    "exports": ["send", "recv"],
                    "imports": ["open_connection"],
                    "export_lines": {"send": 10, "recv": 20},
                    "import_lines": {"open_connection": 5},
                }
            },
        )

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert "test_client.ivy" in idx.exports
        einfo = idx.exports["test_client.ivy"]
        assert einfo.exports == ["send", "recv"]
        assert einfo.imports == ["open_connection"]

    def test_load_with_scopes_individual_files(self, ws):
        """Scopes load from individual <test>.json files."""
        index_dir = _make_manifest(ws, "quic")
        _make_scope_file(
            index_dir,
            "quic_server_test_stream",
            _scope_dict(
                "quic_server_test_stream",
                "quic_tests/server_tests/quic_server_test_stream.ivy",
                includes=["quic_types.ivy", "quic_frame.ivy"],
                exports=["send_frame"],
                role="client",
            ),
        )

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert "quic_server_test_stream" in idx.scopes
        scope = idx.scopes["quic_server_test_stream"]
        assert scope.tester_role == "client"
        assert "quic_types.ivy" in scope.include_closure

    def test_load_with_scopes_meta(self, ws):
        """Scopes load from _meta.json bulk format."""
        index_dir = _make_manifest(ws, "quic")
        _make_scopes_meta(
            index_dir,
            [
                _scope_dict("test_a", "tests/test_a.ivy", role="server"),
                _scope_dict("test_b", "tests/test_b.ivy", role="client"),
            ],
        )

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert "test_a" in idx.scopes
        assert "test_b" in idx.scopes
        assert idx.scopes["test_a"].tester_role == "server"


# ---------------------------------------------------------------------------
# Test: Load without .ivy-index
# ---------------------------------------------------------------------------


class TestLoadWithoutIndex:
    """Test 2: Load without .ivy-index — verify empty protocol_indexes."""

    def test_no_index_dirs(self, ws):
        """Workspace with no .ivy-index/ directories returns empty."""
        # Create protocol dir without .ivy-index
        (ws / "protocol-testing" / "quic").mkdir(parents=True)

        ctx = WorkspaceContext.load(str(ws))
        assert not ctx.has_index()
        assert ctx.protocol_indexes == {}
        assert ctx.list_protocols() == []

    def test_empty_workspace(self, ws):
        """Workspace with nothing returns empty indexes."""
        ctx = WorkspaceContext.load(str(ws))
        assert not ctx.has_index()
        assert ctx.list_tests() == []


# ---------------------------------------------------------------------------
# Test: Staleness detection
# ---------------------------------------------------------------------------


class TestStalenessDetection:
    """Test 3: Staleness detection — fresh, stale_minor, stale_major."""

    def test_fresh_index(self, ws):
        """All mtimes match -> fresh."""
        ivy_file = _make_ivy_file(ws, "quic", "types.ivy")
        actual_mtime = os.path.getmtime(str(ivy_file))

        _make_manifest(
            ws,
            "quic",
            files_meta={
                "types.ivy": {"mtime": actual_mtime},
            },
        )

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.staleness.status == "fresh"
        assert idx.staleness.changed_files == 0
        assert idx.staleness.total_files == 1

    def test_stale_minor(self, ws):
        """<10% files changed -> stale_minor."""
        # Create 20 files, change 1 (5% changed)
        files_meta = {}
        for i in range(20):
            f = _make_ivy_file(ws, "quic", f"file_{i}.ivy")
            actual_mtime = os.path.getmtime(str(f))
            if i == 0:
                # Mismatched mtime for file_0
                files_meta[f"file_{i}.ivy"] = {"mtime": actual_mtime - 100}
            else:
                files_meta[f"file_{i}.ivy"] = {"mtime": actual_mtime}

        _make_manifest(ws, "quic", files_meta=files_meta)

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.staleness.status == "stale_minor"
        assert idx.staleness.changed_files == 1
        assert idx.staleness.total_files == 20

    def test_stale_major(self, ws):
        """>10% files changed -> stale_major."""
        # Create 5 files, change 2 (40% changed)
        files_meta = {}
        for i in range(5):
            f = _make_ivy_file(ws, "quic", f"file_{i}.ivy")
            actual_mtime = os.path.getmtime(str(f))
            if i < 2:
                files_meta[f"file_{i}.ivy"] = {"mtime": actual_mtime - 100}
            else:
                files_meta[f"file_{i}.ivy"] = {"mtime": actual_mtime}

        _make_manifest(ws, "quic", files_meta=files_meta)

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.staleness.status == "stale_major"
        assert idx.staleness.changed_files == 2

    def test_stale_major_no_files_key(self, ws):
        """Missing files key in manifest -> stale_major."""
        _make_manifest(ws, "quic")  # No files_meta

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.staleness.status == "stale_major"

    def test_stale_deleted_file(self, ws):
        """File in manifest but deleted from disk -> counted as changed."""
        _make_manifest(
            ws,
            "quic",
            files_meta={
                "deleted.ivy": {"mtime": 1000.0},
                "existing.ivy": {"mtime": 1000.0},
            },
        )
        # Create only existing.ivy with matching mtime
        f = _make_ivy_file(ws, "quic", "existing.ivy")
        os.utime(str(f), (1000.0, 1000.0))

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        # deleted.ivy is missing -> 1 changed out of 2 -> 50% -> stale_major
        assert idx.staleness.status == "stale_major"
        assert idx.staleness.changed_files == 1


# ---------------------------------------------------------------------------
# Test: Corrupt JSON recovery
# ---------------------------------------------------------------------------


class TestCorruptJsonRecovery:
    """Test 4: Corrupt JSON recovery — log warning, skip, don't crash."""

    def test_corrupt_manifest_skips_protocol(self, ws):
        """Corrupt manifest.json -> protocol skipped entirely."""
        proto_dir = ws / "protocol-testing" / "quic"
        index_dir = proto_dir / ".ivy-index"
        index_dir.mkdir(parents=True)
        (index_dir / "manifest.json").write_text("not valid json {{{")

        ctx = WorkspaceContext.load(str(ws))
        assert not ctx.has_index()
        assert "quic" not in ctx.protocol_indexes

    def test_corrupt_symbols_uses_empty(self, ws):
        """Corrupt symbols.json -> empty symbols dict, protocol still loads."""
        index_dir = _make_manifest(ws, "quic")
        (index_dir / "symbols.json").write_text("broken[[[")

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.has_index()
        idx = ctx.protocol_indexes["quic"]
        assert idx.symbols == {}

    def test_corrupt_includes_uses_empty_graph(self, ws):
        """Corrupt includes.json -> empty IncludeGraph, protocol still loads."""
        index_dir = _make_manifest(ws, "quic")
        (index_dir / "includes.json").write_text("{bad json")

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.includes.to_edges() == {}

    def test_corrupt_exports_skips_entries(self, ws):
        """Corrupt export entries are skipped, valid ones load."""
        index_dir = _make_manifest(ws, "quic")
        _make_exports_json(
            index_dir,
            {
                "good.ivy": {
                    "file": "good.ivy",
                    "exports": ["send"],
                    "imports": [],
                },
                "bad.ivy": "not a dict",  # will fail from_dict
            },
        )

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert "good.ivy" in idx.exports
        # "bad.ivy" is a string, not a dict -> from_dict raises -> skipped
        assert "bad.ivy" not in idx.exports

    def test_corrupt_scope_file_skipped(self, ws):
        """Corrupt scope JSON file is skipped, other scopes load."""
        index_dir = _make_manifest(ws, "quic")
        scopes_dir = index_dir / "scopes"
        scopes_dir.mkdir()
        (scopes_dir / "good_test.json").write_text(
            json.dumps(_scope_dict("good_test", "tests/good_test.ivy"))
        )
        (scopes_dir / "bad_test.json").write_text("{corrupt}")

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert "good_test" in idx.scopes
        assert "bad_test" not in idx.scopes


# ---------------------------------------------------------------------------
# Test: Corrupt pickle recovery
# ---------------------------------------------------------------------------


class TestCorruptPickleRecovery:
    """Test 5: Corrupt pickle -> semantic_model=None, JSON still loads."""

    def test_corrupt_pickle_yields_none(self, ws):
        """Corrupt pickle.gz -> field is None, protocol still loads."""
        index_dir = _make_manifest(ws, "quic")
        _make_symbols_json(index_dir, {"types.ivy": []})

        # Write garbage to the pickle files
        (index_dir / "semantic_model.pickle.gz").write_bytes(b"not a pickle")
        (index_dir / "requirement_graph.pickle.gz").write_bytes(b"also garbage")

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.semantic_model is None
        assert idx.requirement_graph is None
        # JSON artifacts still loaded
        assert "types.ivy" in idx.symbols

    def test_valid_pickle_loads(self, ws):
        """Valid pickle.gz loads correctly."""
        index_dir = _make_manifest(ws, "quic")
        model_data = {"nodes": ["a", "b"], "edges": []}
        _make_pickle_gz(index_dir / "semantic_model.pickle.gz", model_data)

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.semantic_model == model_data

    def test_missing_pickle_is_none(self, ws):
        """Missing pickle file -> field is None (not an error)."""
        _make_manifest(ws, "quic")

        ctx = WorkspaceContext.load(str(ws))
        idx = ctx.protocol_indexes["quic"]
        assert idx.semantic_model is None
        assert idx.requirement_graph is None


# ---------------------------------------------------------------------------
# Test: Protocol isolation
# ---------------------------------------------------------------------------


class TestProtocolIsolation:
    """Test 6: Two protocols with independent indexes."""

    def test_two_protocols_independent(self, ws):
        """Each protocol has its own symbols, scopes, etc."""
        # Protocol: quic
        quic_idx = _make_manifest(ws, "quic")
        _make_symbols_json(
            quic_idx,
            {"quic_types.ivy": [{"name": "cid", "kind": 5, "range": [0, 0, 0, 3]}]},
        )
        _make_scope_file(
            quic_idx,
            "quic_test_stream",
            _scope_dict(
                "quic_test_stream", "tests/quic_test_stream.ivy", role="client"
            ),
        )

        # Protocol: apt
        apt_idx = _make_manifest(ws, "apt")
        _make_symbols_json(
            apt_idx,
            {"apt_types.ivy": [{"name": "pkt", "kind": 5, "range": [0, 0, 0, 3]}]},
        )
        _make_scope_file(
            apt_idx,
            "apt_test_basic",
            _scope_dict("apt_test_basic", "tests/apt_test_basic.ivy", role="server"),
        )

        ctx = WorkspaceContext.load(str(ws))
        assert sorted(ctx.list_protocols()) == ["apt", "quic"]

        # Verify isolation
        quic = ctx.protocol_indexes["quic"]
        apt = ctx.protocol_indexes["apt"]
        assert "quic_types.ivy" in quic.symbols
        assert "quic_types.ivy" not in apt.symbols
        assert "apt_types.ivy" in apt.symbols
        assert "apt_types.ivy" not in quic.symbols

    def test_protocols_dont_share_scopes(self, ws):
        """Scopes in one protocol don't appear in another."""
        quic_idx = _make_manifest(ws, "quic")
        _make_scope_file(
            quic_idx, "quic_test", _scope_dict("quic_test", "tests/quic_test.ivy")
        )
        _make_manifest(ws, "apt")  # No scopes

        ctx = WorkspaceContext.load(str(ws))
        assert "quic_test" in ctx.protocol_indexes["quic"].scopes
        assert "quic_test" not in ctx.protocol_indexes["apt"].scopes


# ---------------------------------------------------------------------------
# Test: detect() classmethod
# ---------------------------------------------------------------------------


class TestDetect:
    """Test 7: detect() returns correct dict."""

    def test_detect_with_indexes(self, ws):
        """detect() returns expected dict shape with protocols."""
        ivy_file = _make_ivy_file(ws, "quic", "types.ivy")
        actual_mtime = os.path.getmtime(str(ivy_file))
        _make_manifest(
            ws,
            "quic",
            files_meta={
                "types.ivy": {"mtime": actual_mtime},
            },
        )
        _make_manifest(ws, "apt")

        result = WorkspaceContext.detect(str(ws))

        assert result["workspace_root"] == str(ws)
        assert result["project_type"] in ("panther", "standalone", "fallback", None)
        assert "detected_by" in result
        assert sorted(result["protocols"]) == ["apt", "quic"]
        assert result["has_index"] is True
        assert result["staleness"]["quic"] == "fresh"
        assert result["staleness"]["apt"] == "stale_major"  # No files key

    def test_detect_without_indexes(self, ws):
        """detect() returns empty protocols list when no indexes."""
        result = WorkspaceContext.detect(str(ws))

        assert result["has_index"] is False
        assert result["protocols"] == []
        assert result["staleness"] == {}


# ---------------------------------------------------------------------------
# Test: get_test_scope() across protocols
# ---------------------------------------------------------------------------


class TestGetTestScope:
    """Test 8: get_test_scope() finds scope across protocols."""

    def test_find_scope_in_first_protocol(self, ws):
        """Scope found in the first protocol searched."""
        idx = _make_manifest(ws, "quic")
        _make_scope_file(
            idx,
            "test_stream",
            _scope_dict(
                "test_stream",
                "tests/test_stream.ivy",
                exports=["send_frame"],
                role="client",
            ),
        )

        ctx = WorkspaceContext.load(str(ws))
        scope = ctx.get_test_scope("test_stream")
        assert scope is not None
        assert scope.tester_role == "client"
        assert "send_frame" in scope.exported_actions

    def test_find_scope_in_second_protocol(self, ws):
        """Scope found when it's in a different protocol."""
        _make_manifest(ws, "quic")  # No scopes
        apt_idx = _make_manifest(ws, "apt")
        _make_scope_file(
            apt_idx,
            "apt_test",
            _scope_dict("apt_test", "tests/apt_test.ivy", role="server"),
        )

        ctx = WorkspaceContext.load(str(ws))
        scope = ctx.get_test_scope("apt_test")
        assert scope is not None
        assert scope.tester_role == "server"

    def test_missing_scope_returns_none(self, ws):
        """Non-existent scope returns None."""
        _make_manifest(ws, "quic")

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.get_test_scope("nonexistent") is None


# ---------------------------------------------------------------------------
# Test: list_tests()
# ---------------------------------------------------------------------------


class TestListTests:
    """Test 9: list_tests() with and without protocol filter."""

    def test_list_all_tests(self, ws):
        """list_tests() without filter returns all tests sorted."""
        quic_idx = _make_manifest(ws, "quic")
        _make_scope_file(quic_idx, "test_b", _scope_dict("test_b", "tests/test_b.ivy"))
        _make_scope_file(quic_idx, "test_a", _scope_dict("test_a", "tests/test_a.ivy"))

        apt_idx = _make_manifest(ws, "apt")
        _make_scope_file(apt_idx, "test_c", _scope_dict("test_c", "tests/test_c.ivy"))

        ctx = WorkspaceContext.load(str(ws))
        tests = ctx.list_tests()
        assert tests == ["test_a", "test_b", "test_c"]

    def test_list_tests_filtered(self, ws):
        """list_tests(protocol=...) returns only that protocol's tests."""
        quic_idx = _make_manifest(ws, "quic")
        _make_scope_file(quic_idx, "quic_test", _scope_dict("quic_test", "t.ivy"))
        apt_idx = _make_manifest(ws, "apt")
        _make_scope_file(apt_idx, "apt_test", _scope_dict("apt_test", "t.ivy"))

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.list_tests(protocol="quic") == ["quic_test"]
        assert ctx.list_tests(protocol="apt") == ["apt_test"]

    def test_list_tests_unknown_protocol(self, ws):
        """list_tests(protocol=...) with unknown protocol returns empty."""
        _make_manifest(ws, "quic")

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.list_tests(protocol="nonexistent") == []


# ---------------------------------------------------------------------------
# Test: list_protocols()
# ---------------------------------------------------------------------------


class TestListProtocols:
    """Test 10: list_protocols() returns sorted list."""

    def test_sorted_protocols(self, ws):
        """Protocols are returned in sorted order."""
        _make_manifest(ws, "quic")
        _make_manifest(ws, "apt")
        _make_manifest(ws, "minip")

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.list_protocols() == ["apt", "minip", "quic"]

    def test_empty_protocols(self, ws):
        """No protocols -> empty list."""
        ctx = WorkspaceContext.load(str(ws))
        assert ctx.list_protocols() == []


# ---------------------------------------------------------------------------
# Test: resolve_include()
# ---------------------------------------------------------------------------


class TestResolveInclude:
    """resolve_include() searches include graphs and symbol keys."""

    def test_resolve_from_include_graph(self, ws):
        """Finds file via include graph edges."""
        idx_dir = _make_manifest(ws, "quic")
        _make_includes_json(idx_dir, {"conn.ivy": ["/opt/ivy/quic_types.ivy"]})

        ctx = WorkspaceContext.load(str(ws))
        result = ctx.resolve_include("quic_types")
        assert result == "/opt/ivy/quic_types.ivy"

    def test_resolve_from_symbols_keys(self, ws):
        """Finds file via symbols dict keys when not in include graph."""
        idx_dir = _make_manifest(ws, "quic")
        _make_symbols_json(
            idx_dir,
            {
                "/workspace/quic_frame.ivy": [
                    {"name": "frame", "kind": 5, "range": [0, 0, 0, 5]}
                ]
            },
        )

        ctx = WorkspaceContext.load(str(ws))
        result = ctx.resolve_include("quic_frame")
        assert result == "/workspace/quic_frame.ivy"

    def test_resolve_not_found(self, ws):
        """Returns None when include name not found."""
        _make_manifest(ws, "quic")

        ctx = WorkspaceContext.load(str(ws))
        assert ctx.resolve_include("nonexistent") is None


# ---------------------------------------------------------------------------
# Test: _load_json helper
# ---------------------------------------------------------------------------


class TestLoadJsonHelper:
    """Unit tests for the _load_json helper."""

    def test_valid_json(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"key": "value"}')
        assert _load_json(str(p)) == {"key": "value"}

    def test_missing_file(self, tmp_path):
        assert _load_json(str(tmp_path / "nope.json")) is None

    def test_corrupt_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{corrupt")
        assert _load_json(str(p)) is None


# ---------------------------------------------------------------------------
# Test: _load_pickle helper
# ---------------------------------------------------------------------------


class TestLoadPickleHelper:
    """Unit tests for the _load_pickle helper."""

    def test_valid_pickle(self, tmp_path):
        p = tmp_path / "obj.pickle.gz"
        obj = {"key": [1, 2, 3]}
        _make_pickle_gz(p, obj)
        assert _load_pickle(str(p), "test", "proto") == obj

    def test_missing_pickle(self, tmp_path):
        assert _load_pickle(str(tmp_path / "nope.pickle.gz"), "test", "proto") is None

    def test_corrupt_pickle(self, tmp_path):
        p = tmp_path / "bad.pickle.gz"
        p.write_bytes(b"not a pickle")
        assert _load_pickle(str(p), "test", "proto") is None


# ---------------------------------------------------------------------------
# Test: _load_scopes helper
# ---------------------------------------------------------------------------


class TestLoadScopesHelper:
    """Unit tests for the _load_scopes helper."""

    def test_no_scopes_dir(self, tmp_path):
        """Missing scopes/ directory -> empty dict."""
        assert _load_scopes(str(tmp_path), "quic") == {}

    def test_meta_json_bulk_load(self, tmp_path):
        """_meta.json with list of scope dicts."""
        scopes_dir = tmp_path / "scopes"
        scopes_dir.mkdir()
        (scopes_dir / "_meta.json").write_text(
            json.dumps(
                [
                    _scope_dict("test_a", "tests/test_a.ivy", role="client"),
                    _scope_dict("test_b", "tests/test_b.ivy", role="server"),
                ]
            )
        )

        result = _load_scopes(str(tmp_path), "quic")
        assert "test_a" in result
        assert "test_b" in result
        assert result["test_a"].tester_role == "client"

    def test_individual_scope_files(self, tmp_path):
        """Individual <test>.json files load independently."""
        scopes_dir = tmp_path / "scopes"
        scopes_dir.mkdir()
        (scopes_dir / "my_test.json").write_text(
            json.dumps(_scope_dict("my_test", "tests/my_test.ivy", role="mim"))
        )

        result = _load_scopes(str(tmp_path), "quic")
        assert "my_test" in result
        assert result["my_test"].tester_role == "mim"

    def test_meta_takes_precedence(self, tmp_path):
        """If test appears in both _meta.json and individual file, _meta wins."""
        scopes_dir = tmp_path / "scopes"
        scopes_dir.mkdir()

        # _meta says role=client
        (scopes_dir / "_meta.json").write_text(
            json.dumps(
                [
                    _scope_dict("test_x", "tests/test_x.ivy", role="client"),
                ]
            )
        )
        # Individual file says role=server
        (scopes_dir / "test_x.json").write_text(
            json.dumps(_scope_dict("test_x", "tests/test_x.ivy", role="server"))
        )

        result = _load_scopes(str(tmp_path), "quic")
        assert result["test_x"].tester_role == "client"


# ---------------------------------------------------------------------------
# Test: StalenessInfo and ProtocolIndex dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Verify dataclass defaults and construction."""

    def test_staleness_info(self):
        s = StalenessInfo(status="fresh", changed_files=0, total_files=10)
        assert s.status == "fresh"
        assert s.changed_files == 0
        assert s.total_files == 10

    def test_protocol_index_defaults(self):
        idx = ProtocolIndex(
            protocol="quic",
            index_dir="/tmp/idx",
            manifest={"version": 1},
        )
        assert idx.symbols == {}
        assert idx.includes.to_edges() == {}
        assert idx.exports == {}
        assert idx.scopes == {}
        assert idx.semantic_model is None
        assert idx.requirement_graph is None
        assert idx.staleness.status == "stale_major"


# ---------------------------------------------------------------------------
# Test: Per-file FileChange tracking in _check_staleness
# ---------------------------------------------------------------------------


class TestFileChangeStaleness:
    """Tests for per-file change tracking in _check_staleness."""

    def test_fresh_index_has_empty_file_changes(self, tmp_path):
        """When all mtimes match, file_changes is empty."""
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t")
        manifest = {
            "files": {
                "a.ivy": {"mtime": f.stat().st_mtime, "sha256": "abc123"},
            }
        }
        info = WorkspaceContext._check_staleness(manifest, str(tmp_path))
        assert info.status == "fresh"
        assert info.file_changes == []

    def test_modified_file_appears_in_file_changes(self, tmp_path):
        """A file with changed mtime appears with reason='modified'."""
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t")
        manifest = {
            "files": {
                "a.ivy": {"mtime": 0.0, "sha256": "abc123"},
            }
        }
        info = WorkspaceContext._check_staleness(manifest, str(tmp_path))
        assert info.changed_files == 1
        assert len(info.file_changes) == 1
        assert info.file_changes[0].rel_path == "a.ivy"
        assert info.file_changes[0].reason == "modified"
        assert info.file_changes[0].cached_sha256 == "abc123"

    def test_removed_file_appears_in_file_changes(self, tmp_path):
        """A file in manifest but missing on disk has reason='removed'."""
        manifest = {
            "files": {
                "gone.ivy": {"mtime": 100.0, "sha256": "def456"},
            }
        }
        info = WorkspaceContext._check_staleness(manifest, str(tmp_path))
        assert info.changed_files == 1
        assert len(info.file_changes) == 1
        assert info.file_changes[0].rel_path == "gone.ivy"
        assert info.file_changes[0].reason == "removed"
        assert info.file_changes[0].cached_sha256 == "def456"

    def test_missing_sha256_in_manifest_returns_none(self, tmp_path):
        """When manifest entry lacks sha256, cached_sha256 is None."""
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7")
        manifest = {
            "files": {
                "a.ivy": {"mtime": 0.0},
            }
        }
        info = WorkspaceContext._check_staleness(manifest, str(tmp_path))
        assert info.file_changes[0].cached_sha256 is None
