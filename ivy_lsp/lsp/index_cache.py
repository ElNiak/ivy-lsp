"""Offline index cache validation: load artifacts, classify files as hits vs misses."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ivy_lsp.infra.utils.hashing import file_sha256 as _file_sha256

logger = logging.getLogger(__name__)


@dataclass
class CachedIndex:
    """Loaded artifacts from a previous .ivy-index/ build."""

    manifest: dict | None = None
    symbols: dict | None = None
    includes_raw: dict | None = None
    exports: dict | None = None
    requirements: dict | None = None


def _load_json(path: str) -> Any:
    """Load JSON from *path*, returning ``None`` on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def load_cached_index(index_dir: str) -> CachedIndex:
    """Load all cached index artifacts from *index_dir*."""
    return CachedIndex(
        manifest=_load_json(os.path.join(index_dir, "manifest.json")),
        symbols=_load_json(os.path.join(index_dir, "symbols.json")),
        includes_raw=_load_json(os.path.join(index_dir, "includes_raw.json")),
        exports=_load_json(os.path.join(index_dir, "exports.json")),
        requirements=_load_json(os.path.join(index_dir, "requirements.json")),
    )


@dataclass
class ClassifyResult:
    """Result of classifying files into cache hits vs extraction targets."""

    symbols_map: Dict[str, list] = field(default_factory=dict)
    includes_raw: Dict[str, List[str]] = field(default_factory=dict)
    exports_map: Dict[str, dict] = field(default_factory=dict)
    requirements_map: Dict[str, list] = field(default_factory=dict)
    manifest_files: Dict[str, dict] = field(default_factory=dict)
    tier_counts: Dict[str, int] = field(default_factory=dict)
    files_to_extract: List[str] = field(default_factory=list)
    sha_for_file: Dict[str, str] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0


def classify_files(
    ivy_files: List[str],
    protocol_dir: str,
    cached: CachedIndex,
    force: bool,
    tier_labels: Dict[str, str] | None = None,
) -> ClassifyResult:
    """Split *ivy_files* into cache hits (populate result maps) and extraction targets.

    Args:
        ivy_files: Absolute paths to all .ivy files to process.
        protocol_dir: Absolute path to the protocol directory.
        cached: Previously loaded index artifacts.
        force: If True, skip cache entirely.
        tier_labels: Mapping of tier label constants for tier counting.

    Returns:
        ClassifyResult with populated maps for cache hits and lists for misses.
    """
    from ivy_lsp.lsp.index_builder import TIER_UNKNOWN

    result = ClassifyResult()
    if tier_labels:
        result.tier_counts = {k: 0 for k in tier_labels.values()}
    else:
        result.tier_counts = {}

    caches_valid = all(
        isinstance(c, dict)
        for c in [
            cached.symbols,
            cached.includes_raw,
            cached.exports,
            cached.requirements,
            cached.manifest,
        ]
    )

    if force or not caches_valid:
        result.files_to_extract = list(ivy_files)
        result.cache_misses = len(ivy_files)
        return result

    cached_sha256: Dict[str, str] = {}
    if isinstance(cached.manifest, dict):
        for rel_p, entry in cached.manifest.get("files", {}).items():
            if isinstance(entry, dict) and entry.get("sha256"):
                cached_sha256[rel_p] = entry["sha256"]

    for filepath in ivy_files:
        rel_path = os.path.relpath(filepath, protocol_dir)

        try:
            current_sha = _file_sha256(filepath)
        except OSError:
            current_sha = ""

        cached_hit = (
            current_sha
            and current_sha == cached_sha256.get(rel_path)
            and rel_path in cached.symbols
            and rel_path in cached.includes_raw
            and rel_path in cached.exports
            and rel_path in cached.requirements
            and rel_path in cached.manifest.get("files", {})
        )

        if cached_hit:
            result.cache_hits += 1
            result.symbols_map[rel_path] = cached.symbols[rel_path]
            result.includes_raw[rel_path] = cached.includes_raw[rel_path]
            result.exports_map[rel_path] = cached.exports[rel_path]
            result.requirements_map[rel_path] = cached.requirements[rel_path]
            result.manifest_files[rel_path] = cached.manifest["files"][rel_path]
            cached_tier = cached.manifest["files"][rel_path].get(
                "parse_tier", TIER_UNKNOWN
            )
            result.tier_counts[cached_tier] = result.tier_counts.get(cached_tier, 0) + 1
        else:
            result.cache_misses += 1
            result.files_to_extract.append(filepath)
            result.sha_for_file[filepath] = current_sha

    return result
