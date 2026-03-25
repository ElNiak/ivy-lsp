"""In-memory dirty layer tracking file mutations during an LSP session.

Tracks created/modified/deleted files with fast Tier 2/3 indexing so that
overlay entries are available within milliseconds of a ``didChange`` or
``didCreate`` notification.  The overlay sits in front of the persistent
workspace index: queries check it first, and ``None`` means "fall through
to the cached index".

Thread-safe: all mutation methods acquire ``_lock``.  Read methods return
snapshots for eventual consistency.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ivy_lsp.analysis.test_scope import ExportImportInfo, TestScope
from ivy_lsp.parsing.symbols import IvySymbol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class OverlayEntry:
    """Entry for a created or modified file in the overlay."""

    symbols: List[IvySymbol] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)
    exports: Optional[ExportImportInfo] = None
    completeness: str = "partial"  # "complete" | "partial"
    dirty_since: float = 0.0  # time.time() when changed


# ---------------------------------------------------------------------------
# SessionOverlay
# ---------------------------------------------------------------------------


class SessionOverlay:
    """In-memory dirty layer tracking file mutations during a session.

    Thread-safe: all mutation methods acquire ``_lock``.  Read methods
    return snapshots for eventual consistency.
    """

    def __init__(self) -> None:
        """Initialize empty overlay state."""
        self._created_files: Dict[str, OverlayEntry] = {}
        self._modified_files: Dict[str, OverlayEntry] = {}
        self._deleted_files: Set[str] = set()
        self._lock = threading.Lock()

    # -- Mutation methods (acquire lock) ------------------------------------

    def notify_file_change(self, path: str, content: str) -> None:
        """Handle file modification.  Fast-indexes content immediately.

        Extracts symbols, includes, and exports outside the lock, then
        stores the resulting ``OverlayEntry`` under the lock.
        """
        entry = self._fast_index(path, content)

        with self._lock:
            # If the file was previously deleted, un-delete it.
            self._deleted_files.discard(path)

            if path in self._created_files:
                # File was created this session -- update the created entry.
                self._created_files[path] = entry
            else:
                self._modified_files[path] = entry

        logger.debug(
            "overlay: file_change %s  symbols=%d includes=%d completeness=%s",
            path,
            len(entry.symbols),
            len(entry.includes),
            entry.completeness,
        )

    def notify_file_create(self, path: str) -> None:
        """Handle file creation.  Creates an empty entry (no content yet).

        The entry will be populated when ``notify_file_change`` is called
        with the file's content (typically from the next ``didChange``).
        """
        entry = OverlayEntry(
            completeness="partial",
            dirty_since=time.time(),
        )

        with self._lock:
            self._deleted_files.discard(path)
            self._created_files[path] = entry
            # Remove from modified if it was there (fresh create supersedes).
            self._modified_files.pop(path, None)

        logger.debug("overlay: file_create %s (empty entry)", path)

    def notify_file_delete(self, path: str) -> None:
        """Handle file deletion."""
        with self._lock:
            self._created_files.pop(path, None)
            self._modified_files.pop(path, None)
            self._deleted_files.add(path)

        logger.debug("overlay: file_delete %s", path)

    def clear(self) -> None:
        """Reset all overlay state."""
        with self._lock:
            self._created_files.clear()
            self._modified_files.clear()
            self._deleted_files.clear()

        logger.debug("overlay: cleared")

    # -- Read methods (return snapshots) ------------------------------------

    def get_effective_symbols(self, path: str) -> Optional[List[IvySymbol]]:
        """Return overlay symbols for *path*, or ``None`` to fall through.

        Deleted files return an empty list (not ``None``) to prevent the
        caller from falling through to a stale cached index entry.
        """
        if path in self._deleted_files:
            return []

        entry = self._created_files.get(path) or self._modified_files.get(path)
        if entry is not None:
            return list(entry.symbols)
        return None

    def get_effective_includes(self, path: str) -> Optional[List[str]]:
        """Return overlay includes for *path*, or ``None`` to fall through.

        Deleted files return an empty list (not ``None``) to prevent the
        caller from falling through to a stale cached index entry.
        """
        if path in self._deleted_files:
            return []

        entry = self._created_files.get(path) or self._modified_files.get(path)
        if entry is not None:
            return list(entry.includes)
        return None

    def get_effective_exports(self, path: str) -> Optional[ExportImportInfo]:
        """Return overlay export/import info for *path*, or ``None``."""
        if path in self._deleted_files:
            return ExportImportInfo(file=path)

        entry = self._created_files.get(path) or self._modified_files.get(path)
        if entry is not None:
            return entry.exports
        return None

    def is_deleted(self, path: str) -> bool:
        """Return ``True`` if *path* is marked as deleted in the overlay."""
        return path in self._deleted_files

    def dirty_files(self) -> Set[str]:
        """Return all files with overlay entries (created + modified)."""
        return set(self._created_files.keys()) | set(self._modified_files.keys())

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _fast_index(path: str, content: str) -> OverlayEntry:
        """Extract symbols, includes, and exports using Tier 2/3 only.

        Runs *outside* the lock so that extraction does not block
        concurrent readers.
        """
        from ivy_lsp.analysis.light_mode_extractor import extract_exports_imports_light
        from ivy_lsp.parsing.tiered_extractor import TieredExtractor

        now = time.time()

        # Use Tier 2/3 only (skip parser to keep latency low).
        extractor = TieredExtractor(parser_timeout=0.0)
        extractor._parser_available = False

        try:
            result = extractor.extract(source=content, filepath=path)
            symbols = result.symbols
            includes = result.includes
            completeness = "complete" if result.tier_used in (2, 3) else "partial"
        except Exception:
            logger.debug(
                "overlay: fast-index extraction failed for %s", path, exc_info=True
            )
            symbols = []
            includes = []
            completeness = "partial"

        try:
            exports = extract_exports_imports_light(content, path)
        except Exception:
            logger.debug(
                "overlay: fast-index export extraction failed for %s",
                path,
                exc_info=True,
            )
            exports = ExportImportInfo(file=path)

        return OverlayEntry(
            symbols=symbols,
            includes=includes,
            exports=exports,
            completeness=completeness,
            dirty_since=now,
        )


# ---------------------------------------------------------------------------
# TestScopeView
# ---------------------------------------------------------------------------


class TestScopeView:
    """Lightweight filter over index+overlay for one test's transitive closure.

    Provides a scoped view of workspace data filtered to a specific test's
    include closure. When overlay files are dirty, the view reports stale.
    """

    def __init__(
        self,
        name: str,
        test_name: str,
        protocol: str,
        scope: TestScope,
        overlay: SessionOverlay,
    ) -> None:
        """Initialize a scoped view for a specific test."""
        self.name = name
        self.test_name = test_name
        self.protocol = protocol
        self._scope = scope
        self._overlay = overlay
        self._is_stale = False

    @property
    def is_stale(self) -> bool:
        """True if overlay has touched a file in this scope."""
        for path in self._overlay.dirty_files():
            if self._scope.is_file_in_scope(path):
                return True
        return self._is_stale

    def files_in_scope(self) -> List[str]:
        """Return all files in scope, including overlay additions."""
        base = set(self._scope.include_closure)
        # Add overlay files that might be relevant (new files)
        for path in self._overlay.dirty_files():
            if self._scope.is_file_in_scope(path):
                base.add(path)
        # Remove deleted files
        base = {f for f in base if not self._overlay.is_deleted(f)}
        return sorted(base)

    def symbols_in_scope(self) -> List[IvySymbol]:
        """Return symbols from all files in scope, preferring overlay data."""
        symbols: List[IvySymbol] = []
        for path in self.files_in_scope():
            overlay_syms = self._overlay.get_effective_symbols(path)
            if overlay_syms is not None:
                symbols.extend(overlay_syms)
            # When overlay_syms is None, caller should fall through to index
        return symbols

    def refresh(self) -> None:
        """Mark the view as fresh (acknowledge overlay changes)."""
        self._is_stale = False

    def completeness(self) -> str:
        """'complete' if all files exist and are indexed, else 'partial' or 'building'."""
        files = self.files_in_scope()
        if not files:
            return "building"
        for path in files:
            if self._overlay.is_deleted(path):
                return "partial"
        return "complete"
