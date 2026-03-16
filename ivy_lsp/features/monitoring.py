"""Monitoring request handlers for the Ivy LSP server."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict

from ivy_lsp import __version__
from ivy_lsp.protocols import IvyServerProtocol

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


def handle_server_status(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return server status including mode, uptime, and tool availability."""
    mode = "full" if server.full_mode else "light"
    result = server.state_tracker.to_status_dict(
        mode=mode, version=__version__, tools=_get_tool_cache()
    )
    result["initializing"] = getattr(server, "initializing", False)
    return result


def handle_indexer_stats(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return indexer statistics such as file, symbol, and edge counts."""
    if server.indexer is None:
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
    stats = server.indexer.get_stats()
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


def handle_operation_history(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return the recent operation history from the state tracker."""
    history = server.state_tracker.operation_tracker.get_history()
    ops = []
    for rec in history:
        start_iso = datetime.fromtimestamp(rec.start_time, tz=timezone.utc).isoformat()
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


def handle_include_graph(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return the include dependency graph as nodes and edges."""
    if server.indexer is None:
        return {"nodes": [], "edges": []}
    graph = server.indexer.include_graph

    # Snapshot dicts before iteration to avoid RuntimeError if
    # background indexing mutates _includes/_included_by concurrently.
    all_paths: set = set()
    includes = dict(getattr(graph, "_includes", {}))
    included_by_keys = set(getattr(graph, "_included_by", {}).keys())
    all_paths.update(includes.keys())
    for targets in includes.values():
        all_paths.update(targets)
    all_paths.update(included_by_keys)

    nodes = []
    for fpath in sorted(all_paths):
        # Normalize to absolute path so get_symbols() cache lookup works
        abs_path = os.path.abspath(fpath)
        symbol_count = len(server.indexer.get_symbols(abs_path))
        nodes.append({"uri": abs_path, "symbolCount": symbol_count, "hasErrors": False})

    edges = []
    for src, targets in includes.items():
        for tgt in targets:
            edges.append({"from": os.path.abspath(src), "to": os.path.abspath(tgt)})

    return {"nodes": nodes, "edges": edges}


def handle_reindex(server: IvyServerProtocol) -> Dict[str, Any]:
    """Trigger a workspace re-index and return the result."""
    if server.indexer is None:
        return {"success": False, "message": "No indexer available"}
    try:
        server.state_tracker.set_indexing()
        start = time.time()
        server.indexer.reindex()
        duration = time.time() - start
        server.state_tracker.set_indexed(duration)
        return {"success": True, "message": f"Re-indexed in {duration:.1f}s"}
    except Exception as e:
        server.state_tracker.set_index_error(str(e))
        return {"success": False, "message": str(e)}


def handle_clear_cache(server: IvyServerProtocol) -> Dict[str, Any]:
    """Clear the resolver staging cache and re-index the workspace."""
    if server.indexer is None:
        return {"success": False, "message": "No indexer available"}
    try:
        resolver = server.indexer.resolver
        staging = getattr(resolver, "_staging_dir", None) if resolver else None
        if staging and os.path.exists(staging):
            shutil.rmtree(staging)
        server.indexer.reindex()
        return {"success": True, "message": "Cache cleared and re-indexed"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def handle_feature_status(server: IvyServerProtocol) -> Dict[str, Any]:
    """Compute per-feature availability status."""
    from ivy_lsp.features.status import IndexingState

    features = []
    is_full = server.full_mode
    has_indexer = server.indexer is not None
    indexing = server.state_tracker.indexing_state
    indexer_ok = has_indexer and indexing == IndexingState.IDLE
    indexer_loading = has_indexer and indexing == IndexingState.INDEXING
    has_graph = has_indexer and server.indexer.requirement_graph is not None
    has_pipeline = server.analysis_pipeline is not None
    has_model = server.semantic_model is not None
    has_parser = server.parser is not None

    # --- Code Lens ---
    if indexer_loading:
        features.append(
            {
                "id": "codeLens",
                "name": "Code Lens",
                "status": "loading",
                "reason": "Indexing in progress",
                "dependsOn": ["indexing"],
            }
        )
    elif not indexer_ok:
        features.append(
            {
                "id": "codeLens",
                "name": "Code Lens",
                "status": "unavailable",
                "reason": "Requires successful indexing",
                "dependsOn": ["indexing"],
            }
        )
    else:
        features.append(
            {
                "id": "codeLens",
                "name": "Code Lens",
                "status": "ready",
                "reason": (
                    "Requirement graph available" if has_graph else "Index available"
                ),
                "dependsOn": ["indexing"],
            }
        )

    # --- Document Symbols ---
    if has_parser and is_full:
        features.append(
            {
                "id": "documentSymbols",
                "name": "Document Symbols",
                "status": "ready",
                "reason": "Full parser available",
            }
        )
    elif has_parser:
        features.append(
            {
                "id": "documentSymbols",
                "name": "Document Symbols",
                "status": "degraded",
                "reason": "Fallback parser (light mode)",
            }
        )
    else:
        features.append(
            {
                "id": "documentSymbols",
                "name": "Document Symbols",
                "status": "unavailable",
                "reason": "No parser available",
            }
        )

    # --- Diagnostics ---
    features.append(
        {
            "id": "diagnostics",
            "name": "Diagnostics",
            "status": "ready" if is_full else "degraded",
            "reason": (
                "Full diagnostics active"
                if is_full
                else "Structural checks only (light mode)"
            ),
        }
    )

    # --- Semantic Analysis ---
    if not has_pipeline:
        features.append(
            {
                "id": "semanticAnalysis",
                "name": "Semantic Analysis",
                "status": "unavailable",
                "reason": "Pipeline not initialized",
            }
        )
    elif not is_full:
        features.append(
            {
                "id": "semanticAnalysis",
                "name": "Semantic Analysis",
                "status": "degraded",
                "reason": "Tier 1 only (light mode, no z3)",
            }
        )
    else:
        features.append(
            {
                "id": "semanticAnalysis",
                "name": "Semantic Analysis",
                "status": "ready",
                "reason": "All tiers available",
            }
        )

    # --- RFC Coverage ---
    if not has_model:
        features.append(
            {
                "id": "rfcCoverage",
                "name": "RFC Coverage",
                "status": "unavailable",
                "reason": "Semantic model not initialized",
                "dependsOn": ["semanticAnalysis"],
            }
        )
    else:
        model_ready = server.semantic_model.node_count() > 0
        rfc_feature: Dict[str, Any] = {
            "id": "rfcCoverage",
            "name": "RFC Coverage",
            "status": "ready" if model_ready else "degraded",
            "reason": (
                f"Semantic model active ({server.semantic_model.node_count()} nodes)"
                if model_ready
                else "No data in semantic model yet"
            ),
            "dependsOn": ["semanticAnalysis"],
        }
        if model_ready:
            try:
                from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement
                from ivy_lsp.semantic.rfc_annotations import compute_coverage

                annotations = server.semantic_model.get_nodes_by_type(RfcAnnotation)
                reqs_list = server.semantic_model.get_nodes_by_type(RfcRequirement)
                reqs = {r.id: r for r in reqs_list}
                if reqs:
                    stats = compute_coverage(annotations, reqs)
                    rfc_feature["coverage"] = {
                        "total": stats.total,
                        "covered": stats.covered,
                        "uncovered": stats.uncovered,
                        "byLevel": stats.by_level,
                    }
            except Exception:
                logger.debug("RFC coverage stats unavailable", exc_info=True)
        features.append(rfc_feature)

    # --- Navigation (completion, definition, hover, references) ---
    if indexer_loading:
        features.append(
            {
                "id": "navigation",
                "name": "Navigation",
                "status": "loading",
                "reason": "Indexing in progress",
                "dependsOn": ["indexing"],
            }
        )
    elif indexer_ok:
        features.append(
            {
                "id": "navigation",
                "name": "Navigation",
                "status": "ready",
                "reason": "Index available",
                "dependsOn": ["indexing"],
            }
        )
    else:
        features.append(
            {
                "id": "navigation",
                "name": "Navigation",
                "status": "unavailable",
                "reason": "Requires indexing",
                "dependsOn": ["indexing"],
            }
        )

    # --- Pipeline state ---
    pipeline_state = {
        "tier1FileCount": 0,
        "tier2FileCount": 0,
        "tier3FileCount": 0,
        "tier3Running": False,
        "tier3Succeeded": 0,
        "tier3Failed": 0,
        "tier3CurrentFile": None,
        "tier3LastFile": None,
        "tier3LastCompletedAt": None,
        "tier3Pending": 0,
        "semanticNodeCount": 0,
        "semanticEdgeCount": 0,
        "semanticModelReady": False,
        "bulkAnalysisRunning": False,
        "bulkAnalysisTotal": 0,
        "bulkAnalysisCompleted": 0,
        "bulkCompileRunning": False,
        "bulkCompileTotal": 0,
        "bulkCompileCompleted": 0,
        "cachedFiles": 0,
        "activeProcesses": 0,
        "maxConcurrent": 0,
    }
    if has_pipeline:
        pipeline_state = server.analysis_pipeline.get_pipeline_state()

    return {"features": features, "analysisPipeline": pipeline_state}


def handle_deep_index_progress(
    server: IvyServerProtocol, params: dict | None = None
) -> Dict[str, Any]:
    """Return current deep indexing progress.

    By default only summary counts are returned.  Pass
    ``includeFileStatuses: true`` to include the per-file detail array
    (which can be large for workspaces with many test files).
    """
    if server.indexer is None:
        result: Dict[str, Any] = {
            "running": False,
            "totalTests": 0,
            "completedTests": 0,
            "currentFile": None,
            "startedAt": None,
            "elapsedSeconds": None,
            "fileStatusCount": 0,
        }
        if (params or {}).get("includeFileStatuses", False):
            result["fileStatuses"] = []
        return result
    # Thread-safe snapshot via the public accessor — all fields are
    # captured under _progress_lock in a single call.
    snap = server.indexer.get_deep_index_progress()
    running = snap["running"]
    total_tests = snap["total_test_files"]
    completed_tests = snap["completed_test_files"]
    current_file = snap["current_file"]
    started_at = snap["started_at"]
    file_status_count = snap["file_status_count"]
    include_files = (params or {}).get("includeFileStatuses", False)
    file_statuses_snapshot = (
        list(snap["file_statuses"].values()) if include_files else []
    )

    elapsed = None
    started_at_iso = None
    if started_at is not None:
        elapsed = round(time.time() - started_at, 1)
        started_at_iso = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()

    result: Dict[str, Any] = {
        "running": running,
        "totalTests": total_tests,
        "completedTests": completed_tests,
        "currentFile": current_file,
        "startedAt": started_at_iso,
        "elapsedSeconds": elapsed,
        "fileStatusCount": file_status_count,
    }

    if include_files:
        result["fileStatuses"] = [
            {
                "file": s.filepath,
                "shallowIndexed": s.shallow_indexed,
                "deepParseAttempted": s.deep_parse_attempted,
                "deepParseSucceeded": s.deep_parse_succeeded,
                "parseError": s.parse_error,
                "parseDuration": (
                    round(s.parse_duration, 2) if s.parse_duration is not None else None
                ),
            }
            for s in file_statuses_snapshot
        ]

    return result


def handle_compilation_progress(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return current bulk compilation progress.

    Reads from the unified ``AnalysisPipeline.get_pipeline_state()``
    which includes both bulk compile tracking and CompilerManager stats.
    """
    pipeline = server.analysis_pipeline
    if pipeline is not None:
        ps = pipeline.get_pipeline_state()
        return {
            "running": ps.get("bulkCompileRunning", False),
            "total": ps.get("bulkCompileTotal", 0),
            "completed": ps.get("bulkCompileCompleted", 0),
            "cachedFiles": ps.get("cachedFiles", 0),
            "activeProcesses": ps.get("activeProcesses", 0),
            "maxConcurrent": ps.get("maxConcurrent", 0),
        }
    return {
        "running": False,
        "total": 0,
        "completed": 0,
        "cachedFiles": 0,
        "activeProcesses": 0,
        "maxConcurrent": 0,
    }


def handle_analysis_pipeline_detail(
    server: IvyServerProtocol, params: dict | None = None
) -> Dict[str, Any]:
    """Combined analysis pipeline detail: tiers, T3 per-file, compilation, bulk, semantic."""
    has_pipeline = server.analysis_pipeline is not None

    # Tier counts
    if has_pipeline:
        ps = server.analysis_pipeline.get_pipeline_state()
    else:
        ps = {
            "tier1FileCount": 0,
            "tier2FileCount": 0,
            "tier3FileCount": 0,
            "tier3Running": False,
            "tier3Succeeded": 0,
            "tier3Failed": 0,
            "tier3CurrentFile": None,
            "tier3LastFile": None,
            "tier3LastCompletedAt": None,
            "tier3Pending": 0,
            "semanticNodeCount": 0,
            "semanticEdgeCount": 0,
            "semanticModelReady": False,
            "bulkAnalysisRunning": False,
            "bulkAnalysisTotal": 0,
            "bulkAnalysisCompleted": 0,
            "bulkCompileRunning": False,
            "bulkCompileTotal": 0,
            "bulkCompileCompleted": 0,
            "cachedFiles": 0,
            "activeProcesses": 0,
            "maxConcurrent": 0,
        }

    # T3 detail
    include_results = (params or {}).get("includeFileResults", False)
    tier3: Dict[str, Any] = {
        "running": ps.get("tier3Running", False),
        "currentFile": ps.get("tier3CurrentFile"),
        "fileCount": ps.get("tier3FileCount", 0),
        "succeeded": ps.get("tier3Succeeded", 0),
        "failed": ps.get("tier3Failed", 0),
        "lastFile": ps.get("tier3LastFile"),
        "lastCompletedAt": ps.get("tier3LastCompletedAt"),
        "pending": ps.get("tier3Pending", 0),
    }
    if include_results and has_pipeline:
        tier3["results"] = server.analysis_pipeline.get_tier3_file_results()

    # Compilation status
    compilation = handle_compilation_progress(server)

    return {
        "tiers": {
            "t1": ps.get("tier1FileCount", 0),
            "t2": ps.get("tier2FileCount", 0),
            "t3": ps.get("tier3FileCount", 0),
        },
        "tier3": tier3,
        "compilation": compilation,
        "bulk": {
            "running": ps.get("bulkAnalysisRunning", False),
            "total": ps.get("bulkAnalysisTotal", 0),
            "completed": ps.get("bulkAnalysisCompleted", 0),
        },
        "semanticModel": {
            "nodeCount": ps.get("semanticNodeCount", 0),
            "edgeCount": ps.get("semanticEdgeCount", 0),
            "ready": ps.get("semanticModelReady", False),
        },
    }


def handle_test_feature_matrix(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return per-test feature availability matrix."""
    if server.indexer is None:
        return {"tests": []}
    # Thread-safe snapshots via public accessors.
    export_imports = server.indexer.get_file_export_imports()
    progress_snap = server.indexer.get_deep_index_progress()
    file_statuses = progress_snap["file_statuses"]

    tests = []
    for filepath, info in export_imports.items():
        if not info.has_exports:
            continue
        status = file_statuses.get(filepath)
        deep_ok = status is not None and status.deep_parse_succeeded
        if deep_ok:
            features = {
                "completion": "ready",
                "definition": "ready",
                "hover": "ready",
                "diagnostics": "ready",
                "codeLens": "ready",
                "rfcCoverage": "degraded",
            }
        else:
            features = {
                "completion": "degraded",
                "definition": "degraded",
                "hover": "degraded",
                "diagnostics": "unavailable",
                "codeLens": "unavailable",
                "rfcCoverage": "unavailable",
            }
        tests.append({"file": filepath, "features": features})
    return {"tests": tests}


def handle_batch_status(server: IvyServerProtocol) -> Dict[str, Any]:
    """Return all monitoring data in one round-trip."""
    return {
        "serverStatus": handle_server_status(server),
        "indexerStats": handle_indexer_stats(server),
        "operationHistory": handle_operation_history(server),
        "featureStatus": handle_feature_status(server),
        "deepIndexProgress": handle_deep_index_progress(server),
        "testFeatureMatrix": handle_test_feature_matrix(server),
        "analysisPipelineDetail": handle_analysis_pipeline_detail(server),
    }


# --- LSP wiring ---


def register(server: Any) -> None:
    """Register monitoring request handlers on the server."""

    @server.feature("ivy/serverStatus")
    async def on_server_status(params: Any = None) -> Dict[str, Any]:
        return handle_server_status(server)

    @server.feature("ivy/indexerStats")
    async def on_indexer_stats(params: Any = None) -> Dict[str, Any]:
        return handle_indexer_stats(server)

    @server.feature("ivy/operationHistory")
    async def on_operation_history(params: Any = None) -> Dict[str, Any]:
        return handle_operation_history(server)

    @server.feature("ivy/includeGraph")
    async def on_include_graph(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, handle_include_graph, server)

    @server.feature("ivy/reindex")
    async def on_reindex(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, handle_reindex, server)

    @server.feature("ivy/clearCache")
    async def on_clear_cache(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, handle_clear_cache, server)

    @server.feature("ivy/featureStatus")
    async def on_feature_status(params: Any = None) -> Dict[str, Any]:
        return handle_feature_status(server)

    @server.feature("ivy/deepIndexProgress")
    async def on_deep_index_progress(params: Any = None) -> Dict[str, Any]:
        return handle_deep_index_progress(
            server, params if isinstance(params, dict) else None
        )

    @server.feature("ivy/compilationStatus")
    async def on_compilation_status(params: Any = None) -> Dict[str, Any]:
        return handle_compilation_progress(server)

    @server.feature("ivy/analysisPipelineDetail")
    async def on_analysis_pipeline_detail(params: Any = None) -> Dict[str, Any]:
        return handle_analysis_pipeline_detail(
            server, params if isinstance(params, dict) else None
        )

    @server.feature("ivy/testFeatureMatrix")
    async def on_test_feature_matrix(params: Any = None) -> Dict[str, Any]:
        return handle_test_feature_matrix(server)

    @server.feature("ivy/batchStatus")
    async def on_batch_status(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, handle_batch_status, server)
