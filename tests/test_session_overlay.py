"""Tests for ivy_lsp.core.workspace.session_overlay."""

from __future__ import annotations

import threading
import time

from ivy_lsp.core.workspace.session_overlay import OverlayEntry, SessionOverlay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IVY_CONTENT_WITH_SYMBOLS = """\
#lang ivy1.7

type packet_type = {initial, handshake, retry}

action send_packet(pkt: packet_type) = {
    require pkt ~= retry;
}

relation connected(X: node, Y: node)
"""

IVY_CONTENT_WITH_INCLUDE = """\
#lang ivy1.7

include quic_connection

action foo = {
    require true;
}
"""


# ---------------------------------------------------------------------------
# OverlayEntry defaults
# ---------------------------------------------------------------------------


class TestOverlayEntry:
    def test_defaults(self):
        entry = OverlayEntry()
        assert entry.symbols == []
        assert entry.includes == []
        assert entry.exports is None
        assert entry.completeness == "partial"
        assert entry.dirty_since == 0.0


# ---------------------------------------------------------------------------
# SessionOverlay — basic operations
# ---------------------------------------------------------------------------


class TestSessionOverlayBasic:
    def test_create_file_returns_empty_symbols(self):
        """Create file, get_effective_symbols returns empty (no content yet)."""
        overlay = SessionOverlay()
        overlay.notify_file_create("/tmp/test.ivy")

        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None
        assert symbols == []

    def test_create_file_returns_empty_includes(self):
        """Create file, get_effective_includes returns empty (no content yet)."""
        overlay = SessionOverlay()
        overlay.notify_file_create("/tmp/test.ivy")

        includes = overlay.get_effective_includes("/tmp/test.ivy")
        assert includes is not None
        assert includes == []

    def test_modify_file_with_content_returns_parsed_symbols(self):
        """Modify file with content, get_effective_symbols returns parsed symbols."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)

        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None
        assert len(symbols) > 0

        # Check that we got the expected symbol names.
        names = {s.name for s in symbols}
        assert "packet_type" in names
        assert "send_packet" in names
        assert "connected" in names

    def test_modify_file_with_content_returns_parsed_includes(self):
        """Modify file with include, get_effective_includes returns include names."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_INCLUDE)

        includes = overlay.get_effective_includes("/tmp/test.ivy")
        assert includes is not None
        assert "quic_connection" in includes

    def test_modify_file_populates_exports(self):
        """Modify file with content, get_effective_exports returns info."""
        content = """\
#lang ivy1.7

export quic.send
import tls.handshake
"""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", content)

        exports = overlay.get_effective_exports("/tmp/test.ivy")
        assert exports is not None
        assert "quic.send" in exports.exports
        assert "tls.handshake" in exports.imports

    def test_delete_file_is_deleted_returns_true(self):
        """Delete file, is_deleted returns True."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/test.ivy")

        assert overlay.is_deleted("/tmp/test.ivy") is True

    def test_deleted_file_symbols_returns_empty_list(self):
        """Deleted file returns empty lists (not None) for symbols."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/test.ivy")

        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None  # Must NOT be None (would cause fallthrough)
        assert symbols == []

    def test_deleted_file_includes_returns_empty_list(self):
        """Deleted file returns empty list (not None) for includes."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_INCLUDE)
        overlay.notify_file_delete("/tmp/test.ivy")

        includes = overlay.get_effective_includes("/tmp/test.ivy")
        assert includes is not None
        assert includes == []

    def test_deleted_file_exports_returns_empty_info(self):
        """Deleted file returns empty ExportImportInfo (not None)."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/test.ivy")

        exports = overlay.get_effective_exports("/tmp/test.ivy")
        assert exports is not None
        assert exports.exports == []
        assert exports.imports == []


# ---------------------------------------------------------------------------
# dirty_files
# ---------------------------------------------------------------------------


class TestDirtyFiles:
    def test_dirty_files_returns_created_and_modified(self):
        """dirty_files returns all created + modified files."""
        overlay = SessionOverlay()
        overlay.notify_file_create("/tmp/new.ivy")
        overlay.notify_file_change("/tmp/existing.ivy", IVY_CONTENT_WITH_SYMBOLS)

        dirty = overlay.dirty_files()
        assert "/tmp/new.ivy" in dirty
        assert "/tmp/existing.ivy" in dirty
        assert len(dirty) == 2

    def test_dirty_files_excludes_deleted(self):
        """Deleted files are removed from dirty_files."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/a.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_change("/tmp/b.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/a.ivy")

        dirty = overlay.dirty_files()
        assert "/tmp/a.ivy" not in dirty
        assert "/tmp/b.ivy" in dirty

    def test_dirty_files_empty_initially(self):
        overlay = SessionOverlay()
        assert overlay.dirty_files() == set()


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_resets_all_state(self):
        """Clear resets all state."""
        overlay = SessionOverlay()
        overlay.notify_file_create("/tmp/new.ivy")
        overlay.notify_file_change("/tmp/mod.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/del.ivy")

        overlay.clear()

        assert overlay.dirty_files() == set()
        assert overlay.is_deleted("/tmp/del.ivy") is False
        assert overlay.get_effective_symbols("/tmp/new.ivy") is None
        assert overlay.get_effective_symbols("/tmp/mod.ivy") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_file_returns_none(self):
        """Query for unknown file returns None (fall through to index)."""
        overlay = SessionOverlay()
        assert overlay.get_effective_symbols("/tmp/unknown.ivy") is None
        assert overlay.get_effective_includes("/tmp/unknown.ivy") is None
        assert overlay.get_effective_exports("/tmp/unknown.ivy") is None
        assert overlay.is_deleted("/tmp/unknown.ivy") is False

    def test_modify_nonexistent_file_creates_entry(self):
        """Modify non-existent file creates an entry (implicit create)."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/brand_new.ivy", IVY_CONTENT_WITH_SYMBOLS)

        # Should be in modified (not created), but accessible.
        symbols = overlay.get_effective_symbols("/tmp/brand_new.ivy")
        assert symbols is not None
        assert len(symbols) > 0
        assert "/tmp/brand_new.ivy" in overlay.dirty_files()

    def test_create_then_modify_updates_created_entry(self):
        """Create file then modify it updates the created entry."""
        overlay = SessionOverlay()
        overlay.notify_file_create("/tmp/test.ivy")

        # Initially empty.
        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols == []

        # Now modify with content.
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)

        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None
        assert len(symbols) > 0

        # Should still be in _created_files, not _modified_files.
        assert "/tmp/test.ivy" in overlay._created_files
        assert "/tmp/test.ivy" not in overlay._modified_files

    def test_delete_then_recreate(self):
        """Delete then recreate a file un-deletes it."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        overlay.notify_file_delete("/tmp/test.ivy")
        assert overlay.is_deleted("/tmp/test.ivy") is True

        overlay.notify_file_create("/tmp/test.ivy")
        assert overlay.is_deleted("/tmp/test.ivy") is False
        # It should now be in created_files with an empty entry.
        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None
        assert symbols == []

    def test_modify_after_delete_undeletes(self):
        """Modifying a deleted file un-deletes it."""
        overlay = SessionOverlay()
        overlay.notify_file_delete("/tmp/test.ivy")
        assert overlay.is_deleted("/tmp/test.ivy") is True

        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        assert overlay.is_deleted("/tmp/test.ivy") is False
        symbols = overlay.get_effective_symbols("/tmp/test.ivy")
        assert symbols is not None
        assert len(symbols) > 0

    def test_empty_content_returns_empty_symbols(self):
        """Empty content produces an entry with no symbols."""
        overlay = SessionOverlay()
        overlay.notify_file_change("/tmp/empty.ivy", "")

        symbols = overlay.get_effective_symbols("/tmp/empty.ivy")
        assert symbols is not None
        assert symbols == []

    def test_dirty_since_is_set(self):
        """OverlayEntry.dirty_since is set to approximately now."""
        overlay = SessionOverlay()
        before = time.time()
        overlay.notify_file_change("/tmp/test.ivy", IVY_CONTENT_WITH_SYMBOLS)
        after = time.time()

        entry = overlay._modified_files["/tmp/test.ivy"]
        assert before <= entry.dirty_since <= after


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_modifications_dont_crash(self):
        """Concurrent modifications from multiple threads don't crash."""
        overlay = SessionOverlay()
        errors: list = []
        barrier = threading.Barrier(4)

        def writer(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(50):
                    path = f"/tmp/file_{idx}_{i}.ivy"
                    overlay.notify_file_create(path)
                    overlay.notify_file_change(path, f"type t{idx}_{i} = {{a, b}}\n")
                    if i % 3 == 0:
                        overlay.notify_file_delete(path)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    dirty = overlay.dirty_files()
                    for path in list(dirty)[:5]:
                        overlay.get_effective_symbols(path)
                        overlay.get_effective_includes(path)
                        overlay.is_deleted(path)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(2,)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Thread errors: {errors}"

    def test_clear_during_writes(self):
        """Clear during concurrent writes doesn't crash."""
        overlay = SessionOverlay()
        errors: list = []
        stop = threading.Event()

        def writer() -> None:
            try:
                i = 0
                while not stop.is_set():
                    overlay.notify_file_change(
                        f"/tmp/file_{i}.ivy", f"type t{i} = {{a}}\n"
                    )
                    i += 1
            except Exception as exc:
                errors.append(exc)

        def clearer() -> None:
            try:
                for _ in range(10):
                    overlay.clear()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=clearer)
        t1.start()
        t2.start()
        t2.join(timeout=10)
        stop.set()
        t1.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
