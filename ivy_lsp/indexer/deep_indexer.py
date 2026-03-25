"""Deep indexing mixin for WorkspaceIndexer.

Extracts background full-parse methods that upgrade shallow-indexed symbols
to AST quality.  All methods operate on ``self`` which is a
:class:`~ivy_lsp.indexer.workspace_indexer.WorkspaceIndexer` instance at
runtime.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any, List, Optional

from ivy_lsp.analysis.light_mode_extractor import (
    extract_exports_imports_light,
    extract_requirements_light,
)
from ivy_lsp.config import get_config
from ivy_lsp.observability import LogCategory, LogEvent
from ivy_lsp.parsing.symbols import IvySymbol, SymbolTable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class DeepIndexMixin:
    """Deep indexing methods: background parse upgrade and on-demand deep parsing."""

    # ------------------------------------------------------------------
    # Phase 2: Background full-parse from test entry points
    # ------------------------------------------------------------------

    def _notify_progress(self) -> None:
        """Invoke the progress callback with current deep-index state.

        Thread-safe: reads progress under lock, calls back outside lock.
        """
        if self._progress_callback is None:
            return
        with self._progress_lock:
            completed = self._deep_index_progress.completed_test_files
            total = self._deep_index_progress.total_test_files
            current = self._deep_index_progress.current_file
        try:
            self._progress_callback(completed, total, current)
        except Exception:
            logger.debug("Progress callback failed", exc_info=True)

    def _deep_index_from_tests(self) -> None:
        """Full-parse from test entry points in a background thread.

        Only files with exports (test files) are parsed.  As each completes,
        its symbols are upgraded to AST quality.  This progressively enriches
        the symbol table while keeping lock contention to a minimum.
        """
        try:
            self._deep_index_from_tests_impl()
        except RuntimeError as e:
            if "shutdown" in str(e).lower() or "interpreter" in str(e).lower():
                return
            logger.exception("Unexpected error in deep indexer thread")
        except Exception:
            if self._stop_requested.is_set():
                return
            logger.exception("Unexpected error in deep indexer thread")

    def _deep_index_from_tests_impl(self) -> None:
        """Inner implementation of deep indexing (separated for clean shutdown)."""
        from ivy_lsp.observability import StructuredLogAdapter

        slog = StructuredLogAdapter(logger, {})

        with self._exports_lock:
            test_files = [
                f for f, info in self._file_export_imports.items() if info.has_exports
            ]
        slog.info(
            "Deep index: %d test entry points out of %d files",
            len(test_files),
            len(self._file_export_imports),
            extra={
                "event": LogEvent(
                    LogCategory.MILESTONE,
                    "deep_index",
                    {
                        "test_files": len(test_files),
                        "total_files": len(self._file_export_imports),
                    },
                )
            },
        )

        with self._progress_lock:
            self._deep_index_progress.total_test_files = len(test_files)
            self._deep_index_progress.started_at = time.time()

        # Signal progress start (0/total)
        self._notify_progress()

        num_workers = get_config().parse_workers
        use_parallel = num_workers != 1 and len(test_files) > 3

        if self._stop_requested.is_set():
            return

        if use_parallel:
            if self._stop_requested.is_set():
                return
            logger.warning(
                "Parallel deep indexing uses light-mode extraction only "
                "(T2/T3 semantic tiers skipped). Set 'ivy.lsp.parseWorkers' "
                "to 1 in VS Code settings for full semantic analysis."
            )
            try:
                self._deep_index_parallel(test_files, num_workers)
            except RuntimeError as e:
                if "shutdown" in str(e).lower():
                    logger.debug("Parallel indexer interrupted by interpreter shutdown")
                    return
                raise
        else:
            self._deep_index_serial(test_files)

        # Re-wire graphs after all upgrades
        if not self._stop_requested.is_set():
            self._wire_requirement_graph()
            self._compute_test_scopes()

        # Release source cache memory now that all phases are done.
        self._clear_source_cache()

        with self._progress_lock:
            self._deep_index_progress.current_file = None
            self._deep_index_running = False
        # Signal progress end (total/total)
        self._notify_progress()
        deep_duration = time.time() - (self._deep_index_progress.started_at or 0.0)
        slog.info(
            "Deep index complete for %d test files",
            len(test_files),
            extra={
                "event": LogEvent(
                    LogCategory.PERFORMANCE,
                    "deep_index",
                    {
                        "test_files": len(test_files),
                        "duration_s": round(deep_duration, 3),
                    },
                )
            },
        )

        if self._done_callback is not None:
            try:
                self._done_callback()
            except Exception:
                logger.warning("Deep index done callback failed", exc_info=True)

    def _deep_index_serial(self, test_files: List[str]) -> None:
        """Serial deep indexing of test files."""
        from ivy_lsp.indexer.workspace_indexer import FileIndexStatus
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols

        for test_file in test_files:
            if self._stop_requested.is_set():
                logger.info(
                    "Deep index interrupted by shutdown after %d files",
                    self._deep_index_progress.completed_test_files,
                )
                break
            file_start = time.time()
            with self._progress_lock:
                self._deep_index_progress.current_file = test_file

            source = self._read_source(test_file)
            if source is None:
                with self._progress_lock:
                    status = self._deep_index_progress.file_statuses.get(
                        test_file, FileIndexStatus(filepath=test_file)
                    )
                    status.deep_parse_attempted = True
                    status.deep_parse_succeeded = False
                    status.parse_error = "Cannot read file"
                    status.parse_duration = time.time() - file_start
                    self._deep_index_progress.file_statuses[test_file] = status
                    self._deep_index_progress.completed_test_files += 1
                self._notify_progress()
                continue

            result = None
            try:
                result = self._parser.parse(source, test_file)
            except Exception:
                logger.info(
                    "Deep index parse failed for %s",
                    test_file,
                    exc_info=True,
                )

            if result is not None and result.success and result.ast is not None:
                ast_symbols = ast_to_symbols(result.ast, test_file, source)
                self._upgrade_file_symbols(test_file, ast_symbols, result)
                # Remove Phase 1 light requirements before adding Phase 2 full
                self._requirement_graph.remove_file(test_file)
                self._extract_file_requirements(test_file, result, source)
                self._extract_file_exports_imports(test_file, result, source)
                # Inline T1+T2+T3 if pipeline is available.
                # Each tier is wrapped individually so a failure in one
                # does not prevent the others from running.
                if self._analysis_pipeline is not None:
                    t1_annotations = None
                    try:
                        t1_annotations = self._analysis_pipeline.run_tier1(
                            source, test_file
                        )
                    except Exception:
                        logger.warning(
                            "Inline T1 failed for %s", test_file, exc_info=True
                        )
                    try:
                        self._analysis_pipeline.run_tier2(
                            source,
                            test_file,
                            parse_result=result,
                            rfc_annotations=t1_annotations,
                        )
                    except Exception:
                        logger.warning(
                            "Inline T2 failed for %s", test_file, exc_info=True
                        )
                    try:
                        self._analysis_pipeline.run_tier3_background(
                            source, test_file, track_state=False
                        )
                    except Exception:
                        logger.warning(
                            "Inline T3 failed for %s", test_file, exc_info=True
                        )
            elif self._analysis_pipeline is not None:
                # For failed parse, still run T1 (regex-only, no AST needed)
                try:
                    self._analysis_pipeline.run_tier1(source, test_file)
                except Exception:
                    logger.debug(
                        "Inline T1 failed for %s",
                        test_file,
                        exc_info=True,
                    )

            with self._progress_lock:
                status = self._deep_index_progress.file_statuses.get(
                    test_file, FileIndexStatus(filepath=test_file)
                )
                status.deep_parse_attempted = True
                status.deep_parse_succeeded = (
                    result is not None and result.success and result.ast is not None
                )
                status.last_indexed_at = time.time()
                status.parse_duration = time.time() - file_start
                if not status.deep_parse_succeeded and result is not None:
                    if hasattr(result, "errors") and result.errors:
                        from ivy_lsp.utils.ivy_output import format_ivy_error

                        err_msg = format_ivy_error(result.errors[0])
                        status.parse_error = err_msg
                        self._index_errors.append(
                            {
                                "uri": test_file,
                                "error": err_msg,
                            }
                        )
                self._deep_index_progress.file_statuses[test_file] = status
                self._deep_index_progress.completed_test_files += 1
            self._notify_progress()

    def _deep_index_parallel(
        self,
        test_files: List[str],
        num_workers: int,
    ) -> None:
        """Parallel deep indexing using ProcessPoolExecutor."""
        from ivy_lsp.indexer.workspace_indexer import FileIndexStatus

        if self._stop_requested.is_set():
            return

        from ivy_lsp.indexer.parallel_indexer import ParallelDeepIndexer
        from ivy_lsp.parsing.symbols import IvySymbol as _IvySymbol

        indexer = ParallelDeepIndexer(
            num_workers=num_workers,
            resolver_config=self._resolver.to_config_dict(),
            stop_event=self._stop_requested,
        )
        results = indexer.parse_files(test_files)

        for filepath, worker_result in results.items():
            if self._stop_requested.is_set():
                logger.info(
                    "Deep index (parallel) interrupted by shutdown after %d files",
                    self._deep_index_progress.completed_test_files,
                )
                break
            with self._progress_lock:
                self._deep_index_progress.current_file = filepath
            if worker_result.success:
                symbols = [_IvySymbol.from_dict(d) for d in worker_result.symbols]
                self._upgrade_file_symbols(filepath, symbols, None)
                # Re-extract requirements and exports with light-mode
                # extractors.  ASTs can't cross process boundaries so
                # full extraction isn't possible, but re-running light
                # mode ensures consistency with the serial path.
                try:
                    source = self._read_source(filepath)
                    if source is None:
                        raise OSError(f"Cannot read {filepath}")
                    # Remove Phase 1 light requirements before re-adding
                    self._requirement_graph.remove_file(filepath)
                    reqs, writes = extract_requirements_light(source, filepath)
                    self._requirement_graph.add_file_requirements(
                        filepath, reqs, writes
                    )
                    info = extract_exports_imports_light(source, filepath)
                    with self._exports_lock:
                        self._file_export_imports[filepath] = info
                    # Inline T1+T2+T3 (same pattern as serial path) so
                    # the subsequent bulk T1+T2 run can skip these files.
                    if self._analysis_pipeline is not None:
                        t1_annotations = None
                        try:
                            t1_annotations = self._analysis_pipeline.run_tier1(
                                source, filepath
                            )
                        except Exception:
                            logger.warning(
                                "Parallel inline T1 failed for %s",
                                filepath,
                                exc_info=True,
                            )
                        try:
                            self._analysis_pipeline.run_tier2(
                                source,
                                filepath,
                                rfc_annotations=t1_annotations,
                            )
                        except Exception:
                            logger.warning(
                                "Parallel inline T2 failed for %s",
                                filepath,
                                exc_info=True,
                            )
                        try:
                            self._analysis_pipeline.run_tier3_background(
                                source, filepath, track_state=False
                            )
                        except Exception:
                            logger.warning(
                                "Parallel inline T3 failed for %s",
                                filepath,
                                exc_info=True,
                            )
                except Exception:
                    logger.debug(
                        "Parallel: light extraction failed for %s",
                        filepath,
                        exc_info=True,
                    )
            elif self._analysis_pipeline is not None:
                # For failed parse, still run T1 (regex-only, no AST needed)
                try:
                    source = self._read_source(filepath)
                    if source is not None:
                        self._analysis_pipeline.run_tier1(source, filepath)
                except Exception:
                    logger.debug(
                        "Parallel inline T1 failed for %s",
                        filepath,
                        exc_info=True,
                    )
            with self._progress_lock:
                status = self._deep_index_progress.file_statuses.get(
                    filepath, FileIndexStatus(filepath=filepath)
                )
                status.deep_parse_attempted = True
                status.deep_parse_succeeded = worker_result.success
                status.last_indexed_at = time.time()
                if not worker_result.success and worker_result.errors:
                    status.parse_error = worker_result.errors[0]
                    self._index_errors.append(
                        {
                            "uri": filepath,
                            "error": status.parse_error,
                        }
                    )
                self._deep_index_progress.file_statuses[filepath] = status
                self._deep_index_progress.completed_test_files += 1
            self._notify_progress()

    def _upgrade_file_symbols(
        self,
        filepath: str,
        new_symbols: List[IvySymbol],
        parse_result: Any,
    ) -> None:
        """Replace fallback-scanned symbols with AST-quality symbols for a file."""
        # The _table_lock serializes concurrent writers (e.g. two
        # _upgrade_file_symbols calls from parallel deep indexing).
        # The assignment to self._symbol_table is atomic at the Python
        # level (GIL), so unsynchronized readers see either the old
        # table or the new one -- never a partially-built one.
        with self._table_lock:
            old_symbols = list(self._symbol_table.all_symbols())
            new_table = SymbolTable()
            for sym in old_symbols:
                if sym.file_path != filepath:
                    new_table.add_symbol(sym)
            for sym in new_symbols:
                new_table.add_symbol(sym)
            self._symbol_table = new_table

        # Update cache - reuse includes from existing cache entry
        cached = self._cache.get(filepath)
        includes = cached.includes if cached else []
        self._cache.put(filepath, parse_result, new_symbols, includes)

    def deep_parse_on_demand(self, filepath: str) -> bool:
        """Parse a file with the full AST parser if it was only shallow-indexed.

        Returns True if the file was upgraded, False if already deep-parsed
        or not eligible.
        """
        from ivy_lsp.observability import StructuredLogAdapter
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols

        slog = StructuredLogAdapter(logger, {})

        abs_path = os.path.abspath(filepath)
        status = self._deep_index_progress.file_statuses.get(abs_path)
        if status is None:
            return False
        if status.deep_parse_attempted:
            return False

        try:
            with open(abs_path) as f:
                source = f.read()
        except OSError:
            return False

        result = self._parser.parse(source, abs_path)
        status.deep_parse_attempted = True
        if not result.success:
            status.deep_parse_succeeded = False
            return False

        symbols = ast_to_symbols(result.ast, abs_path, source)
        status.deep_parse_succeeded = True
        self._upgrade_file_symbols(abs_path, symbols, result)

        slog.info(
            "On-demand deep parse: %s",
            os.path.basename(abs_path),
        )
        return True
