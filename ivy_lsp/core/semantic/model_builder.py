"""Shared semantic model builder for LSP and MCP servers.

Extracts the model-building logic that was previously inlined in
``mcp_server._build_model()`` into a reusable function.  Both the
MCP server's lazy model builder and the LSP server's
``AnalysisPipeline`` can delegate to this module to ensure
consistent model construction.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def build_semantic_model(
    root: str,
    find_files_fn: Callable[[str], list[str]],
    include_resolver: Any | None = None,
    stdlib_modules: frozenset[str] | None = None,
) -> Optional[Any]:
    """Build a SemanticModel from workspace files.

    Shared between the MCP server's standalone model builder and the
    LSP server's AnalysisPipeline.

    Parameters
    ----------
    root:
        Absolute path to the workspace root directory.
    find_files_fn:
        Callable that takes a root path and returns a list of relative
        ``.ivy`` file paths within the workspace.
    include_resolver:
        Optional resolve callback for the TieredExtractor parser tier.
        Signature: ``(include_name: str, from_file: str) -> Optional[str]``.
    stdlib_modules:
        Known Ivy standard library module names.  When ``None``, defaults
        to the standard set (order, collections, ip, etc.).

    Returns:
        SemanticModel or ``None`` when required dependencies are missing
        (logged at WARNING).
    """
    # Import required modules -- narrow ImportError to just these imports
    try:
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.rfc_annotations import (
            find_manifests,
            load_requirement_manifest,
            parse_file_rfc_annotations,
        )
    except ImportError:
        logger.warning(
            "Semantic model unavailable: required modules "
            "(ivy_lsp.core.semantic.model or ivy_lsp.core.semantic.rfc_annotations) "
            "could not be imported. Install ivy-lsp[semantic] to enable "
            "traceability tools.",
            exc_info=True,
        )
        return None

    model = SemanticModel()

    # Load manifests
    for manifest_path in find_manifests(root):
        reqs = load_requirement_manifest(manifest_path)
        for req in reqs.values():
            model.add_node(req)

    # Scan .ivy files for annotations, types, and symbols using
    # tiered extraction: parser -> lexer -> regex cascade.
    from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
    from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor

    extractor = TieredExtractor(resolve_callback=include_resolver)
    # Cache includes per file for INCLUDES edge wiring later
    file_includes: dict[str, list[str]] = {}
    # Cache references per file for CALLS/USES/MONITORS edge wiring later
    file_references: dict[str, list] = {}
    # Map basename (stem) -> abs_path for INCLUDES edge wiring (avoids
    # a second find_files_fn call).
    basename_to_path: dict[str, str] = {}
    tier_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
    total_symbols = 0
    build_start = time.monotonic()

    all_files = find_files_fn(root)
    for i, rel_path in enumerate(all_files):
        if i > 0 and i % 100 == 0:
            logger.info("Model build progress: %d/%d files", i, len(all_files))
        abs_path = os.path.join(root, rel_path)
        stem = os.path.splitext(os.path.basename(rel_path))[0]
        basename_to_path.setdefault(stem, abs_path)
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError as exc:
            logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
            continue

        # RFC annotations (operates on comments, not declarations)
        for ann in parse_file_rfc_annotations(source, abs_path):
            model.add_node(ann)

        # Extract symbols via tiered cascade
        result = extractor.extract(source, abs_path)
        if result.tier_used > 0:
            count = populate_model_from_symbols(
                model, result.symbols, abs_path, tier_used=result.tier_used
            )
            total_symbols += count
            tier_counts[result.tier_used] = tier_counts.get(result.tier_used, 0) + 1
            file_includes[abs_path] = result.includes
            if result.references:
                file_references[abs_path] = result.references

    build_elapsed = (time.monotonic() - build_start) * 1000
    logger.info(
        "Model built: %d files, tiers={1: %d, 2: %d, 3: %d}, %d symbols (%.1fms)",
        sum(tier_counts.values()),
        tier_counts.get(1, 0),
        tier_counts.get(2, 0),
        tier_counts.get(3, 0),
        total_symbols,
        build_elapsed,
    )
    fallback_count = tier_counts.get(2, 0) + tier_counts.get(3, 0)
    if fallback_count > 0:
        logger.info(
            "Model build: %d files required fallback parsing (%d tier-2, %d tier-3)",
            fallback_count,
            tier_counts.get(2, 0),
            tier_counts.get(3, 0),
        )

    # -- Wire semantic edges --
    _wire_semantic_edges(model, basename_to_path, file_includes, file_references)

    return model


def _wire_semantic_edges(
    model: Any,
    basename_to_path: dict[str, str],
    file_includes: dict[str, list[str]],
    file_references: dict[str, list] | None = None,
) -> None:
    """Wire COVERS, HAS_PARAM, RETURNS_TYPE, INCLUDES, CALLS, USES, MONITORS, and CONTAINS edges.

    Extracted as a helper to keep ``build_semantic_model`` readable.
    """
    from ivy_lsp.core.semantic.edges import SemanticEdgeType
    from ivy_lsp.core.semantic.nodes import (
        RfcAnnotation,
        RfcRequirement,
        SymbolNode,
        TypeNode,
    )
    from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

    # 1. COVERS: RfcAnnotation -> RfcRequirement
    # Use normalize_tag_to_manifest_ids for proper tag resolution:
    # bare numbers like "4" match "rfc9000:4.*", section refs like
    # "4.1" match "rfc9000:4.1", and qualified tags match exactly.
    req_by_id: dict[str, object] = {
        n.id: n for n in model.get_nodes_by_type(RfcRequirement)
    }
    req_id_set = set(req_by_id.keys())
    for ann in model.get_nodes_by_type(RfcAnnotation):
        for tag in ann.tags:
            matched_ids = normalize_tag_to_manifest_ids(tag, req_id_set)
            for req_id in matched_ids:
                model.add_edge(ann.id, SemanticEdgeType.COVERS, req_id)

    # 2. HAS_PARAM / RETURNS_TYPE: SymbolNode -> TypeNode
    type_by_name: dict[str, str] = {}
    for tn in model.get_nodes_by_type(TypeNode):
        if tn.name not in type_by_name:
            type_by_name[tn.name] = tn.id

    for sn in model.get_nodes_by_type(SymbolNode):
        # HAS_PARAM: parse "var : type" from params
        if sn.params:
            for param in sn.params:
                parts = param.split(":")
                if len(parts) < 2:
                    continue
                type_ref = parts[-1].strip()
                base = type_ref.split(".")[-1]
                target = type_by_name.get(base) or type_by_name.get(type_ref)
                if target:
                    model.add_edge(sn.id, SemanticEdgeType.HAS_PARAM, target)

        # RETURNS_TYPE
        ret = getattr(sn, "return_sort", None)
        if ret:
            base = ret.split(".")[-1]
            target = type_by_name.get(base) or type_by_name.get(ret)
            if target:
                model.add_edge(sn.id, SemanticEdgeType.RETURNS_TYPE, target)

    # 3. INCLUDES: file -> file (via include directives extracted above)
    for abs_path, includes in file_includes.items():
        nodes_in_src = model.get_nodes_in_file(abs_path)
        if not nodes_in_src:
            continue
        src_id = nodes_in_src[0].id
        for inc_name in includes:
            target_path = basename_to_path.get(inc_name)
            if not target_path or target_path == abs_path:
                continue
            nodes_in_tgt = model.get_nodes_in_file(target_path)
            if nodes_in_tgt:
                model.add_edge(src_id, SemanticEdgeType.INCLUDES, nodes_in_tgt[0].id)

    # 4. CALLS / USES / MONITORS edges from extracted references
    if file_references:

        def _resolve_name(name: str) -> str | None:
            """Resolve a symbol name to a node ID using O(1) name index."""
            last = name.rsplit(".", 1)[-1] if "." in name else name
            candidates = model.get_nodes_by_name(last)
            # Prefer qualified name match
            for c in candidates:
                if getattr(c, "qualified_name", None) == name:
                    return c.id
            # Fallback: first match by short name
            if candidates:
                return candidates[0].id
            return None

        for refs in file_references.values():
            for ref in refs:
                source_id = _resolve_name(ref.source_name)
                target_id = _resolve_name(ref.target_name)
                if not source_id or not target_id:
                    continue
                if source_id == target_id:
                    continue  # Skip self-edges

                if ref.kind == "call":
                    model.add_edge(source_id, SemanticEdgeType.CALLS, target_id)
                elif ref.kind == "instance":
                    model.add_edge(source_id, SemanticEdgeType.USES, target_id)
                elif ref.kind == "monitor":
                    model.add_edge(source_id, SemanticEdgeType.MONITORS, target_id)

    # 5. CONTAINS edges from qualified names
    all_node_ids: dict[str, str] = {}
    for sn in model.get_nodes_by_type(SymbolNode):
        all_node_ids[sn.qualified_name] = sn.id
    for tn in model.get_nodes_by_type(TypeNode):
        all_node_ids[tn.qualified_name] = tn.id

    for qname, node_id in all_node_ids.items():
        if "." in qname:
            parent_qname = qname.rsplit(".", 1)[0]
            parent_id = all_node_ids.get(parent_qname)
            if parent_id:
                model.add_edge(parent_id, SemanticEdgeType.CONTAINS, node_id)
