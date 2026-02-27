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


# --- Pure handler functions (testable without LSP wiring) ---


def handle_server_status(server: Any) -> Dict[str, Any]:
    mode = "full" if server._full_mode else "light"
    tools = {
        "ivyCheck": shutil.which("ivy_check") is not None,
        "ivyc": shutil.which("ivyc") is not None,
        "ivyShow": shutil.which("ivy_show") is not None,
    }
    return server.state_tracker.to_status_dict(
        mode=mode, version=__version__, tools=tools
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
