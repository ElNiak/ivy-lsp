"""Offline index builder for ``.ivy-index/`` directories.

Builds a pre-computed index for each protocol directory so that the LSP
server and MCP tools can start with a warm cache instead of re-parsing
every file on startup.

The build pipeline:

1. Discover ``.ivy`` files via :class:`IncludeResolver`.
2. Parse each file with :class:`TieredExtractor` (Tier 1 -> 2 -> 3).
3. Collect symbols, includes, exports, and requirements per file.
4. Build an :class:`IncludeGraph` from resolved includes.
5. Compute test scopes for files with exports.
6. Optionally build a :class:`SemanticModel` and
   :class:`ScopedRequirementModel`.
7. Write all artifacts to ``protocol_dir/.ivy-index/``.

CLI entry point: :func:`cli_index` (for ``ivy-lsp index``).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import ivy_lsp
from ivy_lsp.core.workspace.detection import detect_ivy_workspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUILDER_VERSION = ivy_lsp.__version__

# Tier labels — used in manifest entries and CLI reporting.
TIER_AST = "ast"
TIER_LEXER = "lexer"
TIER_REGEX = "regex"
TIER_UNKNOWN = "unknown"

# Completeness labels — used in manifest entries.
COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_MISSING = "missing"


def _file_sha256(filepath: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_from_bytes(data: bytes) -> str:
    """Compute SHA-256 hex digest from in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def _error_manifest_entry(
    mtime: float = 0.0,
    size: int = 0,
    sha256: str = "",
    completeness: str = COMPLETENESS_MISSING,
) -> dict:
    """Build a manifest entry for a file that failed reading or parsing."""
    return {
        "mtime": mtime,
        "size": size,
        "sha256": sha256,
        "completeness": completeness,
        "parse_tier": TIER_UNKNOWN,
    }


def _tier_label(tier: int) -> str:
    """Map tier number to a human-readable label."""
    return {1: TIER_AST, 2: TIER_LEXER, 3: TIER_REGEX}.get(tier, TIER_UNKNOWN)


# ---------------------------------------------------------------------------
# Worker process helpers (top-level for ProcessPoolExecutor picklability)
# ---------------------------------------------------------------------------


from ivy_lsp.infra.utils.process import worker_init as _worker_init


@dataclass
class FileExtractionResult:
    """Result of extracting a single .ivy file.

    All fields are plain Python objects so instances are picklable and can be
    transferred across process boundaries via :mod:`multiprocessing`.
    """

    rel_path: str
    symbols: list = field(default_factory=list)
    includes: list = field(default_factory=list)
    exports: dict = field(default_factory=dict)
    requirements: list = field(default_factory=list)
    manifest_entry: dict = field(default_factory=dict)
    tier_label: str = TIER_UNKNOWN
    tier1_errors: list = field(default_factory=list)
    sha256: str = ""
    error: Optional[str] = None


def _extract_one_file(
    filepath: str,
    protocol_dir: str,
    resolver_config: dict,
    fast: bool,
    parser_timeout: float,
    precomputed_sha: str = "",
) -> FileExtractionResult:
    """Extract symbols, includes, exports, requirements, and manifest data for one file.

    This function is intentionally defined at module level (not as a method) so
    that it can be pickled and dispatched to worker processes via
    :class:`concurrent.futures.ProcessPoolExecutor`.

    Args:
        filepath: Absolute path to the ``.ivy`` file to process.
        protocol_dir: Absolute path to the protocol directory (used to compute
            relative paths for the output maps).
        resolver_config: Serialised :class:`IncludeResolver` config dict produced
            by :meth:`IncludeResolver.to_config_dict`.  The resolver is
            reconstructed inside this function so no non-picklable objects are
            passed across the process boundary.
        fast: If ``True``, skip Tier 1 (AST parser) and use Tier 2/3 only.
        parser_timeout: Seconds to allow the parser lock to be acquired (Tier 1).
        precomputed_sha: If non-empty, reuse this SHA-256 instead of recomputing.

    Returns:
        :class:`FileExtractionResult` populated with all extracted data.
        If the file cannot be read, ``result.error`` is set and all other
        collection fields are empty.
    """
    from ivy_lsp.core.analysis.light_mode_extractor import (
        extract_exports_imports_light,
        extract_requirements_light,
    )
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
    from ivy_lsp.infra.utils.path_normalize import remap_node_id

    rel_path = os.path.relpath(filepath, protocol_dir)

    # Reconstruct the resolver from its serialised config
    resolver = IncludeResolver.from_config(resolver_config)

    # -- Read source ----------------------------------------------------------
    # Read raw bytes once: compute SHA from the binary representation (matching
    # _file_sha256 semantics), then decode for the parser.
    try:
        with open(filepath, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return FileExtractionResult(
            rel_path=rel_path,
            manifest_entry=_error_manifest_entry(),
            error=f"read error: {rel_path}: {exc}",
        )

    sha = precomputed_sha or _sha256_from_bytes(raw)
    source = raw.decode("utf-8", errors="replace")

    # -- Parse with TieredExtractor ------------------------------------------
    extractor = TieredExtractor(
        resolve_callback=resolver.resolve,
        parser_timeout=0.0 if fast else parser_timeout,
        skip_tier1=fast,
    )

    try:
        result = extractor.extract(source, filepath)
    except Exception as exc:
        try:
            stat = os.stat(filepath)
        except OSError:
            stat = None
        return FileExtractionResult(
            rel_path=rel_path,
            manifest_entry=_error_manifest_entry(
                mtime=stat.st_mtime if stat else 0.0,
                size=stat.st_size if stat else 0,
                sha256=sha,
                completeness=COMPLETENESS_PARTIAL,
            ),
            error=f"parse error: {rel_path}: {exc}",
        )

    # -- Symbols and includes ------------------------------------------------
    symbols = [s.to_dict() for s in result.symbols]
    includes = list(result.includes)

    # -- Exports / imports ---------------------------------------------------
    try:
        export_info = extract_exports_imports_light(source, filepath)
        exports = export_info.to_dict()
        export_error: Optional[str] = None
    except Exception as exc:
        from ivy_lsp.core.analysis.test_scope import ExportImportInfo

        export_info = ExportImportInfo(file=filepath)
        exports = export_info.to_dict()
        export_error = f"export error: {rel_path}: {exc}"

    # -- Requirements --------------------------------------------------------
    requirements: list = []
    req_error: Optional[str] = None
    try:
        reqs, _writes = extract_requirements_light(source, filepath)
        for r in reqs:
            r.file = rel_path
            r.id = remap_node_id(r.id, lambda _p: rel_path)
        requirements = [
            {
                "id": r.id,
                "kind": r.kind,
                "formula_text": r.formula_text,
                "line": r.line,
                "file": r.file,
                "monitor_action": r.monitor_action,
                "mixin_kind": r.mixin_kind,
            }
            for r in reqs
        ]
    except Exception as exc:
        req_error = f"requirement error: {rel_path}: {exc}"

    # -- Manifest entry ------------------------------------------------------
    completeness = COMPLETENESS_COMPLETE if not result.errors else COMPLETENESS_PARTIAL
    try:
        stat = os.stat(filepath)
    except OSError:
        stat = None
    tier = _tier_label(result.tier_used)
    manifest_entry = {
        "mtime": stat.st_mtime if stat else 0.0,
        "size": stat.st_size if stat else 0,
        "sha256": sha,
        "completeness": completeness,
        "parse_tier": tier,
    }

    # -- Tier-1 error details ------------------------------------------------
    tier1_errors = [
        {
            "file": rel_path,
            "error_type": e.error_type,
            "message": e.message,
        }
        for e in result.errors
        if e.tier == 1
    ]

    # Aggregate any soft errors into a single string
    soft_errors = [e for e in [export_error, req_error] if e is not None]
    combined_error: Optional[str] = "; ".join(soft_errors) if soft_errors else None

    return FileExtractionResult(
        rel_path=rel_path,
        symbols=symbols,
        includes=includes,
        exports=exports,
        requirements=requirements,
        manifest_entry=manifest_entry,
        tier_label=tier,
        tier1_errors=tier1_errors,
        sha256=sha,
        error=combined_error,
    )


# ---------------------------------------------------------------------------
# IndexBuilder
# ---------------------------------------------------------------------------


class IndexBuilder:
    """Offline index builder for ``.ivy-index/`` directories.

    Builds a complete pre-computed index for one or more protocol
    directories under a workspace root.
    """

    def __init__(
        self,
        workspace_root: str,
        workspace_config: Any,
        fast: bool = False,
        force: bool = False,
        workers: int = 1,
    ) -> None:
        """Initialize the builder.

        Args:
            workspace_root: Absolute path to workspace root
                (contains ``protocol-testing/``).
            workspace_config: :class:`WorkspaceConfig` from workspace
                detection.
            fast: If ``True``, use Tier 2 (lexer) only.  Default
                ``False`` (Tier 1 full parse).
            force: If ``True``, rebuild even if index appears fresh.
            workers: Number of parallel worker processes for file
                extraction.  Values < 1 are clamped to 1 (sequential).
        """
        self.workspace_root = os.path.abspath(workspace_root)
        self.workspace_config = workspace_config
        self.fast = fast
        self.force = force
        self.workers = max(1, workers)

    # -- Private helpers for build_protocol phases --------------------------

    def _create_protocol_resolver(self, protocol_dir: str) -> Optional[tuple]:
        """Create an IncludeResolver with staging for *protocol_dir*.

        Returns ``(resolver, ivy_files, layers)`` on success, or ``None``
        when no ``.ivy`` files are found after staging.
        """
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.workspace.detection import _apply_marker, _read_marker

        protocol_rel = os.path.relpath(protocol_dir, self.workspace_root)

        marker_path = os.path.join(protocol_dir, ".ivyworkspace")
        marker_data = _read_marker(marker_path)
        proto_config = None
        if marker_data is not None:
            proto_config = _apply_marker(marker_path, marker_data)

        if proto_config is not None:
            layers = proto_config.workspace_layers
            exclude = proto_config.exclude_paths
        else:
            layers = self.workspace_config.workspace_layers
            exclude = self.workspace_config.exclude_paths

        resolver = IncludeResolver(
            workspace_root=self.workspace_root,
            exclude_paths=exclude,
            include_paths=[protocol_rel],
            workspace_layers=layers,
        )
        try:
            resolver.create_staging_directory()
            if layers:
                resolver.build_layered_staging()
        except Exception:
            logger.warning(
                "Staging creation failed for %s; Tier 1 may not resolve all includes",
                os.path.basename(protocol_dir),
            )
        ivy_files = resolver.find_all_ivy_files(root=protocol_dir)
        if not ivy_files:
            return None
        return resolver, ivy_files, layers

    def _build_models(
        self,
        protocol_dir: str,
        protocol: str,
        resolver,
        ivy_files: List[str],
        requirements_map: Dict[str, list],
        scopes: Dict,
    ) -> tuple:
        """Build the optional SemanticModel and ScopedRequirementModel.

        Returns ``(semantic_model, requirement_graph)``; either may be ``None``.
        """
        semantic_model = None
        try:
            from ivy_lsp.core.semantic.model_builder import build_semantic_model

            def _find_files(root: str) -> List[str]:
                return [os.path.relpath(f, root) for f in ivy_files]

            semantic_model = build_semantic_model(
                root=protocol_dir,
                find_files_fn=_find_files,
                include_resolver=resolver.resolve,
            )
        except Exception as exc:
            logger.debug("Semantic model build failed for %s: %s", protocol, exc)

        requirement_graph = None
        try:
            from ivy_lsp.core.analysis.requirement_graph import RequirementNode
            from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel

            req_graph = ScopedRequirementModel()
            for rel_path, reqs_list in requirements_map.items():
                for req_dict in reqs_list:
                    try:
                        node = RequirementNode(
                            id=req_dict["id"],
                            kind=req_dict["kind"],
                            formula_text=req_dict["formula_text"],
                            line=req_dict["line"],
                            col=0,
                            file=req_dict["file"],
                            monitor_action=req_dict["monitor_action"],
                            mixin_kind=req_dict.get("mixin_kind", ""),
                        )
                        req_graph.add_requirement(node)
                    except Exception:
                        pass
            for scope in scopes.values():
                req_graph.register_test_scope(scope)
            requirement_graph = req_graph
        except Exception as exc:
            logger.debug("Requirement graph build failed for %s: %s", protocol, exc)

        return semantic_model, requirement_graph

    # -- Public API ---------------------------------------------------------

    def build_protocol(self, protocol_dir: str) -> dict:
        """Build ``.ivy-index/`` for a single protocol directory.

        Returns:
            Summary dict with protocol name, file count, test count,
            timing, and any errors encountered.
        """
        protocol_dir = os.path.abspath(protocol_dir)
        protocol = os.path.basename(protocol_dir)
        t0 = time.monotonic()
        errors: List[str] = []

        logger.info("Building index for protocol %s at %s", protocol, protocol_dir)

        # Skip protocols with no .ivy files before investing in staging
        if not glob.glob(os.path.join(protocol_dir, "**", "*.ivy"), recursive=True):
            logger.info("No .ivy files in %s, skipping", protocol_dir)
            return {
                "protocol": protocol,
                "files": 0,
                "tests": 0,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "status": "empty",
            }

        # -- 1. Discover .ivy files ----------------------------------------
        result = self._create_protocol_resolver(protocol_dir)
        if result is None:
            logger.warning("No .ivy files found in %s", protocol_dir)
            return {
                "protocol": protocol,
                "files": 0,
                "tests": 0,
                "elapsed_ms": 0.0,
                "status": "empty",
            }
        resolver, ivy_files, _layers = result

        logger.info("Found %d .ivy files for protocol %s", len(ivy_files), protocol)

        # -- 2-4. Parse files, collect artifacts ---------------------------
        from ivy_lsp.core.parsing.symbols import IncludeGraph

        manifest_files: Dict[str, dict] = {}
        symbols_map: Dict[str, list] = {}
        includes_raw: Dict[str, List[str]] = {}
        exports_map: Dict[str, dict] = {}
        requirements_map: Dict[str, list] = {}
        tier_counts: Dict[str, int] = {
            TIER_AST: 0,
            TIER_LEXER: 0,
            TIER_REGEX: 0,
            TIER_UNKNOWN: 0,
        }
        tier1_failures: List[Dict[str, str]] = []

        # Serialise the resolver once so _extract_one_file() can reconstruct it.
        # The staging_dir is included in the config dict so the worker process
        # can resolve cross-directory includes.
        resolver_config = resolver.to_config_dict()

        # -- Load existing index cache from .ivy-index/ --------------------
        # We load cached artifacts so that files whose SHA-256 hasn't changed
        # can skip re-parsing entirely (only SHA-256 computation is needed).
        # When --force is set, skip the cache entirely to ensure a full rebuild.
        index_dir_existing = os.path.join(protocol_dir, ".ivy-index")
        cached_manifest: Any = None
        cached_symbols: Any = None
        cached_includes_raw: Any = None
        cached_exports: Any = None
        cached_requirements: Any = None
        if not self.force:
            cached_manifest = self._load_json(
                os.path.join(index_dir_existing, "manifest.json")
            )
            cached_symbols = self._load_json(
                os.path.join(index_dir_existing, "symbols.json")
            )
            cached_includes_raw = self._load_json(
                os.path.join(index_dir_existing, "includes_raw.json")
            )
            cached_exports = self._load_json(
                os.path.join(index_dir_existing, "exports.json")
            )
            cached_requirements = self._load_json(
                os.path.join(index_dir_existing, "requirements.json")
            )

        # Build a sha256 lookup from the cached manifest: {rel_path -> sha256}
        cached_sha256: Dict[str, str] = {}
        if isinstance(cached_manifest, dict):
            for rel_p, entry in cached_manifest.get("files", {}).items():
                if isinstance(entry, dict) and entry.get("sha256"):
                    cached_sha256[rel_p] = entry["sha256"]

        cache_hits = 0
        cache_misses = 0
        files_to_extract: List[str] = []
        # Map filepath -> pre-computed SHA so _extract_one_file doesn't recompute
        sha_for_file: Dict[str, str] = {}

        # Validate all cache dicts once upfront instead of per-file isinstance checks
        caches_valid = all(
            isinstance(c, dict)
            for c in [
                cached_symbols,
                cached_includes_raw,
                cached_exports,
                cached_requirements,
                cached_manifest,
            ]
        )

        # -- Phase A: split files into cache hits vs cache misses ----------
        if self.force or not caches_valid:
            # --force or no usable cache: extract everything, skip SHA upfront
            # (_extract_one_file computes SHA from the raw bytes it reads).
            files_to_extract = list(ivy_files)
            cache_misses = len(files_to_extract)
        else:
            for filepath in ivy_files:
                rel_path = os.path.relpath(filepath, protocol_dir)

                try:
                    current_sha = _file_sha256(filepath)
                except OSError:
                    current_sha = ""

                cached_hit = (
                    current_sha
                    and current_sha == cached_sha256.get(rel_path)
                    and rel_path in cached_symbols
                    and rel_path in cached_includes_raw
                    and rel_path in cached_exports
                    and rel_path in cached_requirements
                    and rel_path in cached_manifest.get("files", {})
                )

                if cached_hit:
                    cache_hits += 1
                    symbols_map[rel_path] = cached_symbols[rel_path]
                    includes_raw[rel_path] = cached_includes_raw[rel_path]
                    exports_map[rel_path] = cached_exports[rel_path]
                    requirements_map[rel_path] = cached_requirements[rel_path]
                    manifest_files[rel_path] = cached_manifest["files"][rel_path]
                    cached_tier = cached_manifest["files"][rel_path].get(
                        "parse_tier", TIER_UNKNOWN
                    )
                    tier_counts[cached_tier] = tier_counts.get(cached_tier, 0) + 1
                else:
                    cache_misses += 1
                    files_to_extract.append(filepath)
                    sha_for_file[filepath] = current_sha

        # -- Phase B: extract cache misses (parallel or sequential) --------
        parser_timeout = 5.0

        if self.workers > 1 and len(files_to_extract) > 3:
            extraction_results = self._extract_parallel(
                files_to_extract=files_to_extract,
                protocol_dir=protocol_dir,
                resolver_config=resolver_config,
                fast=self.fast,
                parser_timeout=parser_timeout,
                sha_for_file=sha_for_file,
            )
        else:
            extraction_results = [
                _extract_one_file(
                    filepath=filepath,
                    protocol_dir=protocol_dir,
                    resolver_config=resolver_config,
                    fast=self.fast,
                    parser_timeout=parser_timeout,
                    precomputed_sha=sha_for_file.get(filepath, ""),
                )
                for filepath in files_to_extract
            ]

        # -- Phase C: integrate extraction results into maps ---------------
        for file_result in extraction_results:
            rel_path = file_result.rel_path

            if file_result.error and file_result.manifest_entry.get("completeness") in (
                COMPLETENESS_MISSING,
                None,
            ):
                # Unreadable file — only a manifest entry was produced
                logger.warning("Cannot read %s: %s", rel_path, file_result.error)
                manifest_files[rel_path] = file_result.manifest_entry
                errors.append(file_result.error)
                continue

            if file_result.error and not file_result.symbols:
                # Parse failure — manifest entry with parse-error metadata
                logger.warning(
                    "Extraction failed for %s: %s", rel_path, file_result.error
                )
                manifest_files[rel_path] = file_result.manifest_entry
                errors.append(file_result.error)
                continue

            # Happy path: populate all maps
            symbols_map[rel_path] = file_result.symbols
            includes_raw[rel_path] = file_result.includes
            exports_map[rel_path] = file_result.exports
            requirements_map[rel_path] = file_result.requirements
            manifest_files[rel_path] = file_result.manifest_entry
            tier_label = file_result.tier_label
            tier_counts[tier_label] = tier_counts.get(tier_label, 0) + 1
            tier1_failures.extend(file_result.tier1_errors)

            # Propagate soft errors (export / requirement extraction failures)
            if file_result.error:
                errors.append(file_result.error)

        # -- Log cache hit rate -------------------------------------------
        total_processed = cache_hits + cache_misses
        if total_processed > 0:
            hit_pct = 100.0 * cache_hits / total_processed
            logger.info(
                "Cache hit rate for %s: %d/%d files (%.1f%%) — %d parsed, %d from cache",
                protocol,
                cache_hits,
                total_processed,
                hit_pct,
                cache_misses,
                cache_hits,
            )

        # -- 5. Build IncludeGraph from resolved includes ------------------
        include_graph = IncludeGraph()

        # Map basenames to relative paths for resolution
        basename_to_rel: Dict[str, str] = {}
        for rel_path in manifest_files:
            stem = os.path.splitext(os.path.basename(rel_path))[0]
            basename_to_rel.setdefault(stem, rel_path)

        for rel_path, inc_names in includes_raw.items():
            for inc_name in inc_names:
                # Try to resolve to a relative path in this protocol
                target_rel = basename_to_rel.get(inc_name)
                if target_rel is not None:
                    include_graph.add_edge(rel_path, target_rel)

        # -- 6-7. Compute test scopes -------------------------------------
        from ivy_lsp.core.analysis.test_scope import (
            ExportImportInfo,
            TestScope,
            detect_test_role,
        )

        # Build export info map keyed by relative path
        file_exports: Dict[str, ExportImportInfo] = {}
        for rel_path, exp_dict in exports_map.items():
            try:
                file_exports[rel_path] = ExportImportInfo.from_dict(exp_dict)
            except Exception:
                pass

        scopes: Dict[str, TestScope] = {}
        for rel_path, info in file_exports.items():
            if not info.has_exports:
                continue

            closure = {rel_path}
            closure |= include_graph.get_transitive_includes(rel_path)

            all_exports: List[str] = []
            all_imports: List[str] = []
            for f in closure:
                f_info = file_exports.get(f)
                if f_info is not None:
                    all_exports.extend(f_info.exports)
                    all_imports.extend(f_info.imports)

            frozen_closure = frozenset(closure)
            scope = TestScope(
                test_file=rel_path,
                include_closure=frozen_closure,
                exported_actions=frozenset(all_exports),
                imported_actions=frozenset(all_imports),
                tester_role=detect_test_role(frozen_closure),
            )
            test_name = os.path.basename(rel_path).replace(".ivy", "")
            scopes[test_name] = scope

        # -- 8-9. Build optional SemanticModel and ScopedRequirementModel ---
        semantic_model, requirement_graph = self._build_models(
            protocol_dir,
            protocol,
            resolver,
            ivy_files,
            requirements_map,
            scopes,
        )

        # -- 10. Write output to .ivy-index/ --------------------------------
        index_dir = os.path.join(protocol_dir, ".ivy-index")
        os.makedirs(index_dir, exist_ok=True)

        # Build manifest
        default_tier = TIER_LEXER if self.fast else TIER_AST
        manifest = {
            "version": 1,
            "protocol": protocol,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "builder_version": _BUILDER_VERSION,
            "default_parse_tier": default_tier,
            "files": manifest_files,
        }

        self._write_json(os.path.join(index_dir, "manifest.json"), manifest)
        self._write_json(os.path.join(index_dir, "symbols.json"), symbols_map)
        self._write_json(
            os.path.join(index_dir, "includes.json"), include_graph.to_edges()
        )
        # includes_raw.json stores per-file raw include name lists for the
        # incremental cache.  This is separate from includes.json which stores
        # the resolved graph edges.
        self._write_json(os.path.join(index_dir, "includes_raw.json"), includes_raw)
        self._write_json(os.path.join(index_dir, "exports.json"), exports_map)
        self._write_json(os.path.join(index_dir, "requirements.json"), requirements_map)

        # Scopes
        scopes_dir = os.path.join(index_dir, "scopes")
        os.makedirs(scopes_dir, exist_ok=True)

        meta_entries: List[dict] = []
        for test_name, scope in sorted(scopes.items()):
            scope_dict = scope.to_dict()
            self._write_json(os.path.join(scopes_dir, f"{test_name}.json"), scope_dict)
            meta_entries.append(scope_dict)

        self._write_json(os.path.join(scopes_dir, "_meta.json"), meta_entries)

        # Optional pickle artifacts
        if semantic_model is not None:
            self._write_pickle(index_dir, "semantic_model.pickle.gz", semantic_model)
        if requirement_graph is not None:
            self._write_pickle(
                index_dir, "requirement_graph.pickle.gz", requirement_graph
            )

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "Index built for %s: %d files, %d tests, %.1fms",
            protocol,
            len(ivy_files),
            len(scopes),
            elapsed,
        )

        # Summarize tier-1 failures by category for actionable output
        failure_summary: List[Dict[str, Any]] = []
        if tier1_failures:
            # Group by error message to deduplicate
            from collections import Counter

            msg_counts: Counter = Counter()
            msg_files: Dict[str, List[str]] = {}
            for f in tier1_failures:
                msg = f["message"]
                msg_counts[msg] += 1
                msg_files.setdefault(msg, []).append(f["file"])
            for msg, count in msg_counts.most_common():
                files_sample = msg_files[msg][:3]
                failure_summary.append(
                    {
                        "reason": msg,
                        "count": count,
                        "files_sample": files_sample,
                    }
                )

        # Remove zero-count tiers for cleaner output
        tier_counts_clean = {k: v for k, v in tier_counts.items() if v > 0}

        return {
            "protocol": protocol,
            "files": len(ivy_files),
            "tests": len(scopes),
            "elapsed_ms": round(elapsed, 1),
            "status": "ok",
            "errors": errors if errors else None,
            "parse_tiers": tier_counts_clean,
            "tier1_failures": failure_summary if failure_summary else None,
        }

    def _extract_parallel(
        self,
        files_to_extract: List[str],
        protocol_dir: str,
        resolver_config: dict,
        fast: bool,
        parser_timeout: float,
        sha_for_file: Optional[Dict[str, str]] = None,
    ) -> List[FileExtractionResult]:
        """Extract multiple files in parallel using a process pool.

        Args:
            files_to_extract: Absolute paths to ``.ivy`` files.
            protocol_dir: Protocol directory for relative path computation.
            resolver_config: Serialised resolver config (picklable).
            fast: If ``True``, skip Tier 1 (AST parser) and use Tier 2/3 only.
            parser_timeout: Timeout for the Tier 1 parser lock.
            sha_for_file: Pre-computed SHA-256 per filepath (avoids double hash).

        Returns:
            List of :class:`FileExtractionResult`, one per input file.
            Failed workers produce results with ``error`` set.
        """
        import concurrent.futures

        sha_map = sha_for_file or {}
        results: List[FileExtractionResult] = []

        parent_path = list(sys.path)

        try:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_worker_init,
                initargs=(parent_path,),
            )
        except (PermissionError, OSError) as exc:
            logger.warning(
                "ProcessPoolExecutor unavailable (%s), falling back to sequential",
                exc,
            )
            return [
                _extract_one_file(
                    fp,
                    protocol_dir,
                    resolver_config,
                    fast,
                    parser_timeout,
                    precomputed_sha=sha_map.get(fp, ""),
                )
                for fp in files_to_extract
            ]

        with executor:
            future_to_path = {}
            for filepath in files_to_extract:
                future = executor.submit(
                    _extract_one_file,
                    filepath=filepath,
                    protocol_dir=protocol_dir,
                    resolver_config=resolver_config,
                    fast=fast,
                    parser_timeout=parser_timeout,
                    precomputed_sha=sha_map.get(filepath, ""),
                )
                future_to_path[future] = filepath

            for future in concurrent.futures.as_completed(future_to_path):
                filepath = future_to_path[future]
                rel_path = os.path.relpath(filepath, protocol_dir)
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    logger.warning("Worker failed for %s: %s", rel_path, exc)
                    # Produce a minimal error result so the caller can
                    # record the failure in the manifest.
                    results.append(
                        FileExtractionResult(
                            rel_path=rel_path,
                            manifest_entry=_error_manifest_entry(),
                            error=f"worker error: {rel_path}: {exc}",
                        )
                    )

        return results

    def build_all(self) -> List[dict]:
        """Glob ``protocol-testing/*/``, build each.

        Returns:
            List of summary dicts (one per protocol).
        """
        pattern = os.path.join(self.workspace_root, "protocol-testing", "*")
        summaries: List[dict] = []
        for candidate in sorted(glob.glob(pattern)):
            if not os.path.isdir(candidate):
                continue
            # Skip hidden directories
            if os.path.basename(candidate).startswith("."):
                continue
            protocol = os.path.basename(candidate)

            if not self.force:
                status = self.check_status(candidate)
                if status["status"] == "fresh":
                    logger.info(
                        "Skipping %s: index is fresh (%d files)",
                        protocol,
                        status["total_files"],
                    )
                    summaries.append(
                        {
                            "protocol": protocol,
                            "files": status["total_files"],
                            "tests": 0,
                            "elapsed_ms": 0.0,
                            "status": "skipped_fresh",
                        }
                    )
                    continue

            summary = self.build_protocol(candidate)
            summaries.append(summary)

        return summaries

    def check_status(self, protocol_dir: str) -> dict:
        """Check staleness of existing index without rebuilding.

        Returns:
            Dict with protocol, status, changed_files, total_files.
        """
        protocol_dir = os.path.abspath(protocol_dir)
        protocol = os.path.basename(protocol_dir)
        manifest_path = os.path.join(protocol_dir, ".ivy-index", "manifest.json")

        if not os.path.isfile(manifest_path):
            return {
                "protocol": protocol,
                "status": "stale_major",
                "changed_files": 0,
                "total_files": 0,
            }

        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {
                "protocol": protocol,
                "status": "stale_major",
                "changed_files": 0,
                "total_files": 0,
            }

        files_meta = manifest.get("files", {})
        if not files_meta:
            return {
                "protocol": protocol,
                "status": "stale_major",
                "changed_files": 0,
                "total_files": 0,
            }

        total = len(files_meta)
        changed = 0

        for rel_path, meta in files_meta.items():
            expected_mtime = meta.get("mtime") if isinstance(meta, dict) else None
            if expected_mtime is None:
                changed += 1
                continue

            abs_path = os.path.join(protocol_dir, rel_path)
            try:
                actual_mtime = os.path.getmtime(abs_path)
            except OSError:
                changed += 1
                continue

            if abs(actual_mtime - expected_mtime) > 1.0:
                changed += 1

        if changed == 0:
            status = "fresh"
        elif total > 0 and (changed / total) < 0.10:
            status = "stale_minor"
        else:
            status = "stale_major"

        return {
            "protocol": protocol,
            "status": status,
            "changed_files": changed,
            "total_files": total,
        }

    # -- Private helpers ----------------------------------------------------

    @staticmethod
    def _load_json(path: str) -> Any:
        """Load JSON from *path*, returning ``None`` on any error."""
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        """Write data as indented JSON."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    @staticmethod
    def _write_pickle(index_dir: str, filename: str, obj: Any) -> None:
        """Write a gzipped pickle with file locking."""
        from ivy_lsp.infra.utils.serialization import write_locked_pickle

        write_locked_pickle(index_dir, filename, obj, logger)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def cli_index(args: list) -> int:
    """CLI entry point for ``ivy-lsp index``.

    Parse args:
        - First positional arg: protocol dir (or ``--all`` for all)
        - ``--fast``: use Tier 2 only
        - ``--force``: rebuild even if fresh
        - ``--status``: check staleness without rebuilding

    Returns:
        0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        prog="ivy-lsp index",
        description="Build .ivy-index/ for protocol directories.",
    )
    parser.add_argument(
        "protocol_dir",
        nargs="?",
        default=None,
        help="Path to a protocol directory. Omit with --all to index all protocols.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="build_all",
        help="Build index for all protocols under protocol-testing/.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Tier 2 (lexer) only, skip full parser.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if index appears fresh.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check staleness without rebuilding.",
    )
    parser.add_argument(
        "--workers",
        "-j",
        type=int,
        default=1,
        help="Number of parallel worker processes for file extraction (default: 1).",
    )

    parsed = parser.parse_args(args)

    if not parsed.protocol_dir and not parsed.build_all:
        parser.error("Provide a protocol directory or use --all.")
        return 1  # pragma: no cover

    # Detect workspace
    if parsed.protocol_dir:
        start_dir = os.path.abspath(parsed.protocol_dir)
    else:
        start_dir = os.getcwd()

    ws_config = detect_ivy_workspace(start_dir)

    builder = IndexBuilder(
        workspace_root=ws_config.workspace_root,
        workspace_config=ws_config,
        fast=parsed.fast,
        force=parsed.force,
        workers=parsed.workers,
    )

    try:
        if parsed.status:
            if parsed.build_all:
                pattern = os.path.join(
                    ws_config.workspace_root, "protocol-testing", "*"
                )
                for candidate in sorted(glob.glob(pattern)):
                    if os.path.isdir(candidate) and not os.path.basename(
                        candidate
                    ).startswith("."):
                        result = builder.check_status(candidate)
                        print(json.dumps(result, indent=2))
            elif parsed.protocol_dir:
                result = builder.check_status(parsed.protocol_dir)
                print(json.dumps(result, indent=2))
            return 0

        if parsed.build_all:
            summaries = builder.build_all()
        else:
            summaries = [builder.build_protocol(parsed.protocol_dir)]

        for s in summaries:
            print(json.dumps(s, indent=2))

        # Return 1 if any protocol had a non-ok status (excluding skipped)
        if any(
            s.get("status") not in ("ok", "skipped_fresh", "empty") for s in summaries
        ):
            return 1
        return 0

    except Exception as exc:
        logger.error("Index build failed: %s", exc, exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
