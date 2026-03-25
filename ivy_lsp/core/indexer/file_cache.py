"""Mtime-based file parse cache with LRU eviction."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CachedFile:
    """Cached parse result for a single file."""

    filepath: str
    mtime: float
    parse_result: Any
    symbols: List[Any]
    includes: List[str] = field(default_factory=list)
    requirements: List[Any] = field(default_factory=list)
    writes: List[Any] = field(default_factory=list)
    export_import_info: Any = None


class FileCache:
    """LRU cache of parsed Ivy file results, keyed by filepath.

    All access to the internal ``_cache`` is guarded by a lock so that
    concurrent threads (init, deep-index daemon, LSP handler) can
    safely read and write without data races.
    """

    def __init__(self, max_size: int = 500) -> None:
        """Initialize an empty LRU cache with the given capacity."""
        self._max_size = max_size
        self._cache: OrderedDict[str, CachedFile] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, filepath: str) -> Optional[CachedFile]:
        """Return cached entry if it exists and mtime still matches.

        Moves the entry to the end of the LRU order on hit.
        Returns None and removes the entry if the file was modified
        or deleted since caching.
        """
        with self._lock:
            entry = self._cache.get(filepath)
            if entry is None:
                return None
            try:
                current_mtime = os.path.getmtime(filepath)
            except OSError:
                self._cache.pop(filepath, None)
                return None
            if current_mtime != entry.mtime:
                self._cache.pop(filepath, None)
                return None
            self._cache.move_to_end(filepath)
            return entry

    def put(
        self,
        filepath: str,
        result: Any,
        symbols: List[Any],
        includes: Optional[List[str]] = None,
        requirements: Optional[List[Any]] = None,
        writes: Optional[List[Any]] = None,
        export_import_info: Any = None,
    ) -> None:
        """Store a parse result with the file's current mtime.

        Evicts the oldest entry if the cache exceeds *max_size*.
        Silently returns if the file cannot be stat'd.
        """
        with self._lock:
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                logger.debug("Cannot stat %s; parse result not cached", filepath)
                return
            entry = CachedFile(
                filepath=filepath,
                mtime=mtime,
                parse_result=result,
                symbols=symbols,
                includes=includes or [],
                requirements=requirements or [],
                writes=writes or [],
                export_import_info=export_import_info,
            )
            self._cache[filepath] = entry
            self._cache.move_to_end(filepath)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate(self, filepath: str) -> None:
        """Remove *filepath* from the cache. No error if absent."""
        with self._lock:
            self._cache.pop(filepath, None)

    def invalidate_dependents(self, filepath: str, include_graph: Any) -> None:
        """Invalidate all files that directly include *filepath*.

        Uses ``include_graph.get_included_by(filepath)`` to discover
        dependents.  The file itself is **not** invalidated.
        """
        dependents = include_graph.get_included_by(filepath)
        with self._lock:
            for dep in dependents:
                self._cache.pop(dep, None)


class PersistentFileCache:
    """SQLite-backed file cache with in-memory LRU hot layer.

    Data path: ``~/.cache/ivy-lsp/<hash>/index.db``
    """

    SCHEMA_VERSION = "1"

    def __init__(
        self,
        workspace_root: str,
        max_memory: int = 200,
        cache_dir: Optional[str] = None,
    ) -> None:
        """Initialize SQLite-backed cache for the given workspace."""
        self._workspace_root = workspace_root
        self._memory = FileCache(max_size=max_memory)
        self._db_path = self._get_db_path(workspace_root, cache_dir)
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _get_db_path(self, workspace_root: str, cache_dir: Optional[str] = None) -> str:
        h = hashlib.sha256(os.path.abspath(workspace_root).encode()).hexdigest()[:16]
        if cache_dir is not None:
            cache_base = os.path.join(cache_dir, h)
        else:
            cache_base = os.path.join(
                os.path.expanduser("~"),
                ".cache",
                "ivy-lsp",
                h,
            )
        return os.path.join(cache_base, "index.db")

    def _init_schema(self) -> None:
        cur = self._db.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY, value TEXT)"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS file_cache (
                filepath TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                symbols_json TEXT NOT NULL,
                includes_json TEXT NOT NULL,
                cached_at REAL NOT NULL)"""
        )
        row = cur.execute(
            "SELECT value FROM cache_meta WHERE key='schema_version'"
        ).fetchone()
        stored_version = row[0] if row else None
        if stored_version != self.SCHEMA_VERSION:
            cur.execute("DELETE FROM file_cache")
            cur.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) "
                "VALUES ('schema_version', ?)",
                (self.SCHEMA_VERSION,),
            )
        self._db.commit()

    def get(self, filepath: str) -> Optional[CachedFile]:
        """Look up a file in memory then SQLite; return None if stale."""
        cached = self._memory.get(filepath)
        if cached is not None:
            return cached
        with self._db_lock:
            try:
                row = self._db.execute(
                    "SELECT * FROM file_cache WHERE filepath=?",
                    (filepath,),
                ).fetchone()
            except sqlite3.Error:
                logger.debug("SQLite read failed for %s", filepath, exc_info=True)
                return None
        if row is None:
            return None
        try:
            current_mtime = os.path.getmtime(filepath)
        except OSError:
            return None
        if current_mtime != row["mtime"]:
            with self._db_lock:
                try:
                    self._db.execute(
                        "DELETE FROM file_cache WHERE filepath=?",
                        (filepath,),
                    )
                    self._db.commit()
                except sqlite3.Error:
                    logger.debug("SQLite delete failed for %s", filepath, exc_info=True)
            return None
        try:
            from ivy_lsp.parsing.symbols import IvySymbol

            symbols = [IvySymbol.from_dict(d) for d in json.loads(row["symbols_json"])]
            includes = json.loads(row["includes_json"])
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.debug("Corrupt cache entry for %s", filepath, exc_info=True)
            return None
        entry = CachedFile(
            filepath=filepath,
            mtime=row["mtime"],
            parse_result=None,
            symbols=symbols,
            includes=includes,
        )
        self._memory.put(filepath, None, symbols, includes)
        return entry

    def put(
        self,
        filepath: str,
        result: Any,
        symbols: List[Any],
        includes: Optional[List[str]] = None,
        requirements: Optional[List[Any]] = None,
        writes: Optional[List[Any]] = None,
        export_import_info: Any = None,
    ) -> None:
        """Store a parse result in memory and persist to SQLite."""
        self._memory.put(
            filepath,
            result,
            symbols,
            includes,
            requirements=requirements,
            writes=writes,
            export_import_info=export_import_info,
        )
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            return
        with self._db_lock:
            try:
                syms_json = json.dumps([s.to_dict() for s in symbols])
                incs_json = json.dumps(includes or [])
                self._db.execute(
                    """INSERT OR REPLACE INTO file_cache
                       (filepath, mtime, symbols_json, includes_json, cached_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (filepath, mtime, syms_json, incs_json, time.time()),
                )
                self._db.commit()
            except (sqlite3.Error, TypeError):
                logger.debug("SQLite write failed for %s", filepath, exc_info=True)

    def invalidate(self, filepath: str) -> None:
        """Remove a file from both memory and SQLite caches."""
        self._memory.invalidate(filepath)
        with self._db_lock:
            try:
                self._db.execute(
                    "DELETE FROM file_cache WHERE filepath=?",
                    (filepath,),
                )
                self._db.commit()
            except sqlite3.Error:
                logger.debug("SQLite invalidate failed for %s", filepath, exc_info=True)

    def invalidate_dependents(
        self,
        filepath: str,
        include_graph: Any,
    ) -> None:
        """Invalidate files that include the given file."""
        self._memory.invalidate_dependents(filepath, include_graph)
        dependents = include_graph.get_included_by(filepath)
        with self._db_lock:
            try:
                for dep in dependents:
                    self._db.execute(
                        "DELETE FROM file_cache WHERE filepath=?",
                        (dep,),
                    )
                self._db.commit()
            except sqlite3.Error:
                logger.debug(
                    "SQLite invalidate_dependents failed for %s",
                    filepath,
                    exc_info=True,
                )

    def clear_all(self) -> None:
        """Wipe both memory and SQLite caches entirely."""
        self._memory = FileCache(max_size=self._memory._max_size)
        with self._db_lock:
            try:
                self._db.execute("DELETE FROM file_cache")
                self._db.commit()
            except sqlite3.Error:
                logger.debug("SQLite clear_all failed", exc_info=True)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._db_lock:
            try:
                self._db.close()
            except sqlite3.Error:
                logger.debug("SQLite close failed", exc_info=True)
