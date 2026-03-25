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
import fcntl
import glob
import gzip
import hashlib
import json
import logging
import os
import pickle
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import ivy_lsp
from ivy_lsp.core.workspace.detection import detect_ivy_workspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUILDER_VERSION = ivy_lsp.__version__


def _file_sha256(filepath: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _tier_label(tier: int) -> str:
    """Map tier number to a human-readable label."""
    return {1: "ast", 2: "lexer", 3: "regex"}.get(tier, "unknown")


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
        """
        self.workspace_root = os.path.abspath(workspace_root)
        self.workspace_config = workspace_config
        self.fast = fast
        self.force = force

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

        # -- 1. Discover .ivy files ----------------------------------------
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        protocol_rel = os.path.relpath(protocol_dir, self.workspace_root)
        resolver = IncludeResolver(
            workspace_root=self.workspace_root,
            exclude_paths=self.workspace_config.exclude_paths,
            include_paths=[protocol_rel],
            workspace_layers=self.workspace_config.workspace_layers,
        )
        # Create staging so resolver.resolve() can find cross-directory includes
        try:
            resolver.create_staging_directory()
            if self.workspace_config.workspace_layers:
                resolver.build_layered_staging()
        except Exception:
            logger.warning(
                "Staging creation failed for %s; Tier 1 may not resolve all includes",
                protocol,
            )
        ivy_files = resolver.find_all_ivy_files(root=protocol_dir)
        if not ivy_files:
            logger.warning("No .ivy files found in %s", protocol_dir)
            return {
                "protocol": protocol,
                "files": 0,
                "tests": 0,
                "elapsed_ms": 0.0,
                "status": "empty",
            }

        logger.info("Found %d .ivy files for protocol %s", len(ivy_files), protocol)

        # -- 2-4. Parse files, collect artifacts ---------------------------
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_exports_imports_light,
            extract_requirements_light,
        )
        from ivy_lsp.core.parsing.symbols import IncludeGraph
        from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor

        extractor = TieredExtractor(
            resolve_callback=resolver.resolve,
            parser_timeout=0.0 if self.fast else 5.0,
        )
        if self.fast:
            # Skip Tier 1 (parser) entirely in fast mode
            extractor._parser_available = False

        manifest_files: Dict[str, dict] = {}
        symbols_map: Dict[str, list] = {}
        includes_raw: Dict[str, List[str]] = {}
        exports_map: Dict[str, dict] = {}
        requirements_map: Dict[str, list] = {}
        tier_counts: Dict[str, int] = {"ast": 0, "lexer": 0, "regex": 0, "unknown": 0}
        tier1_failures: List[Dict[str, str]] = []

        for filepath in ivy_files:
            rel_path = os.path.relpath(filepath, protocol_dir)
            try:
                source = self._read_file(filepath)
            except OSError as exc:
                logger.warning("Cannot read %s: %s", filepath, exc)
                manifest_files[rel_path] = self._manifest_entry_missing(filepath)
                errors.append(f"read error: {rel_path}: {exc}")
                continue

            # Parse with TieredExtractor
            try:
                result = extractor.extract(source, filepath)
            except Exception as exc:
                logger.warning("Extraction failed for %s: %s", filepath, exc)
                manifest_files[rel_path] = self._manifest_entry_error(filepath)
                errors.append(f"parse error: {rel_path}: {exc}")
                continue

            # Symbols
            symbols_map[rel_path] = [s.to_dict() for s in result.symbols]

            # Includes (raw names)
            includes_raw[rel_path] = list(result.includes)

            # Exports/imports
            try:
                export_info = extract_exports_imports_light(source, filepath)
                exports_map[rel_path] = export_info.to_dict()
            except Exception as exc:
                logger.debug("Export extraction failed for %s: %s", filepath, exc)
                from ivy_lsp.core.analysis.test_scope import ExportImportInfo

                export_info = ExportImportInfo(file=filepath)
                exports_map[rel_path] = export_info.to_dict()
                errors.append(f"export error: {rel_path}: {exc}")

            # Requirements
            try:
                reqs, _writes = extract_requirements_light(source, filepath)
                requirements_map[rel_path] = [
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
                logger.debug("Requirement extraction failed for %s: %s", filepath, exc)
                requirements_map[rel_path] = []
                errors.append(f"requirement error: {rel_path}: {exc}")

            # Manifest entry
            completeness = "complete" if not result.errors else "partial"
            try:
                stat = os.stat(filepath)
                sha = _file_sha256(filepath)
            except OSError:
                stat = None
                sha = ""
            tier_label = _tier_label(result.tier_used)
            manifest_files[rel_path] = {
                "mtime": stat.st_mtime if stat else 0.0,
                "size": stat.st_size if stat else 0,
                "sha256": sha,
                "completeness": completeness,
                "parse_tier": tier_label,
            }
            tier_counts[tier_label] = tier_counts.get(tier_label, 0) + 1

            # Collect tier-1 failure details for CLI reporting
            for tier_err in result.errors:
                if tier_err.tier == 1:
                    tier1_failures.append(
                        {
                            "file": rel_path,
                            "error_type": tier_err.error_type,
                            "message": tier_err.message,
                        }
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

        # -- 8. Optional: SemanticModel ------------------------------------
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

        # -- 9. Optional: ScopedRequirementModel ---------------------------
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

        # -- 10. Write output to .ivy-index/ --------------------------------
        index_dir = os.path.join(protocol_dir, ".ivy-index")
        os.makedirs(index_dir, exist_ok=True)

        # Build manifest
        default_tier = "lexer" if self.fast else "ast"
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
    def _read_file(filepath: str) -> str:
        """Read a file as UTF-8 text with error replacement."""
        with open(filepath, encoding="utf-8", errors="replace") as f:
            return f.read()

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        """Write data as indented JSON."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    @staticmethod
    def _write_pickle(index_dir: str, filename: str, obj: Any) -> None:
        """Write a gzipped pickle with file locking."""
        lock_path = os.path.join(index_dir, ".build.lock")
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out_path = os.path.join(index_dir, filename)
            with gzip.open(out_path, "wb") as f:
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
        except BlockingIOError:
            logger.warning(
                "Could not acquire lock for %s, skipping pickle write",
                filename,
            )
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fd.close()

    @staticmethod
    def _manifest_entry_missing(filepath: str) -> dict:
        """Manifest entry for a file that could not be read."""
        return {
            "mtime": 0.0,
            "size": 0,
            "sha256": "",
            "completeness": "missing",
            "parse_tier": "unknown",
        }

    @staticmethod
    def _manifest_entry_error(filepath: str) -> dict:
        """Manifest entry for a file that failed parsing."""
        try:
            stat = os.stat(filepath)
            return {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "sha256": _file_sha256(filepath),
                "completeness": "partial",
                "parse_tier": "unknown",
            }
        except OSError:
            return {
                "mtime": 0.0,
                "size": 0,
                "sha256": "",
                "completeness": "missing",
                "parse_tier": "unknown",
            }


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
