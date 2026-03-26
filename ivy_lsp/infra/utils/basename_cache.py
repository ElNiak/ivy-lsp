"""Thread-safe basename cache for .ivy file lookups."""

import os
import threading
from typing import Callable


class BasenameCache:
    """Thread-safe lazy cache mapping .ivy basenames to relative paths.

    Uses double-check locking to build the cache on first access and
    return it instantly on subsequent calls.
    """

    def __init__(
        self,
        find_files_fn: Callable[[str], list[str]],
        root: str,
    ) -> None:
        """Initialize with a file-finder callable and workspace root."""
        self._find_files_fn = find_files_fn
        self._root = root
        self._cache: dict[str, list[str]] | None = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, list[str]]:
        """Return cached basename->paths map, building on first call."""
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is not None:
                return self._cache
            cache: dict[str, list[str]] = {}
            for rel_path in self._find_files_fn(self._root):
                basename = os.path.basename(rel_path)[:-4]  # strip .ivy
                cache.setdefault(basename, []).append(rel_path)
            self._cache = cache
            return cache
