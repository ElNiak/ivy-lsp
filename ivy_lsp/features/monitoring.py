"""Monitoring request handlers for the Ivy LSP server."""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict

from ivy_lsp import __version__

logger = logging.getLogger(__name__)

# Cache tool availability at import time to avoid repeated PATH scans.
_tool_cache: Dict[str, bool] | None = None


def _get_tool_cache() -> Dict[str, bool]:
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = {
            "ivyCheck": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivyShow": shutil.which("ivy_show") is not None,
        }
    return _tool_cache


# --- Pure handler functions (testable without LSP wiring) ---


def handle_server_status(server: Any) -> Dict[str, Any]:
    mode = "full" if server._full_mode else "light"
    return server.state_tracker.to_status_dict(
        mode=mode, version=__version__, tools=_get_tool_cache()
    )


def handle_indexer_stats(server: Any) -> Dict[str, Any]:
    if server._indexer is None:
        return {
            "fileCount": 0,
            "symbolCount": 0,
            "includeEdgeCount": 0,
            "testScopeCount": 0,
            "perFileErrors": [],
            "staleFiles": [],
            "lastIndexTime": None,
            "lastIndexDuration": None,
        }
    stats = server._indexer.get_stats()
    return {
        "fileCount": stats.file_count,
        "symbolCount": stats.symbol_count,
        "includeEdgeCount": stats.include_edge_count,
        "testScopeCount": stats.test_scope_count,
        "perFileErrors": stats.per_file_errors,
        "staleFiles": stats.stale_files,
        "lastIndexTime": stats.last_index_time,
        "lastIndexDuration": stats.last_index_duration,
    }


def handle_operation_history(server: Any) -> Dict[str, Any]:
    history = server.state_tracker.operation_tracker.get_history()
    ops = []
    for rec in history:
        start_iso = datetime.fromtimestamp(
            rec.start_time, tz=timezone.utc
        ).isoformat()
        ops.append(
            {
                "type": rec.type,
                "file": rec.file,
                "startTime": start_iso,
                "duration": rec.duration or 0,
                "success": rec.success or False,
                "message": rec.message,
            }
        )
    return {"operations": ops}


def handle_include_graph(server: Any) -> Dict[str, Any]:
    if server._indexer is None:
        return {"nodes": [], "edges": []}
    graph = server._indexer._include_graph

    # Derive node set from _includes and _included_by keys
    all_uris: set = set()
    includes = getattr(graph, "_includes", {})
    included_by = getattr(graph, "_included_by", {})
    all_uris.update(includes.keys())
    for targets in includes.values():
        all_uris.update(targets)
    all_uris.update(included_by.keys())

    nodes = []
    for uri in sorted(all_uris):
        symbol_count = len(server._indexer.get_symbols(uri))
        nodes.append({"uri": uri, "symbolCount": symbol_count, "hasErrors": False})

    edges = []
    for src, targets in includes.items():
        for tgt in targets:
            edges.append({"from": src, "to": tgt})

    return {"nodes": nodes, "edges": edges}


def handle_reindex(server: Any) -> Dict[str, Any]:
    if server._indexer is None:
        return {"success": False, "message": "No indexer available"}
    try:
        server.state_tracker.set_indexing()
        start = time.time()
        server._indexer.reindex()
        duration = time.time() - start
        server.state_tracker.set_indexed(duration)
        return {"success": True, "message": f"Re-indexed in {duration:.1f}s"}
    except Exception as e:
        server.state_tracker.set_index_error(str(e))
        return {"success": False, "message": str(e)}


def handle_clear_cache(server: Any) -> Dict[str, Any]:
    if server._indexer is None:
        return {"success": False, "message": "No indexer available"}
    try:
        staging = getattr(server._indexer, "_staging_dir", None)
        if staging and os.path.exists(staging):
            shutil.rmtree(staging)
        server._indexer.reindex()
        return {"success": True, "message": "Cache cleared and re-indexed"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def handle_feature_status(server: Any) -> Dict[str, Any]:
    """Compute per-feature availability status."""
    from ivy_lsp.features.status import IndexingState

    features = []
    is_full = server._full_mode
    has_indexer = server._indexer is not None
    indexing = server.state_tracker.indexing_state
    indexer_ok = has_indexer and indexing == IndexingState.IDLE
    indexer_loading = has_indexer and indexing == IndexingState.INDEXING
    has_graph = has_indexer and getattr(
        server._indexer, "_requirement_graph", None
    ) is not None
    has_pipeline = getattr(server, "_analysis_pipeline", None) is not None
    has_model = getattr(server, "_semantic_model", None) is not None
    has_parser = server._parser is not None

    # --- Code Lens ---
    if indexer_loading:
        features.append({
            "id": "codeLens", "name": "Code Lens", "status": "loading",
            "reason": "Indexing in progress", "dependsOn": ["indexing"],
        })
    elif not indexer_ok:
        features.append({
            "id": "codeLens", "name": "Code Lens", "status": "unavailable",
            "reason": "Requires successful indexing", "dependsOn": ["indexing"],
        })
    else:
        features.append({
            "id": "codeLens", "name": "Code Lens", "status": "ready",
            "reason": "Requirement graph available" if has_graph
            else "Index available", "dependsOn": ["indexing"],
        })

    # --- Document Symbols ---
    if has_parser and is_full:
        features.append({
            "id": "documentSymbols", "name": "Document Symbols",
            "status": "ready", "reason": "Full parser available",
        })
    elif has_parser:
        features.append({
            "id": "documentSymbols", "name": "Document Symbols",
            "status": "degraded", "reason": "Fallback parser (light mode)",
        })
    else:
        features.append({
            "id": "documentSymbols", "name": "Document Symbols",
            "status": "unavailable", "reason": "No parser available",
        })

    # --- Diagnostics ---
    features.append({
        "id": "diagnostics", "name": "Diagnostics",
        "status": "ready" if is_full else "degraded",
        "reason": "Full diagnostics active" if is_full
        else "Structural checks only (light mode)",
    })

    # --- Semantic Analysis ---
    if not has_pipeline:
        features.append({
            "id": "semanticAnalysis", "name": "Semantic Analysis",
            "status": "unavailable", "reason": "Pipeline not initialized",
        })
    elif not is_full:
        features.append({
            "id": "semanticAnalysis", "name": "Semantic Analysis",
            "status": "degraded",
            "reason": "Tier 1 only (light mode, no z3)",
        })
    else:
        features.append({
            "id": "semanticAnalysis", "name": "Semantic Analysis",
            "status": "ready", "reason": "All tiers available",
        })

    # --- RFC Coverage ---
    if not has_model:
        features.append({
            "id": "rfcCoverage", "name": "RFC Coverage",
            "status": "unavailable",
            "reason": "Semantic model not initialized",
            "dependsOn": ["semanticAnalysis"],
        })
    else:
        model_ready = server._semantic_model.node_count() > 0
        features.append({
            "id": "rfcCoverage", "name": "RFC Coverage",
            "status": "ready" if model_ready else "degraded",
            "reason": f"Semantic model active ({server._semantic_model.node_count()} nodes)"
            if model_ready else "No data in semantic model yet",
            "dependsOn": ["semanticAnalysis"],
        })

    # --- Navigation (completion, definition, hover, references) ---
    if indexer_loading:
        features.append({
            "id": "navigation", "name": "Navigation",
            "status": "loading",
            "reason": "Indexing in progress",
            "dependsOn": ["indexing"],
        })
    elif indexer_ok:
        features.append({
            "id": "navigation", "name": "Navigation",
            "status": "ready",
            "reason": "Index available",
            "dependsOn": ["indexing"],
        })
    else:
        features.append({
            "id": "navigation", "name": "Navigation",
            "status": "unavailable",
            "reason": "Requires indexing",
            "dependsOn": ["indexing"],
        })

    # --- Pipeline state ---
    pipeline_state = {
        "tier1FileCount": 0, "tier2FileCount": 0, "tier3FileCount": 0,
        "tier3Running": False, "semanticNodeCount": 0,
        "semanticEdgeCount": 0, "semanticModelReady": False,
    }
    if has_pipeline:
        pipeline_state = server._analysis_pipeline.get_pipeline_state()

    return {"features": features, "analysisPipeline": pipeline_state}


# --- LSP wiring ---


def register(server: Any) -> None:
    """Register monitoring request handlers on the server."""

    @server.feature("ivy/serverStatus")
    def on_server_status(params: Any = None) -> Dict[str, Any]:
        return handle_server_status(server)

    @server.feature("ivy/indexerStats")
    def on_indexer_stats(params: Any = None) -> Dict[str, Any]:
        return handle_indexer_stats(server)

    @server.feature("ivy/operationHistory")
    def on_operation_history(params: Any = None) -> Dict[str, Any]:
        return handle_operation_history(server)

    @server.feature("ivy/includeGraph")
    def on_include_graph(params: Any = None) -> Dict[str, Any]:
        return handle_include_graph(server)

    @server.feature("ivy/reindex")
    def on_reindex(params: Any = None) -> Dict[str, Any]:
        return handle_reindex(server)

    @server.feature("ivy/clearCache")
    def on_clear_cache(params: Any = None) -> Dict[str, Any]:
        return handle_clear_cache(server)

    @server.feature("ivy/featureStatus")
    def on_feature_status(params: Any = None) -> Dict[str, Any]:
        return handle_feature_status(server)
