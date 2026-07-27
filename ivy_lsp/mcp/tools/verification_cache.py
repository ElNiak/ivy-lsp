"""Verification result cache: entry storage, freshness checks, eviction."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from ivy_lsp.core.patterns import INCLUDE_RE

logger = logging.getLogger(__name__)

CACHE_MAX_SIZE = 100

ISOLATE_STATUS_RE = re.compile(
    r"^\s*isolate\s+([\w.]+)\s*:\s*(PASS|FAIL|OK)\s*$", re.MULTILINE
)


@dataclass
class CacheEntry:
    """One cached verification result keyed by (abs_path, isolate)."""

    result: dict
    file_mtime: float
    include_mtimes: dict[str, float]


def create_cache() -> tuple[dict, asyncio.Lock, set]:
    """Create a fresh verification cache triple.

    Returns (cache_dict, async_lock, in_flight_set).
    """
    return {}, asyncio.Lock(), set()


def get_file_mtime(abs_path: str) -> float:
    """Get file mtime, returning 0.0 if file doesn't exist."""
    try:
        return os.path.getmtime(abs_path)
    except OSError:
        return 0.0


def get_include_mtimes(abs_path: str, basename_cache_fn: Any) -> dict[str, float]:
    """Get mtimes for the file's transitive include closure."""
    mtimes: dict[str, float] = {}
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        for m in INCLUDE_RE.finditer(source):
            inc_name = m.group(1)
            candidates = basename_cache_fn(inc_name)
            if candidates:
                inc_path = os.path.join(os.path.dirname(abs_path), candidates[0])
                mtimes[inc_path] = get_file_mtime(inc_path)
    except OSError:
        pass
    return mtimes


def cache_is_fresh(entry: CacheEntry, abs_path: str) -> bool:
    """Check if cached result is still fresh (no files changed)."""
    if get_file_mtime(abs_path) != entry.file_mtime:
        return False
    for inc_path, cached_mtime in entry.include_mtimes.items():
        if get_file_mtime(inc_path) != cached_mtime:
            return False
    return True


def evict_oldest(cache: dict, max_size: int = CACHE_MAX_SIZE) -> None:
    """Evict oldest cache entries when cache exceeds max_size."""
    while len(cache) > max_size:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key)


def cache_per_isolate_results(
    cache: dict,
    abs_path: str,
    raw_output: str,
    full_result: dict[str, Any],
) -> None:
    """Extract per-isolate status from verification output and cache each."""
    for m in ISOLATE_STATUS_RE.finditer(raw_output):
        iso_name = m.group(1)
        status = m.group(2)
        iso_key = (abs_path, iso_name)
        if iso_key not in cache:
            iso_success = status in ("PASS", "OK")
            iso_diags = [
                d
                for d in full_result.get("diagnostics", [])
                if iso_name in d.get("message", "") or iso_name in d.get("file", "")
            ]
            cache[iso_key] = CacheEntry(
                result={
                    "success": iso_success,
                    "diagnostics": iso_diags,
                    "diagnostic_count": len(iso_diags),
                    "error_summary": (
                        full_result.get("error_summary", "") if not iso_success else ""
                    ),
                    "duration_seconds": full_result.get("duration_seconds", 0),
                    "cached": False,
                    "isolate": iso_name,
                },
                file_mtime=get_file_mtime(abs_path),
                include_mtimes={},
            )
            evict_oldest(cache)


def get_cache_summary(cache: dict, max_size: int = CACHE_MAX_SIZE) -> dict[str, Any]:
    """Return verification cache summary for dashboard."""
    verified: list[str] = []
    failed: list[str] = []
    seen: set[str] = set()
    for key, entry in cache.items():
        path = key[0] if isinstance(key, tuple) else str(key)
        if path in seen:
            continue
        seen.add(path)
        if entry.result.get("success"):
            verified.append(path)
        else:
            failed.append(path)
    return {
        "verified_files": verified,
        "failed_files": failed,
        "cache_size": len(cache),
        "cache_max": max_size,
    }
