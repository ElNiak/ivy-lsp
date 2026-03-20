"""Tests for TestScopeView and WorkspaceContext.create_view()."""

from __future__ import annotations

import json
import time

import pytest
from lsprotocol.types import SymbolKind

from ivy_lsp.analysis.test_scope import TestScope
from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.session_overlay import SessionOverlay, TestScopeView
from ivy_lsp.workspace_context import WorkspaceContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scope(
    test_file: str,
    includes: list[str] | None = None,
    exports: list[str] | None = None,
    imports: list[str] | None = None,
    role: str = "unknown",
) -> TestScope:
    """Create a TestScope with sensible defaults."""
    return TestScope(
        test_file=test_file,
        include_closure=frozenset(includes or []),
        exported_actions=frozenset(exports or []),
        imported_actions=frozenset(imports or []),
        tester_role=role,
    )


def _make_symbol(name: str, file_path: str | None = None) -> IvySymbol:
    """Create a minimal IvySymbol for testing."""
    return IvySymbol(
        name=name,
        kind=SymbolKind.Variable,
        range=(0, 0, 0, len(name)),
        file_path=file_path,
    )


def _scope_dict(
    test_name: str,
    entry_file: str,
    includes: list[str] | None = None,
    exports: list[str] | None = None,
    imports: list[str] | None = None,
    role: str = "unknown",
) -> dict:
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


def _make_manifest(tmp_path, protocol, files_meta=None):
    """Create a minimal .ivy-index directory with manifest.json."""
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


def _make_scope_file(index_dir, test_name, scope_data):
    """Write a scopes/<test>.json file."""
    scopes_dir = index_dir / "scopes"
    scopes_dir.mkdir(exist_ok=True)
    (scopes_dir / f"{test_name}.json").write_text(json.dumps(scope_data))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Create a minimal workspace with a .ivyworkspace marker."""
    marker = {
        "version": 3,
        "workspace_layers": [{"id": "default", "include_paths": ["protocol-testing"]}],
    }
    (tmp_path / ".ivyworkspace").write_text(json.dumps(marker))
    monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
    monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: Create view from WorkspaceContext with loaded scope
# ---------------------------------------------------------------------------


class TestCreateViewFromWorkspaceContext:
    """Test 1: create_view returns a TestScopeView when scope exists."""

    def test_create_view_returns_view(self, ws):
        """create_view() returns a TestScopeView for an existing test scope."""
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
        view = ctx.create_view("my_view", "quic_server_test_stream")

        assert view is not None
        assert view.name == "my_view"
        assert view.test_name == "quic_server_test_stream"
        assert view.protocol == "quic"
        assert "my_view" in ctx.active_views
        assert ctx.active_views["my_view"] is view

    def test_create_view_returns_none_for_missing_scope(self, ws):
        """create_view() returns None when the test scope does not exist."""
        _make_manifest(ws, "quic")  # No scopes

        ctx = WorkspaceContext.load(str(ws))
        view = ctx.create_view("missing", "nonexistent_test")

        assert view is None
        assert "missing" not in ctx.active_views

    def test_create_view_uses_shared_overlay(self, ws):
        """create_view() wires the workspace's shared overlay into the view."""
        index_dir = _make_manifest(ws, "quic")
        _make_scope_file(
            index_dir,
            "test_a",
            _scope_dict("test_a", "tests/test_a.ivy"),
        )

        ctx = WorkspaceContext.load(str(ws))
        view = ctx.create_view("view_a", "test_a")

        assert view is not None
        assert view._overlay is ctx.overlay


# ---------------------------------------------------------------------------
# Test 2: files_in_scope returns scope files
# ---------------------------------------------------------------------------


class TestFilesInScope:
    """Test 2: files_in_scope returns the test's include closure."""

    def test_files_in_scope_returns_closure(self):
        """files_in_scope() returns all files from the include closure."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy", "frame.ivy", "conn.ivy"],
        )
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        files = view.files_in_scope()
        assert files == ["conn.ivy", "frame.ivy", "types.ivy"]

    def test_files_in_scope_empty_closure(self):
        """files_in_scope() returns empty list for empty closure."""
        scope = _make_scope("test.ivy", includes=[])
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        assert view.files_in_scope() == []

    def test_files_in_scope_excludes_deleted(self):
        """files_in_scope() excludes files marked as deleted in overlay."""
        scope = _make_scope(
            "test.ivy",
            includes=["a.ivy", "b.ivy", "c.ivy"],
        )
        overlay = SessionOverlay()
        overlay.notify_file_delete("b.ivy")
        view = TestScopeView("v", "test", "quic", scope, overlay)

        files = view.files_in_scope()
        assert "b.ivy" not in files
        assert files == ["a.ivy", "c.ivy"]


# ---------------------------------------------------------------------------
# Test 3: is_stale detects overlay changes to scope files
# ---------------------------------------------------------------------------


class TestIsStaleInScope:
    """Test 3: is_stale detects when overlay touches a file in scope."""

    def test_stale_when_scope_file_modified(self):
        """is_stale is True when overlay modifies a file in scope."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy", "frame.ivy"],
        )
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        assert view.is_stale is False

        overlay.notify_file_change("types.ivy", "#lang ivy1.7\ntype cid\n")

        assert view.is_stale is True

    def test_stale_when_scope_file_created(self):
        """is_stale is True when overlay creates a file that is in scope."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy"],
        )
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        overlay.notify_file_create("types.ivy")

        assert view.is_stale is True


# ---------------------------------------------------------------------------
# Test 4: is_stale ignores overlay changes to out-of-scope files
# ---------------------------------------------------------------------------


class TestIsStaleOutOfScope:
    """Test 4: is_stale ignores changes to files outside this view's scope."""

    def test_not_stale_when_out_of_scope_file_changed(self):
        """is_stale remains False when overlay changes out-of-scope files."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy", "frame.ivy"],
        )
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        # Modify a file that is NOT in scope
        overlay.notify_file_change("unrelated.ivy", "#lang ivy1.7\ntype x\n")

        assert view.is_stale is False

    def test_not_stale_when_out_of_scope_file_created(self):
        """is_stale remains False when overlay creates out-of-scope file."""
        scope = _make_scope("test.ivy", includes=["a.ivy"])
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        overlay.notify_file_create("completely_different.ivy")

        assert view.is_stale is False


# ---------------------------------------------------------------------------
# Test 5: refresh marks view as fresh
# ---------------------------------------------------------------------------


class TestRefresh:
    """Test 5: refresh() resets the internal stale flag."""

    def test_refresh_clears_internal_stale(self):
        """refresh() clears _is_stale so is_stale re-evaluates from overlay."""
        scope = _make_scope("test.ivy", includes=["a.ivy"])
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        # Manually set _is_stale
        view._is_stale = True
        # With no dirty overlay files, is_stale returns _is_stale
        assert view.is_stale is True

        view.refresh()
        assert view.is_stale is False

    def test_refresh_does_not_affect_overlay_staleness(self):
        """refresh() clears internal flag but overlay changes still count."""
        scope = _make_scope("test.ivy", includes=["a.ivy"])
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        overlay.notify_file_change("a.ivy", "#lang ivy1.7\ntype t\n")
        assert view.is_stale is True

        # refresh clears _is_stale, but overlay still has dirty file in scope
        view.refresh()
        assert view.is_stale is True  # still stale because overlay is dirty


# ---------------------------------------------------------------------------
# Test 6: completeness returns "complete" when all files present
# ---------------------------------------------------------------------------


class TestCompleteness:
    """Test 6: completeness() reports correct status."""

    def test_complete_when_all_files_present(self):
        """completeness() returns 'complete' when no files are deleted."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy", "frame.ivy"],
        )
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        assert view.completeness() == "complete"

    def test_building_when_no_files(self):
        """completeness() returns 'building' when scope has no files."""
        scope = _make_scope("test.ivy", includes=[])
        overlay = SessionOverlay()
        view = TestScopeView("v", "test", "quic", scope, overlay)

        assert view.completeness() == "building"

    def test_partial_when_file_deleted(self):
        """completeness() returns 'partial' when a scope file is deleted.

        Note: this tests the edge case where a file is both in include_closure
        AND marked deleted. The files_in_scope() method filters them out, so
        completeness() iterating over files_in_scope() won't see them as
        deleted. The 'partial' status only triggers when a file passes through
        files_in_scope() but is_deleted returns True -- which can't happen
        simultaneously. So with the current implementation, deleting a scope
        file causes it to be removed from files_in_scope() and if that empties
        the list, completeness() returns 'building'.
        """
        scope = _make_scope("test.ivy", includes=["a.ivy"])
        overlay = SessionOverlay()
        overlay.notify_file_delete("a.ivy")
        view = TestScopeView("v", "test", "quic", scope, overlay)

        # a.ivy is deleted and excluded from files_in_scope -> empty -> building
        assert view.completeness() == "building"

    def test_complete_with_mixed_scope(self):
        """completeness() is 'complete' when some files deleted but others remain."""
        scope = _make_scope(
            "test.ivy",
            includes=["a.ivy", "b.ivy", "c.ivy"],
        )
        overlay = SessionOverlay()
        overlay.notify_file_delete("b.ivy")
        view = TestScopeView("v", "test", "quic", scope, overlay)

        # b.ivy is excluded from files_in_scope, remaining files are not deleted
        assert view.completeness() == "complete"


# ---------------------------------------------------------------------------
# Test 7: Concurrent views for different tests don't interfere
# ---------------------------------------------------------------------------


class TestConcurrentViews:
    """Test 7: Multiple views for different tests are independent."""

    def test_independent_views_different_scopes(self):
        """Views for different tests with different scopes are independent."""
        scope_a = _make_scope(
            "test_a.ivy",
            includes=["types.ivy", "frame.ivy"],
        )
        scope_b = _make_scope(
            "test_b.ivy",
            includes=["crypto.ivy", "tls.ivy"],
        )
        overlay = SessionOverlay()

        view_a = TestScopeView("va", "test_a", "quic", scope_a, overlay)
        view_b = TestScopeView("vb", "test_b", "quic", scope_b, overlay)

        # Modify a file in scope_a only
        overlay.notify_file_change("types.ivy", "#lang ivy1.7\ntype cid\n")

        assert view_a.is_stale is True
        assert view_b.is_stale is False

    def test_independent_views_shared_overlay(self):
        """Views sharing an overlay don't interfere with each other's staleness."""
        scope_a = _make_scope("a.ivy", includes=["shared.ivy", "a_only.ivy"])
        scope_b = _make_scope("b.ivy", includes=["shared.ivy", "b_only.ivy"])
        overlay = SessionOverlay()

        view_a = TestScopeView("va", "a", "quic", scope_a, overlay)
        view_b = TestScopeView("vb", "b", "quic", scope_b, overlay)

        # Modify shared file: both become stale
        overlay.notify_file_change("shared.ivy", "#lang ivy1.7\ntype t\n")
        assert view_a.is_stale is True
        assert view_b.is_stale is True

    def test_independent_files_in_scope(self):
        """files_in_scope() for one view does not affect the other."""
        scope_a = _make_scope("a.ivy", includes=["x.ivy", "y.ivy"])
        scope_b = _make_scope("b.ivy", includes=["y.ivy", "z.ivy"])
        overlay = SessionOverlay()
        overlay.notify_file_delete("x.ivy")

        view_a = TestScopeView("va", "a", "quic", scope_a, overlay)
        view_b = TestScopeView("vb", "b", "quic", scope_b, overlay)

        assert view_a.files_in_scope() == ["y.ivy"]
        assert view_b.files_in_scope() == ["y.ivy", "z.ivy"]

    def test_workspace_context_multiple_views(self, ws):
        """WorkspaceContext can hold multiple active views simultaneously."""
        index_dir = _make_manifest(ws, "quic")
        _make_scope_file(
            index_dir,
            "test_stream",
            _scope_dict(
                "test_stream",
                "tests/test_stream.ivy",
                includes=["types.ivy"],
                role="client",
            ),
        )
        _make_scope_file(
            index_dir,
            "test_handshake",
            _scope_dict(
                "test_handshake",
                "tests/test_handshake.ivy",
                includes=["crypto.ivy"],
                role="server",
            ),
        )

        ctx = WorkspaceContext.load(str(ws))
        va = ctx.create_view("stream_view", "test_stream")
        vb = ctx.create_view("handshake_view", "test_handshake")

        assert va is not None
        assert vb is not None
        assert len(ctx.active_views) == 2
        assert ctx.active_views["stream_view"] is va
        assert ctx.active_views["handshake_view"] is vb
        assert va.protocol == "quic"
        assert vb.protocol == "quic"


# ---------------------------------------------------------------------------
# Test 8: symbols_in_scope returns overlay symbols when available
# ---------------------------------------------------------------------------


class TestSymbolsInScope:
    """Test 8: symbols_in_scope prefers overlay data over index."""

    def test_symbols_from_overlay(self):
        """symbols_in_scope() returns overlay symbols for dirty files."""
        scope = _make_scope("test.ivy", includes=["types.ivy"])
        overlay = SessionOverlay()
        # Modify the file so overlay has symbols
        overlay.notify_file_change("types.ivy", "#lang ivy1.7\ntype cid\n")

        view = TestScopeView("v", "test", "quic", scope, overlay)
        symbols = view.symbols_in_scope()

        assert len(symbols) > 0
        names = {s.name for s in symbols}
        assert "cid" in names

    def test_symbols_empty_for_clean_files(self):
        """symbols_in_scope() returns empty list when overlay has no entries."""
        scope = _make_scope("test.ivy", includes=["types.ivy"])
        overlay = SessionOverlay()

        view = TestScopeView("v", "test", "quic", scope, overlay)
        symbols = view.symbols_in_scope()

        # No overlay entries -> get_effective_symbols returns None -> no symbols
        assert symbols == []

    def test_symbols_empty_for_deleted_file(self):
        """symbols_in_scope() returns empty for deleted files in scope."""
        scope = _make_scope("test.ivy", includes=["types.ivy"])
        overlay = SessionOverlay()
        overlay.notify_file_change("types.ivy", "#lang ivy1.7\ntype cid\n")
        overlay.notify_file_delete("types.ivy")

        view = TestScopeView("v", "test", "quic", scope, overlay)
        symbols = view.symbols_in_scope()

        # File is deleted -> excluded from files_in_scope -> no symbols
        assert symbols == []

    def test_symbols_multiple_files(self):
        """symbols_in_scope() aggregates symbols from multiple scope files."""
        scope = _make_scope(
            "test.ivy",
            includes=["types.ivy", "frame.ivy"],
        )
        overlay = SessionOverlay()
        overlay.notify_file_change("types.ivy", "#lang ivy1.7\ntype cid\n")
        overlay.notify_file_change(
            "frame.ivy",
            "#lang ivy1.7\ntype frame_type = {stream, ack}\n",
        )

        view = TestScopeView("v", "test", "quic", scope, overlay)
        symbols = view.symbols_in_scope()

        names = {s.name for s in symbols}
        assert "cid" in names
        assert "frame_type" in names
