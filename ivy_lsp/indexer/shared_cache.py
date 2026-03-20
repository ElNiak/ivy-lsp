"""Shared disk cache for SemanticModel and RequirementGraph.

Allows the LSP process (eager indexing) to write its built model to disk,
and the MCP process (lazy model build) to read it instead of rebuilding
from scratch — saving 30-40s of duplicate startup time.

Cache location: ``~/.cache/ivy-lsp/<workspace-hash>/``

Freshness key: SHA-256 of sorted .ivy file paths + max mtime.
If any .ivy file is added, removed, or modified → cache is stale.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import os
import pickle
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple

if TYPE_CHECKING:
    from ivy_lsp.analysis.requirement_graph import RequirementGraph
    from ivy_lsp.semantic.model import SemanticModel

logger = logging.getLogger(__name__)

_CACHE_BASE = Path.home() / ".cache" / "ivy-lsp"
_META_FILE = "cache_meta.json"
_MODEL_FILE = "semantic_model.pickle.gz"
_GRAPH_FILE = "requirement_graph.pickle.gz"
_LOCK_FILE = ".cache.lock"
_MAX_AGE_SECONDS = 3600  # 1-hour TTL fallback


def _workspace_hash(root: str) -> str:
    """Deterministic hash of the workspace root path."""
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]


def compute_freshness_key(root: str, ivy_files: list[str]) -> str:
    """Compute a freshness key from sorted file paths and max mtime.

    Args:
        root: Workspace root directory.
        ivy_files: List of .ivy file paths (absolute or relative).

    Returns:
        A hex digest that changes when any file is added/removed/modified.
    """
    sorted_files = sorted(ivy_files)
    max_mtime = 0.0
    for f in sorted_files:
        try:
            max_mtime = max(max_mtime, os.path.getmtime(f))
        except OSError:
            continue
    payload = "\n".join(sorted_files) + f"\n__mtime__:{max_mtime}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _cache_dir(root: str) -> Path:
    """Return the cache directory for a given workspace root."""
    return _CACHE_BASE / _workspace_hash(root)


def write_model_cache(
    root: str,
    semantic_model: SemanticModel,
    requirement_graph: Optional[RequirementGraph],
    freshness_key: str,
) -> bool:
    """Serialize and write SemanticModel + RequirementGraph to disk cache.

    Uses file locking to prevent concurrent writes. If the lock is held
    by another process, this call is silently skipped.

    Returns:
        True if the cache was written, False if skipped.
    """
    cache = _cache_dir(root)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Cannot create cache directory: %s", cache)
        return False

    lock_path = cache / _LOCK_FILE
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        logger.debug("Cache lock held by another process, skipping write")
        return False

    try:
        t0 = time.monotonic()

        # Write semantic model
        with gzip.open(cache / _MODEL_FILE, "wb") as f:
            pickle.dump(semantic_model, f, protocol=pickle.HIGHEST_PROTOCOL)

        # Write requirement graph (may be None)
        if requirement_graph is not None:
            with gzip.open(cache / _GRAPH_FILE, "wb") as f:
                pickle.dump(requirement_graph, f, protocol=pickle.HIGHEST_PROTOCOL)
        else:
            # Remove stale graph file if graph is None
            (cache / _GRAPH_FILE).unlink(missing_ok=True)

        # Write metadata
        meta = {
            "workspace_root": os.path.realpath(root),
            "freshness_key": freshness_key,
            "timestamp": time.time(),
            "model_size": (cache / _MODEL_FILE).stat().st_size,
        }
        if requirement_graph is not None:
            meta["graph_size"] = (cache / _GRAPH_FILE).stat().st_size

        with open(cache / _META_FILE, "w") as f:
            json.dump(meta, f, indent=2)

        elapsed = time.monotonic() - t0
        logger.info(
            "Writing shared cache: model=%dKB graph=%sKB (%.1fs) → %s",
            meta["model_size"] // 1024,
            meta.get("graph_size", 0) // 1024 if "graph_size" in meta else "N/A",
            elapsed,
            cache,
        )
        return True

    except Exception:
        logger.warning("Failed to write shared cache", exc_info=True)
        # Clean up partial writes
        for fname in (_MODEL_FILE, _GRAPH_FILE, _META_FILE):
            (cache / fname).unlink(missing_ok=True)
        return False
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def read_model_cache(
    root: str, freshness_key: str
) -> Tuple[Optional[Any], Optional[Any]]:
    """Load SemanticModel and RequirementGraph from disk cache if fresh.

    Args:
        root: Workspace root directory.
        freshness_key: Current freshness key to compare against cached.

    Returns:
        (SemanticModel, RequirementGraph) if cache is fresh, else (None, None).
    """
    cache = _cache_dir(root)
    meta_path = cache / _META_FILE

    if not meta_path.exists():
        logger.debug("No shared cache found at %s", cache)
        return None, None

    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.debug("Cannot read cache metadata at %s", meta_path)
        return None, None

    # Check freshness
    if meta.get("freshness_key") != freshness_key:
        logger.info("Cache stale (key mismatch), will rebuild")
        return None, None

    # Check TTL
    age = time.time() - meta.get("timestamp", 0)
    if age > _MAX_AGE_SECONDS:
        logger.info("Cache expired (age=%.0fs > %ds)", age, _MAX_AGE_SECONDS)
        return None, None

    # Load model
    t0 = time.monotonic()
    model_path = cache / _MODEL_FILE
    graph_path = cache / _GRAPH_FILE

    if not model_path.exists():
        logger.debug("Cache model file missing")
        return None, None

    try:
        with gzip.open(model_path, "rb") as f:
            semantic_model = pickle.load(f)  # noqa: S301

        requirement_graph = None
        if graph_path.exists():
            with gzip.open(graph_path, "rb") as f:
                requirement_graph = pickle.load(f)  # noqa: S301

        elapsed = time.monotonic() - t0
        logger.info(
            "Loaded semantic model from shared cache (%.2fs) ← %s", elapsed, cache
        )
        return semantic_model, requirement_graph

    except Exception:
        logger.warning("Failed to load shared cache", exc_info=True)
        return None, None
