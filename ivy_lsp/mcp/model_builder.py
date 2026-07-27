"""Standalone model-building functions extracted from McpServerState.

These functions are pure in the sense that all dependencies are passed as
explicit arguments rather than read from ``self``.  ``McpServerState``
delegates to these after Task 10.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from ivy_lsp.infra.utils.name_utils import get_last_component

logger = logging.getLogger(__name__)


def build_mcp_model(
    workspace_context: Any,
    root: str,
    include_paths: list[str],
    exclude_dirs: frozenset[str],
    resolver: Any,
    find_ivy_files_fn: Any,
    *,
    resolve_callback: Any = None,
    stdlib_modules: frozenset[str] = frozenset(),
    write_model_fn: Callable[[Any], None] | None = None,
) -> Any:
    """Build a lightweight semantic model from workspace files.

    Tries two strategies in order:
    1. Offline index merge (per-protocol models from .ivy-index/)
    2. Full rebuild via TieredExtractor

    After a successful build, calls *write_model_fn* (if provided) so the
    caller can persist the model back to .ivy-index/.

    Args:
        workspace_context: Loaded WorkspaceContext, or None.
        root: Workspace root directory.
        include_paths: Extra include paths for the resolver.
        exclude_dirs: Directory names to exclude when scanning.
        resolver: Include resolver instance, or None.
        find_ivy_files_fn: Callable used for cached file discovery.
        resolve_callback: Fallback resolve callable when *resolver* is None.
        stdlib_modules: Set of known stdlib module names.
        write_model_fn: Optional callback ``(model) -> None`` called after a
            successful full rebuild so the caller can persist the model.

    Returns:
        A SemanticModel instance, or None if the build failed entirely.
    """
    # --- Strategy 1: Merge per-protocol models from offline index ---
    try:
        if workspace_context is not None and workspace_context.has_index():
            from ivy_lsp import __version__
            from ivy_lsp.core.semantic.model import SemanticModel

            merged = SemanticModel()
            used_protos: list[str] = []
            skipped_protos: list[str] = []
            for proto, idx in workspace_context.protocol_indexes.items():
                # Version fingerprint: reject index built by different ivy-lsp
                manifest_version = idx.manifest.get("builder_version")
                if manifest_version and manifest_version != __version__:
                    skipped_protos.append(f"{proto}(version mismatch)")
                    continue
                if idx.semantic_model is None:
                    skipped_protos.append(f"{proto}(no model)")
                    continue
                if idx.staleness.status not in ("fresh", "stale_minor"):
                    skipped_protos.append(f"{proto}({idx.staleness.status})")
                    continue
                merged.merge_from(idx.semantic_model)
                used_protos.append(proto)

            if used_protos and merged.node_count() > 0 and not skipped_protos:
                logger.info(
                    "Loaded semantic model from offline index: " "%d nodes from %s",
                    merged.node_count(),
                    ", ".join(used_protos),
                )
                return merged
            elif skipped_protos:
                logger.info(
                    "Offline index incomplete, falling back to full build "
                    "(skipped: %s)",
                    ", ".join(skipped_protos),
                )
    except Exception:
        logger.debug(
            "Offline index merge failed, falling back to full build",
            exc_info=True,
        )

    # --- Strategy 2: Full rebuild from scratch ---
    from ivy_lsp.core.semantic.model_builder import build_semantic_model

    model = build_semantic_model(
        root=root,
        find_files_fn=find_ivy_files_fn,
        include_resolver=(resolver.resolve if resolver else resolve_callback),
        stdlib_modules=stdlib_modules,
    )

    # Write rebuilt model to .ivy-index/ so next startup uses Strategy 1
    if model is not None and write_model_fn is not None:
        write_model_fn(model)

    return model


def build_requirement_graph(
    root: str,
    ivy_files: list[str],
    resolver: Any,
    include_paths: list[str],
    exclude_dirs: frozenset[str],
    enrichment_adapter: Any = None,
    *,
    workspace_context: Any = None,
    find_ivy_files_cached_fn: Any = None,
    populate_semantic_model_fn: Callable[[Any], None] | None = None,
) -> Any:
    """Build a RequirementGraph from workspace .ivy files.

    Tries the offline index first (per-protocol requirement graphs
    from .ivy-index/), then falls back to a full build using the
    light-mode extractor.

    Args:
        root: Workspace root directory.
        ivy_files: Pre-discovered list of .ivy file paths (relative to root).
        resolver: Include resolver instance, or None.
        include_paths: Extra include paths for the resolver.
        exclude_dirs: Directory names to exclude when scanning.
        enrichment_adapter: Optional adapter for enriching the graph.
        workspace_context: Loaded WorkspaceContext, or None.
        find_ivy_files_cached_fn: Callable for cached file discovery; used
            instead of *ivy_files* when provided (matches original behaviour).
        populate_semantic_model_fn: Optional callback ``(graph) -> None``
            called after a successful build to mirror graph data into the
            SemanticModel.

    Returns:
        A RequirementGraph instance, or None if the build failed.
    """
    # Try offline index before doing the expensive build
    try:
        if workspace_context is not None and workspace_context.has_index():
            for _proto, idx in workspace_context.protocol_indexes.items():
                if idx.requirement_graph is not None:
                    logger.info("Loaded requirement graph from offline index")
                    return idx.requirement_graph
    except Exception:
        logger.debug(
            "Offline index lookup failed for requirement graph",
            exc_info=True,
        )

    try:
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_requirements_light,
        )
        from ivy_lsp.core.analysis.requirement_graph import (
            ActionNode,
            RequirementGraph,
            StateVarNode,
        )

        t0 = time.monotonic()
        graph = RequirementGraph()
        all_writes: list[tuple[str, str, int]] = []
        known_vars: set[str] = set()

        discovered = (
            find_ivy_files_cached_fn(root)
            if find_ivy_files_cached_fn is not None
            else ivy_files
        )
        logger.info(
            "Requirement graph: discovered %d .ivy files "
            "(root=%s, include_paths=%s)",
            len(discovered),
            root,
            include_paths or "(all)",
        )
        if not discovered:
            logger.warning(
                "Requirement graph: no .ivy files found — graph will be empty. "
                "Check workspace root and include_paths."
            )
            return None

        files_scanned = 0
        for rel_path in discovered:
            abs_path = os.path.join(root, rel_path)
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
                continue

            files_scanned += 1
            reqs, writes = extract_requirements_light(source, abs_path)
            if not reqs and not writes:
                continue

            # Bulk-add requirements + CONSTRAINS edges for this file
            graph.add_file_requirements(abs_path, reqs, writes)

            # Collect write targets as known state vars
            for var_name, _fp, _line in writes:
                known_vars.add(var_name)
            all_writes.extend(writes)

        t1 = time.monotonic()
        logger.info(
            "Requirement graph: file indexing done — %d files in %.1fs",
            files_scanned,
            t1 - t0,
        )

        # Create ActionNodes from monitor_action references
        for req in graph.requirements.values():
            if req.monitor_action:
                graph.add_action_if_absent(
                    ActionNode(
                        id=req.monitor_action,
                        name=get_last_component(req.monitor_action),
                        qualified_name=req.monitor_action,
                        file=req.file,
                        line=req.line,
                    )
                )

        # Create StateVarNodes from write targets
        for var_name, filepath_w, line_w in all_writes:
            if var_name not in graph.state_vars:
                graph.add_state_var(
                    StateVarNode(
                        id=var_name,
                        name=get_last_component(var_name),
                        qualified_name=var_name,
                        file=filepath_w,
                        line=line_w,
                    )
                )

        # Wire READS edges from requirements to state vars
        if known_vars:
            try:
                graph.wire_state_var_edges(known_vars)
            except ImportError:
                logger.debug(
                    "formula_analyzer unavailable; " "skipping READS edge wiring"
                )

        t2 = time.monotonic()
        logger.info(
            "Requirement graph: edge wiring done — %d vars in %.1fs",
            len(known_vars),
            t2 - t1,
        )

        # Load RFC requirement manifests and wire COVERS edges
        try:
            from ivy_lsp.core.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
            )

            for manifest_path in find_manifests(root):
                reqs_dict = load_requirement_manifest(manifest_path)
                for rfc_req in reqs_dict.values():
                    graph.add_rfc_requirement(rfc_req)

            if graph.rfc_requirements:
                graph.wire_coverage_edges()
        except ImportError:
            logger.debug("rfc_annotations unavailable; skipping manifest loading")

        t3 = time.monotonic()
        total = len(graph.requirements) + len(graph.actions) + len(graph.state_vars)
        logger.info(
            "Built requirement graph in %.1fs: %d requirements, %d actions, "
            "%d state vars, %d edges "
            "(indexing=%.1fs, wiring=%.1fs, manifests=%.1fs)",
            t3 - t0,
            len(graph.requirements),
            len(graph.actions),
            len(graph.state_vars),
            len(graph.edges),
            t1 - t0,
            t2 - t1,
            t3 - t2,
        )

        # --- Populate SemanticModel with the same data (sync bridge) ---
        if populate_semantic_model_fn is not None:
            populate_semantic_model_fn(graph)

        if total == 0:
            logger.warning(
                "Requirement graph built but empty: %d files scanned, "
                "0 requirements/actions/vars extracted. "
                "Files may lack monitors or RFC annotations.",
                files_scanned,
            )
            return None
        return graph
    except ImportError as exc:
        logger.warning("Requirement graph build failed (missing dependency): %s", exc)
        return None
    except Exception:
        logger.warning(
            "Failed to build requirement graph",
            exc_info=True,
        )
        return None


def write_model_to_index(
    root: str,
    model: Any,
    workspace_context: Any,
    find_ivy_files_fn: Any,
) -> None:
    """Write a SemanticModel to .ivy-index/ per-protocol directories.

    Uses :func:`write_locked_pickle` for atomic, lock-guarded writes.
    Bootstraps the index directories if they don't exist yet — this
    breaks the original deadlock where ``_write_model_to_index`` required
    pre-existing ``.ivy-index/`` dirs that only ``WorkspaceContext.load()``
    created (which in turn required ``manifest.json`` to exist).

    After this function runs, the next ``WorkspaceContext.load()`` will find
    ``manifest.json`` files and populate ``protocol_indexes``, enabling
    Strategy 1 (instant model load from pickle) on subsequent startups.

    Args:
        root: Workspace root directory.
        model: SemanticModel instance to persist.
        workspace_context: Loaded WorkspaceContext, or None.
        find_ivy_files_fn: Callable ``(dir) -> list[str]`` used to enumerate
            .ivy files within each protocol directory for manifest generation.
    """
    import hashlib
    import json as _json

    from ivy_lsp import __version__
    from ivy_lsp.infra.utils.serialization import write_locked_pickle

    try:
        # --- Discover protocol directories ---
        # If workspace_context has protocol_indexes, use those.
        # Otherwise, walk protocol-testing/*/ to bootstrap.
        proto_dirs: dict[str, str] = {}  # protocol_name -> protocol_dir

        if workspace_context is not None and workspace_context.protocol_indexes:
            for proto, idx in workspace_context.protocol_indexes.items():
                if idx.index_dir:
                    proto_dirs[proto] = os.path.dirname(idx.index_dir)
        else:
            # Bootstrap: discover protocol dirs from filesystem
            pt_root = os.path.join(root, "protocol-testing")
            if os.path.isdir(pt_root):
                for entry in os.listdir(pt_root):
                    candidate = os.path.join(pt_root, entry)
                    if os.path.isdir(candidate) and not entry.startswith("."):
                        proto_dirs[entry] = candidate

        if not proto_dirs:
            logger.debug("No protocol directories found for index bootstrap")
            return

        written = 0
        for proto, proto_dir in proto_dirs.items():
            index_dir = os.path.join(proto_dir, ".ivy-index")
            os.makedirs(index_dir, exist_ok=True)

            # Write .gitignore to prevent pickle commits
            gitignore_path = os.path.join(index_dir, ".gitignore")
            if not os.path.isfile(gitignore_path):
                try:
                    with open(gitignore_path, "w") as gf:
                        gf.write("*\n")
                except OSError:
                    pass

            # Build manifest.json with file mtimes for staleness tracking
            files_meta: dict[str, dict] = {}
            for ivy_path in find_ivy_files_fn(proto_dir):
                try:
                    rel = os.path.relpath(ivy_path, proto_dir)
                    stat = os.stat(ivy_path)
                    with open(ivy_path, "rb") as fh:
                        sha = hashlib.sha256(fh.read()).hexdigest()
                    files_meta[rel] = {
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                        "sha256": sha,
                    }
                except OSError:
                    continue

            manifest = {
                "version": 1,
                "protocol": proto,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "builder_version": __version__,
                "files": files_meta,
            }

            manifest_path = os.path.join(index_dir, "manifest.json")
            try:
                with open(manifest_path, "w") as mf:
                    _json.dump(manifest, mf, indent=2)
                logger.info(
                    "Wrote manifest.json for %s (%d files)", proto, len(files_meta)
                )
            except OSError:
                logger.debug("Failed to write manifest for %s", proto, exc_info=True)
                continue

            # Write semantic model pickle
            if write_locked_pickle(
                index_dir, "semantic_model.pickle.gz", model, logger
            ):
                written += 1

        if written:
            logger.info(
                "[INDEX-BOOTSTRAP] Persisted model to %d protocol index(es). "
                "Next startup will use Strategy 1 (instant load).",
                written,
            )
    except Exception:
        logger.debug("Failed to write model to .ivy-index/", exc_info=True)
