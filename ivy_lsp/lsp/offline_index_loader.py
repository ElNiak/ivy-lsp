"""Offline index deserialization: load pickles, merge per-protocol models."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


def _patch_symbol_file_paths(sym: Any, abs_path: str) -> None:
    """Recursively set ``file_path`` on *sym* and all descendants."""
    stack = [sym]
    while stack:
        s = stack.pop()
        s.file_path = abs_path
        stack.extend(s.children)


def populate_from_offline_index(
    workspace_context: Any,
    indexer: Any,
    analysis_pipeline: Any = None,
) -> Optional[Any]:
    """Populate indexer from offline index artifacts.

    Mutates *indexer* in place (symbol table, include graph, exports,
    requirement graph). Returns the merged SemanticModel if one was
    loaded, or None.

    Args:
        workspace_context: Object with a ``protocol_indexes`` mapping.
        indexer: Workspace indexer to populate in place.
        analysis_pipeline: Optional pipeline for marking cached files analyzed.

    Returns:
        Merged SemanticModel if any protocol index carried one, else None.
    """
    from ivy_lsp.core.indexer.workspace_indexer import FileIndexStatus
    from ivy_lsp.core.parsing.symbols import IvySymbol

    protocol_indexes = getattr(workspace_context, "protocol_indexes", None)
    if not protocol_indexes:
        return None

    prepop_start = time.time()
    total_files = 0
    total_symbols = 0
    total_edges = 0

    frozen_indexes = dict(protocol_indexes)

    for proto_name, proto_idx in frozen_indexes.items():
        protocol_dir = os.path.dirname(proto_idx.index_dir)

        for rel_path, sym_dicts in proto_idx.symbols.items():
            abs_path = os.path.join(protocol_dir, rel_path)
            for sd in sym_dicts:
                try:
                    sym = IvySymbol.from_dict(sd)
                    _patch_symbol_file_paths(sym, abs_path)
                    indexer._symbol_table.add_symbol(sym)
                    total_symbols += 1
                except Exception:
                    logger.debug(
                        "Skipping corrupt symbol in %s/%s",
                        proto_name,
                        rel_path,
                        exc_info=True,
                    )
            total_files += 1

            with indexer._progress_lock:
                indexer._deep_index_progress.file_statuses[abs_path] = FileIndexStatus(
                    filepath=abs_path,
                    shallow_indexed=True,
                    last_indexed_at=time.time(),
                )

        edges = proto_idx.includes.to_edges()
        for from_rel, to_rels in edges.items():
            abs_from = os.path.join(protocol_dir, from_rel)
            for to_rel in to_rels:
                abs_to = os.path.join(protocol_dir, to_rel)
                indexer._include_graph.add_edge(abs_from, abs_to)
                total_edges += 1

        for rel_path, export_info in proto_idx.exports.items():
            abs_path = os.path.join(protocol_dir, rel_path)
            patched = type(export_info)(
                file=abs_path,
                exports=list(export_info.exports),
                imports=list(export_info.imports),
                export_lines=dict(export_info.export_lines),
                import_lines=dict(export_info.import_lines),
            )
            with indexer._exports_lock:
                indexer._file_export_imports[abs_path] = patched

        if proto_idx.requirement_graph is not None:
            proto_idx.requirement_graph.remap_paths(protocol_dir)
            indexer._requirement_graph = proto_idx.requirement_graph

    semantic_model: Any = None
    loaded_model_protocols = 0
    for proto_name, proto_idx in frozen_indexes.items():
        if proto_idx.semantic_model is not None:
            if semantic_model is None:
                from ivy_lsp.core.semantic.model import SemanticModel

                semantic_model = SemanticModel()
                logger.info("Lazy-initialized SemanticModel for offline cache merge")
            try:
                semantic_model.merge_from(proto_idx.semantic_model)
                loaded_model_protocols += 1
            except Exception:
                logger.debug(
                    "Skipping incompatible semantic model for %s",
                    proto_name,
                    exc_info=True,
                )

    if loaded_model_protocols > 0:
        slog.info(
            "Loaded cached semantic model from %d protocol(s)",
            loaded_model_protocols,
            extra={
                "event": LogEvent(
                    LogCategory.MILESTONE,
                    "offline_semantic_model",
                    {"protocols_loaded": loaded_model_protocols},
                )
            },
        )
        if analysis_pipeline is not None:
            cached_files = semantic_model.files
            analysis_pipeline.mark_files_analyzed(cached_files)
            slog.info(
                "Pre-populated T1/T2 tracking sets with %d cached files",
                len(cached_files),
                extra={
                    "event": LogEvent(
                        LogCategory.MILESTONE,
                        "tracking_sets_prepopulated",
                        {"cached_files": len(cached_files)},
                    )
                },
            )

    slog.info(
        "Pre-populated from offline index: %d files, %d symbols, %d include edges",
        total_files,
        total_symbols,
        total_edges,
        extra={
            "event": LogEvent(
                LogCategory.MILESTONE,
                "offline_index_prepopulate",
                {
                    "files": total_files,
                    "symbols": total_symbols,
                    "include_edges": total_edges,
                },
            )
        },
    )

    indexer._wire_requirement_graph()
    indexer._load_requirement_manifests()
    indexer._wire_coverage_edges()
    indexer._compute_test_scopes()
    indexer._last_index_duration = time.time() - prepop_start
    indexer._last_index_time = time.time()

    from ivy_lsp.core.parsing.fallback_parser import FallbackOnlyParser

    has_full_parser = not isinstance(indexer._parser, FallbackOnlyParser)
    if has_full_parser:
        with indexer._progress_lock:
            indexer._deep_index_running = True
        t = threading.Thread(
            target=indexer._deep_index_from_tests,
            daemon=True,
            name="ivy-deep-index",
        )
        t.start()

    return semantic_model if loaded_model_protocols > 0 else None
