"""Mixin providing bulk analysis and compilation orchestration.

Methods on this mixin operate on ``self`` attributes from
:class:`~ivy_lsp.lsp.server.IvyLanguageServer` via Python's MRO.
"""

import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional, Set

from lsprotocol import types as lsp

from ivy_lsp.core.parsing.symbols import IncludeGraph
from ivy_lsp.core.workspace.context import StalenessInfo
from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.infra.utils.hashing import file_sha256

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})

CASCADE_BUDGET_RATIO = 0.5


def _validate_changes(staleness: StalenessInfo, protocol_dir: str) -> Set[str]:
    """Hash-validate mtime-flagged files. Returns truly changed abs paths."""
    dirty: Set[str] = set()
    for fc in staleness.file_changes:
        abs_path = os.path.join(protocol_dir, fc.rel_path)
        if fc.reason == "removed":
            dirty.add(abs_path)
            continue
        if fc.cached_sha256 is None:
            dirty.add(abs_path)
            continue
        try:
            current_sha = file_sha256(abs_path)
        except OSError:
            dirty.add(abs_path)
            continue
        if current_sha != fc.cached_sha256:
            dirty.add(abs_path)
    return dirty


def _expand_dirty_set(dirty_files: Set[str], include_graph: IncludeGraph) -> Set[str]:
    """Expand dirty files with all transitive reverse dependents."""
    expanded = set(dirty_files)
    for f in dirty_files:
        expanded |= include_graph.get_transitive_included_by(f)
    return expanded


def expand_with_budget(
    dirty_files: Set[str],
    include_graph: IncludeGraph,
    total_files: int,
    budget_ratio: float = CASCADE_BUDGET_RATIO,
) -> Optional[Set[str]]:
    """Expand dirty set; return None if cascade exceeds budget."""
    expanded = _expand_dirty_set(dirty_files, include_graph)
    if total_files > 0 and len(expanded) / total_files > budget_ratio:
        return None
    return expanded


if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer as _LS

    from ivy_lsp.lsp._protocols import IvyServerHost

    class _BulkBase(IvyServerHost, _LS): ...  # type: ignore[misc]

else:
    _BulkBase = object


class BulkOrchestrationMixin(_BulkBase):
    """Background analysis and compilation orchestration for IvyLanguageServer."""

    _incremental_stats: Optional[dict] = None

    def _cleanup_staging(self) -> None:
        """Clean up the staging directory on shutdown."""
        if self._indexer:
            try:
                self._indexer.resolver.cleanup_staging()
                slog.info(
                    "Staging directory cleaned up",
                    extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "staging")},
                )
            except Exception:
                logger.exception("Failed to clean up staging directory")

    def _filter_files_to_workspace(self, all_files: list[str]) -> list[str]:
        """Filter file list to only files within active workspace layers.

        If no workspace is active or no layer routing is available,
        returns all files unchanged.
        """
        resolver = self._indexer.resolver if self._indexer else None
        if resolver is None or not hasattr(resolver, "_active_layers"):
            return all_files

        active = getattr(resolver, "_active_layers", set())
        if not active:
            return all_files

        file_to_layer = getattr(resolver, "_file_to_layer", {})
        if not file_to_layer:
            return all_files

        filtered = [f for f in all_files if file_to_layer.get(f) in active]

        if filtered:
            slog.info(
                "Workspace-scoped bulk analysis: %d/%d files (layers: %s)",
                len(filtered),
                len(all_files),
                sorted(active),
                extra={
                    "event": LogEvent(LogCategory.DIAGNOSTIC, "bulk_workspace_filter")
                },
            )
            return filtered

        # Fallback: if filtering produced empty set, use all files
        logger.debug(
            "Workspace filter produced empty set — using all %d files",
            len(all_files),
        )
        return all_files

    def _send_model_ready_notification(self) -> None:
        """Send ``ivy/modelReady`` notification so the client can refresh immediately."""
        if self._shutdown_event.is_set():
            return
        try:
            graph = self._indexer.requirement_graph if self._indexer else None
            action_count = len(graph.actions) if graph else 0
            req_count = len(graph.requirements) if graph else 0
            self.protocol.notify(
                "ivy/modelReady",
                {"actionCount": action_count, "requirementCount": req_count},
            )
            slog.info(
                "Sent ivy/modelReady: %d actions, %d requirements",
                action_count,
                req_count,
                extra={
                    "event": LogEvent(
                        LogCategory.MILESTONE,
                        "model_ready",
                        {"actions": action_count, "requirements": req_count},
                    )
                },
            )
        except Exception:
            logger.warning("Failed to send ivy/modelReady notification", exc_info=True)

    def _write_shared_cache(self) -> None:
        """Persist SemanticModel to .ivy-index/ per-protocol directories.

        Called after bulk T1+T2 analysis completes so the MCP process can
        load the model from .ivy-index/ instead of rebuilding from scratch.

        Uses fcntl file locking to avoid races with concurrent indexing.
        """
        import fcntl
        import glob as glob_mod
        import gzip
        import pickle

        try:
            indexer = self._indexer
            if indexer is None or self._semantic_model is None:
                return
            root = indexer._workspace_root

            # Find all .ivy-index/ directories under protocol-testing/*/
            pattern = os.path.join(root, "protocol-testing", "*", ".ivy-index")
            index_dirs = [d for d in glob_mod.glob(pattern) if os.path.isdir(d)]

            # Also include workspace-level .ivy-index/ if it exists
            ws_index = os.path.join(root, ".ivy-index")
            if os.path.isdir(ws_index):
                index_dirs.append(ws_index)

            if not index_dirs:
                logger.debug("No .ivy-index/ directories found, skipping write")
                return

            for index_dir in index_dirs:
                lock_path = os.path.join(index_dir, ".build.lock")
                try:
                    lock_fd = open(lock_path, "w")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        out_path = os.path.join(index_dir, "semantic_model.pickle.gz")
                        with gzip.open(out_path, "wb") as f:
                            pickle.dump(
                                self._semantic_model,
                                f,
                                protocol=pickle.HIGHEST_PROTOCOL,
                            )
                        logger.info("Wrote model to %s", out_path)
                    except BlockingIOError:
                        logger.debug(
                            "Index lock held for %s, skipping write", index_dir
                        )
                    finally:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                        lock_fd.close()
                except OSError:
                    logger.debug("Cannot write to %s", index_dir, exc_info=True)
        except Exception:
            logger.debug("Model write to .ivy-index/ skipped", exc_info=True)

    def _send_server_ready_notification(self, init_start: float) -> None:
        """Send ``ivy/serverReady`` notification after initialization completes."""
        if self._shutdown_event.is_set():
            return
        try:
            mode = "full" if self._full_mode else "light"
            init_duration = round(time.time() - init_start, 3)
            self.protocol.notify(
                "ivy/serverReady",
                {"mode": mode, "indexingDuration": init_duration},
            )
            slog.info(
                "Sent ivy/serverReady: mode=%s, duration=%.3fs",
                mode,
                init_duration,
                extra={
                    "event": LogEvent(
                        LogCategory.MILESTONE,
                        "server_ready",
                        {"mode": mode, "duration_s": init_duration},
                    )
                },
            )
        except Exception:
            logger.warning("Failed to send ivy/serverReady notification", exc_info=True)

    def _make_progress_callback(
        self,
        title: str,
        begin_msg: str,
        end_msg: str,
        throttle_seconds: float = 0.0,
    ):
        """Create a ``$/progress`` work-done notification callback.

        The callback is invoked from background threads.  pygls message
        sending is thread-safe (queued internally), so
        ``work_done_progress.begin/report/end`` can be called directly.

        Args:
            title: Progress bar title (e.g. "Ivy Deep Index").
            begin_msg: Begin message with ``{total}`` placeholder.
            end_msg: End message with ``{total}`` placeholder.
            throttle_seconds: Minimum interval between intermediate
                reports (0 = no throttle).

        Returns:
            A callable ``(completed, total, current_file) -> None``.
        """
        token = str(uuid.uuid4())
        state = {"begun": False, "disabled": False, "last_report": 0.0}
        state_lock = threading.Lock()
        server = self

        # Skip progress entirely if the client doesn't support it
        if not self._client_supports_work_done_progress:
            state["disabled"] = True
            logger.debug(
                "Skipping work-done progress: client does not advertise support"
            )
        else:
            # Eagerly create the progress token (outside any callback)
            try:
                server.work_done_progress.create(token)
            except Exception:
                state["disabled"] = True
                logger.debug(
                    "Client does not support work-done progress", exc_info=True
                )

        def _callback(completed: int, total: int, current_file):
            if server._shutdown_event.is_set():
                return
            with state_lock:
                if state["disabled"]:
                    return
                if not state["begun"]:
                    try:
                        server.work_done_progress.begin(
                            token,
                            lsp.WorkDoneProgressBegin(
                                title=title,
                                message=begin_msg.format(total=total),
                                cancellable=False,
                                percentage=0,
                            ),
                        )
                        state["begun"] = True
                    except Exception:
                        state["disabled"] = True
                        return

                if completed >= total:
                    try:
                        server.work_done_progress.end(
                            token,
                            lsp.WorkDoneProgressEnd(
                                message=end_msg.format(total=total),
                            ),
                        )
                    except Exception:
                        logger.debug("Failed to end progress", exc_info=True)
                    return

                if throttle_seconds > 0:
                    now = time.time()
                    if (now - state["last_report"]) < throttle_seconds:
                        return
                    state["last_report"] = now

            pct = int(100 * completed / total) if total > 0 else 0
            basename = os.path.basename(current_file) if current_file else ""
            try:
                server.work_done_progress.report(
                    token,
                    lsp.WorkDoneProgressReport(
                        message=f"({completed}/{total}) {basename}",
                        percentage=pct,
                    ),
                )
            except Exception:
                logger.debug("Failed to report progress", exc_info=True)

        return _callback

    def _start_bulk_analysis(self) -> None:
        """Kick off background T1+T2 analysis of all workspace files.

        Called as the ``done_callback`` from :class:`WorkspaceIndexer`
        after Phase 2 (deep indexing) completes.  This runs on a pygls
        thread-pool thread, so we schedule on the event loop via
        ``run_coroutine_threadsafe`` when possible, falling back to a
        plain daemon thread if no loop is available.
        """
        if not get_config().bulk_analysis:
            slog.info(
                "Bulk analysis disabled via IVY_LSP_BULK_ANALYSIS=0",
                extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "bulk_t1t2")},
            )
            return
        if self._analysis_pipeline is None or self._indexer is None:
            return

        include_t2 = get_config().bulk_analysis_t2
        all_files = self._indexer.get_all_ivy_file_paths()
        all_files = self._filter_files_to_workspace(all_files)
        if not all_files:
            return

        progress_cb = self._make_progress_callback(
            "Ivy Background Analysis",
            "Analysing {total} files...",
            "Analysed {total} files",
        )

        def _run():
            try:
                # If semantic model was loaded from offline cache AND all
                # protocol indexes are fresh, skip T1/T2 entirely.
                if getattr(self, "_semantic_model_from_cache", False):
                    ws_ctx = getattr(self, "_workspace_context", None)
                    all_fresh = True
                    if ws_ctx is not None:
                        for proto_idx in ws_ctx.protocol_indexes.values():
                            if proto_idx.staleness.status != "fresh":
                                all_fresh = False
                                break
                    if all_fresh:
                        slog.info(
                            "Cached semantic model is fresh; skipping T1/T2 for %d files",
                            len(all_files),
                            extra={
                                "event": LogEvent(
                                    LogCategory.MILESTONE,
                                    "cached_t1t2_skip",
                                )
                            },
                        )
                        # Skip _write_shared_cache() — data hasn't changed,
                        # avoids unnecessary 113MB serialize + file lock.
                        self._send_model_ready_notification()
                        if progress_cb:
                            progress_cb(len(all_files), len(all_files), None)
                        return
                    else:
                        # Incremental path: hash-validate + cascade expand
                        ws_ctx_inc = getattr(self, "_workspace_context", None)
                        all_dirty: Set[str] = set()
                        total_mtime = 0

                        if ws_ctx_inc is None:
                            slog.info(
                                "No workspace context for incremental analysis; "
                                "falling back to full T1/T2",
                                extra={
                                    "event": LogEvent(
                                        LogCategory.DIAGNOSTIC,
                                        "incremental_no_ctx",
                                    )
                                },
                            )
                            self._analysis_pipeline.reset_tracking()
                        else:
                            for proto_idx in ws_ctx_inc.protocol_indexes.values():
                                if proto_idx.staleness.status == "fresh":
                                    continue
                                protocol_dir = os.path.dirname(proto_idx.index_dir)
                                dirty = _validate_changes(
                                    proto_idx.staleness, protocol_dir
                                )
                                mtime_count = len(proto_idx.staleness.file_changes)
                                total_mtime += mtime_count
                                hash_clean = mtime_count - len(dirty)

                                slog.info(
                                    "Protocol %s: %d mtime-changed, %d hash-dirty, %d hash-clean",
                                    proto_idx.protocol,
                                    mtime_count,
                                    len(dirty),
                                    hash_clean,
                                    extra={
                                        "event": LogEvent(
                                            LogCategory.MILESTONE,
                                            "staleness_detail",
                                            {
                                                "protocol": proto_idx.protocol,
                                                "mtime_changed": mtime_count,
                                                "hash_dirty": len(dirty),
                                                "hash_clean": hash_clean,
                                            },
                                        )
                                    },
                                )
                                all_dirty |= dirty

                        if ws_ctx_inc is not None and not all_dirty:
                            self._incremental_stats = {
                                "incremental_t1t2": True,
                                "outcome": "all_hash_clean",
                                "files_reanalyzed": 0,
                                "files_from_cache": len(all_files),
                            }
                            slog.info(
                                "All %d mtime-changed files are hash-clean; skipping T1/T2",
                                total_mtime,
                                extra={
                                    "event": LogEvent(
                                        LogCategory.MILESTONE,
                                        "incremental_all_clean",
                                    )
                                },
                            )
                            self._send_model_ready_notification()
                            if progress_cb:
                                progress_cb(len(all_files), len(all_files), None)
                            return

                        # Cascade expansion
                        indexer = getattr(self, "_indexer", None)
                        inc_graph = (
                            getattr(indexer, "_include_graph", None)
                            if indexer is not None
                            else None
                        )
                        if inc_graph is not None:
                            expanded = expand_with_budget(
                                all_dirty, inc_graph, len(all_files)
                            )
                        else:
                            expanded = None

                        if expanded is None:
                            self._incremental_stats = {
                                "incremental_t1t2": False,
                                "outcome": "fallback",
                                "files_reanalyzed": len(all_files),
                                "files_from_cache": 0,
                            }
                            slog.info(
                                "Cascade budget exceeded or no include graph; "
                                "falling back to full T1/T2",
                                extra={
                                    "event": LogEvent(
                                        LogCategory.MILESTONE,
                                        "incremental_fallback",
                                    )
                                },
                            )
                            self._analysis_pipeline.reset_tracking()
                        else:
                            slog.info(
                                "Incremental: re-analyzing %d/%d files (%d from cache)",
                                len(expanded),
                                len(all_files),
                                len(all_files) - len(expanded),
                                extra={
                                    "event": LogEvent(
                                        LogCategory.MILESTONE,
                                        "incremental_t1t2",
                                        {
                                            "reanalyzed": len(expanded),
                                            "total": len(all_files),
                                            "from_cache": len(all_files)
                                            - len(expanded),
                                        },
                                    )
                                },
                            )
                            self._incremental_stats = {
                                "incremental_t1t2": True,
                                "outcome": "incremental",
                                "files_reanalyzed": len(expanded),
                                "files_from_cache": len(all_files) - len(expanded),
                                "cascade_expanded": len(expanded) - len(all_dirty),
                            }
                            for filepath in expanded:
                                self._semantic_model.remove_file(filepath)
                            self._analysis_pipeline.invalidate_files(expanded)

                result = self._analysis_pipeline.run_bulk_t1_t2(
                    filepaths=all_files,
                    progress_callback=progress_cb,
                    cancel_event=self._bulk_analysis_cancel,
                    include_t2=include_t2,
                )
                slog.info(
                    "Bulk analysis complete: %d T1, %d T2, %d errors, cancelled=%s",
                    result.t1_completed,
                    result.t2_completed,
                    len(result.errors),
                    result.cancelled,
                    extra={
                        "event": LogEvent(
                            LogCategory.MILESTONE,
                            "bulk_t1t2",
                            {
                                "t1": result.t1_completed,
                                "t2": result.t2_completed,
                                "errors": len(result.errors),
                                "cancelled": result.cancelled,
                            },
                        )
                    },
                )
                self._send_model_ready_notification()
                self._write_shared_cache()
                self._start_bulk_compilation_via_pipeline()
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "shutdown" in msg or "interpreter" in msg:
                    logger.debug("Bulk analysis interrupted by interpreter shutdown")
                else:
                    logger.exception("Bulk analysis failed")
            except Exception:
                if self._shutdown_event.is_set():
                    return
                logger.exception("Bulk analysis failed")

        # Schedule on event loop if available; fall back to daemon thread
        try:
            loop = self.loop  # type: ignore[attr-defined]  # pygls LanguageServer exposes the event loop
            if loop is not None and loop.is_running():
                loop.run_in_executor(None, _run)
                return
        except AttributeError:
            pass

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name="ivy-bulk-analysis",
        )
        thread.start()

    def _send_compilation_progress(
        self, completed: int, total: int, filepath: str, success: bool
    ) -> None:
        """Send ``ivy/compilationProgress`` push notification to the client."""
        if self._shutdown_event.is_set():
            return
        try:
            self.protocol.notify(
                "ivy/compilationProgress",
                {
                    "completed": completed,
                    "total": total,
                    "currentFile": os.path.basename(filepath),
                    "success": success,
                },
            )
        except Exception:
            logger.warning(
                "Failed to send compilationProgress notification",
                exc_info=True,
            )
        if completed >= total:
            self._send_model_ready_notification()

    def _start_bulk_compilation_via_pipeline(self) -> None:
        """Delegate bulk T3 compilation to the analysis pipeline.

        Called after bulk T1+T2 analysis completes.
        """
        if self._analysis_pipeline is None or self._indexer is None:
            return
        if self._compiler_manager is None:
            return
        if not get_config().bulk_compile:
            slog.info(
                "Bulk compilation disabled via IVY_LSP_BULK_COMPILE=0",
                extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "compile_bulk")},
            )
            return

        try:
            graph = self._indexer.requirement_graph
        except AttributeError:
            return

        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel

        if not isinstance(graph, ScopedRequirementModel):
            return

        test_files = graph.list_test_files()
        if not test_files:
            logger.info("No test files found for bulk compilation")
            return

        slog.info(
            "Starting bulk compilation for %d test files",
            len(test_files),
            extra={
                "event": LogEvent(
                    LogCategory.MILESTONE,
                    "compile_bulk",
                    {"test_files": len(test_files)},
                )
            },
        )

        progress_cb = self._make_progress_callback(
            "Ivy Compilation",
            "Compiling {total} test files...",
            "Compiled {total} test files",
            throttle_seconds=1.0,
        )

        self._analysis_pipeline.run_bulk_tier3(
            test_files,
            progress_callback=progress_cb,
            cancel_event=self._bulk_analysis_cancel,
        )
