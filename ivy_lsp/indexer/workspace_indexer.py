"""Workspace-wide Ivy file indexer and cross-file symbol lookup."""

from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ivy_lsp.analysis.light_mode_extractor import (
    extract_exports_imports_light,
    extract_requirements_light,
)
from ivy_lsp.analysis.requirement_extractor import (
    extract_exports_imports_full,
    extract_requirements_full,
)
from ivy_lsp.analysis.test_scope import (
    ExportImportInfo,
    ScopedRequirementModel,
    TestScope,
    detect_test_role,
)
from ivy_lsp.indexer.file_cache import FileCache
from ivy_lsp.indexer.include_resolver import IncludeResolver
from ivy_lsp.parsing.symbols import IncludeGraph, IvySymbol, SymbolTable
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


def _try_tokenize(source: str, filepath: str) -> Optional[Any]:
    """Best-effort tokenization for shared token stream caching.

    Returns a TokenStream or None if the lexer is unavailable.
    Each consumer (fallback_scan, extract_requirements_light,
    extract_exports_imports_light) falls back to its own tokenization
    when None is passed.
    """
    try:
        from ivy_lsp.parsing.token_stream import tokenize_ivy

        return tokenize_ivy(source, filepath)
    except ImportError:
        return None
    except Exception:
        logger.debug("Tokenization failed for %s", filepath, exc_info=True)
        return None


@dataclass
class SymbolLocation:
    """A symbol together with its source file and range."""

    symbol: IvySymbol
    filepath: str
    range: Tuple[int, int, int, int]


@dataclass
class IndexerStats:
    """Snapshot of indexer statistics for monitoring."""

    file_count: int = 0
    symbol_count: int = 0
    include_edge_count: int = 0
    test_scope_count: int = 0
    per_file_errors: List[Dict[str, str]] = field(default_factory=list)
    stale_files: List[str] = field(default_factory=list)
    last_index_time: Optional[str] = None
    last_index_duration: Optional[float] = None


@dataclass
class FileIndexStatus:
    """Per-file index depth tracking."""

    filepath: str
    shallow_indexed: bool = False
    deep_parse_attempted: bool = False
    deep_parse_succeeded: bool = False
    last_indexed_at: Optional[float] = None
    parse_error: Optional[str] = None
    parse_duration: Optional[float] = None


@dataclass
class DeepIndexProgress:
    """Progress of background deep indexing."""

    total_test_files: int = 0
    completed_test_files: int = 0
    current_file: Optional[str] = None
    started_at: Optional[float] = None
    file_statuses: Dict[str, FileIndexStatus] = field(default_factory=dict)


class WorkspaceIndexer:
    """Central cross-file index for the Ivy workspace.

    Maintains a :class:`SymbolTable` and :class:`IncludeGraph` spanning
    every ``.ivy`` file discovered by the :class:`IncludeResolver`.
    Supports incremental re-indexing of individual files and
    cross-file symbol lookup with transitive include scoping.
    """

    def __init__(
        self,
        workspace_root: str,
        parser: Any,
        resolver: IncludeResolver,
        persistent_cache: bool = False,
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]] = None,
        done_callback: Optional[Callable[[], None]] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        """Initialize indexer with parser, resolver, and empty indices."""
        self._workspace_root = os.path.abspath(workspace_root)
        self._parser = parser
        self._resolver = resolver
        self._progress_callback = progress_callback
        self._done_callback = done_callback
        if persistent_cache:
            from ivy_lsp.indexer.file_cache import PersistentFileCache

            self._cache = PersistentFileCache(workspace_root, cache_dir=cache_dir)
        else:
            self._cache = FileCache()
        self._symbol_table = SymbolTable()
        self._table_lock = threading.Lock()  # guards _symbol_table swaps
        self._include_graph = IncludeGraph()
        self._requirement_graph = ScopedRequirementModel()
        self._file_export_imports: Dict[str, ExportImportInfo] = {}
        self._exports_lock = threading.Lock()
        self._index_errors: List[Dict[str, str]] = []
        self._last_index_duration: Optional[float] = None
        self._last_index_time: Optional[float] = None
        self._deep_index_running = False
        self._deep_index_progress = DeepIndexProgress()
        self._progress_lock = threading.Lock()
        self._stop_requested = threading.Event()
        # Use weakref to avoid preventing GC of the indexer.
        # atexit.register(self._stop_requested.set) retains a strong
        # reference to the bound method (and thus the Event and the
        # WorkspaceIndexer), keeping the entire object tree alive until
        # interpreter shutdown.
        stop_ref = weakref.ref(self._stop_requested)
        atexit.register(lambda: (e := stop_ref()) is not None and e.set())
        self._analysis_pipeline: Optional[Any] = None
        # Mtime-validated source cache to avoid re-reading files across
        # pipeline phases.  Maps filepath -> (content, mtime).
        # Cleared after the full pipeline completes.
        self._source_cache: Dict[str, Tuple[str, float]] = {}
        self._source_cache_lock = threading.Lock()

    # -- Public accessors (Phase 3.2) ----------------------------------------

    @property
    def requirement_graph(self):
        """Public accessor for the requirement graph."""
        return self._requirement_graph

    @property
    def include_graph(self):
        """Public accessor for the include graph."""
        return self._include_graph

    @property
    def resolver(self):
        """Public accessor for the include resolver."""
        return self._resolver

    def lookup_all_symbols(self):
        """Return all symbols in the symbol table."""
        with self._table_lock:
            return self._symbol_table.all_symbols()

    def lookup_qualified_symbols(self, name: str):
        """Lookup symbols by qualified name."""
        with self._table_lock:
            results = self._symbol_table.lookup_qualified(name)
            if not results:
                results = self._symbol_table.lookup(name)
            return results

    def get_deep_index_progress(self):
        """Thread-safe snapshot of deep indexing progress.

        Returns a dict with all progress fields captured under
        ``_progress_lock`` so callers never see a mix of old/new values.
        """
        with self._progress_lock:
            p = self._deep_index_progress
            return {
                "running": self._deep_index_running,
                "total_test_files": p.total_test_files,
                "completed_test_files": p.completed_test_files,
                "current_file": p.current_file,
                "started_at": p.started_at,
                "file_status_count": len(p.file_statuses),
                "file_statuses": dict(p.file_statuses),
            }

    def get_file_export_imports(self):
        """Thread-safe snapshot of file export/import info."""
        with self._exports_lock:
            return dict(self._file_export_imports)

    def get_cached_file(self, filepath: str):
        """Get cached file data by path."""
        return self._cache.get(filepath) if self._cache else None

    # -- Lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Signal background threads to stop at the next safe point."""
        self._stop_requested.set()

    def set_analysis_pipeline(self, pipeline: Any) -> None:
        """Inject the analysis pipeline for inline T1/T2 during deep indexing."""
        self._analysis_pipeline = pipeline

    def _read_source(self, filepath: str) -> Optional[str]:
        """Read file content with mtime-validated caching.

        Returns the file content or None if the file cannot be read.
        Cached results are validated against the file's mtime to ensure
        freshness.  Thread-safe.
        """
        try:
            current_mtime = os.path.getmtime(filepath)
        except OSError:
            return None

        with self._source_cache_lock:
            cached = self._source_cache.get(filepath)
            if cached is not None:
                content, cached_mtime = cached
                if cached_mtime == current_mtime:
                    return content

        try:
            with open(filepath) as f:
                content = f.read()
        except OSError:
            return None

        with self._source_cache_lock:
            self._source_cache[filepath] = (content, current_mtime)
        return content

    def _clear_source_cache(self) -> None:
        """Clear the source content cache after pipeline completion."""
        with self._source_cache_lock:
            self._source_cache.clear()

    # ------------------------------------------------------------------
    # Full workspace indexing (two-mode: fast scan + background deep parse)
    # ------------------------------------------------------------------

    def index_workspace(self) -> None:
        """Index the workspace in two phases for responsiveness.

        Phase 1 (synchronous, fast): lexer-only scan of every ``.ivy`` file.
        Populates the symbol table with degraded but usable symbols,
        builds the include graph, extracts requirements and
        export/import info with light-mode extractors.
        The server is marked "ready" immediately after this phase.

        Phase 2 (background, progressive): full-parse ONLY from test entry
        points (files with exports).  Runs in a daemon thread.  As each
        test file completes, its symbols are upgraded with AST-quality data
        under ``_table_lock``.
        """
        start = time.time()
        self._index_errors = []
        self._symbol_table = SymbolTable()
        self._include_graph = IncludeGraph()
        self._requirement_graph = ScopedRequirementModel()
        self._file_export_imports = {}
        with self._progress_lock:
            self._deep_index_running = False

        # Phase 1: fast lexer-only scan (no lock needed)
        self._fast_index_all_files()

        # Post-indexing wiring
        self._wire_requirement_graph()
        self._load_requirement_manifests()
        self._wire_coverage_edges()
        self._compute_test_scopes()
        self._last_index_duration = time.time() - start
        self._last_index_time = time.time()

        # Phase 2: background full-parse from test entry points
        # Only run deep indexing with the real Ivy parser (IvyParserWrapper),
        # not the FallbackOnlyParser which always returns success=False.
        from ivy_lsp.parsing.fallback_parser import FallbackOnlyParser

        has_full_parser = not isinstance(self._parser, FallbackOnlyParser)
        if has_full_parser:
            with self._progress_lock:
                self._deep_index_running = True
            t = threading.Thread(
                target=self._deep_index_from_tests,
                daemon=True,
                name="ivy-deep-index",
            )
            t.start()

    # ------------------------------------------------------------------
    # Phase 1: Fast lexer-only scan
    # ------------------------------------------------------------------

    def _fast_index_all_files(self) -> None:
        """Scan every .ivy file using the fallback lexer scanner.

        Completes in seconds (no background lock contention).
        Provides usable symbols for completion, navigation, and document
        outline immediately.

        When ``IVY_LSP_FAST_INDEX_WORKERS`` > 1 (default 4), file scanning
        runs in a thread pool for faster I/O throughput.  All shared-state
        mutations are performed on the calling thread after collection.
        """
        from ivy_lsp.parsing.fallback_scanner import fallback_scan

        files = self._resolver.find_all_ivy_files()
        num_workers = int(os.environ.get("IVY_LSP_FAST_INDEX_WORKERS", "4"))

        if num_workers > 1 and len(files) > 5:
            self._fast_index_parallel(files, num_workers, fallback_scan)
        else:
            self._fast_index_sequential(files, fallback_scan)

    def _fast_index_sequential(
        self,
        files: List[str],
        fallback_scan: Any,
    ) -> None:
        """Sequential fast index (original path)."""
        for filepath in files:
            cached = self._cache.get(filepath)
            if cached is not None:
                for sym in cached.symbols:
                    self._symbol_table.add_symbol(sym)
                for inc_name in cached.includes:
                    resolved = self._resolver.resolve(inc_name, filepath)
                    if resolved:
                        self._include_graph.add_edge(filepath, resolved)
                with self._progress_lock:
                    self._deep_index_progress.file_statuses[filepath] = FileIndexStatus(
                        filepath=filepath,
                        shallow_indexed=True,
                        last_indexed_at=time.time(),
                    )
                # Restore requirements from cache if available
                if cached.requirements:
                    self._requirement_graph.add_file_requirements(
                        filepath, cached.requirements, cached.writes
                    )
                    if cached.export_import_info is not None:
                        with self._exports_lock:
                            self._file_export_imports[filepath] = (
                                cached.export_import_info
                            )
                    continue
                # Fallback: re-extract if cache entry predates this feature
                source = self._read_source(filepath)
                if source is None:
                    continue
                # Tokenize once for requirement + export extraction
                stream = _try_tokenize(source, filepath)
                reqs, writes = extract_requirements_light(
                    source, filepath, token_stream=stream
                )
                self._requirement_graph.add_file_requirements(filepath, reqs, writes)
                info = extract_exports_imports_light(
                    source, filepath, token_stream=stream
                )
                with self._exports_lock:
                    self._file_export_imports[filepath] = info
                continue

            source = self._read_source(filepath)
            if source is None:
                logger.warning("Cannot read %s; file will not be indexed", filepath)
                continue

            # Tokenize once for symbol scanning + requirement + export extraction
            stream = _try_tokenize(source, filepath)
            symbols, _error_info = fallback_scan(source, filepath, token_stream=stream)
            includes = self._extract_includes(source)

            with self._progress_lock:
                self._deep_index_progress.file_statuses[filepath] = FileIndexStatus(
                    filepath=filepath,
                    shallow_indexed=True,
                    last_indexed_at=time.time(),
                )

            for sym in symbols:
                self._symbol_table.add_symbol(sym)

            for inc_name in includes:
                resolved = self._resolver.resolve(inc_name, filepath)
                if resolved:
                    self._include_graph.add_edge(filepath, resolved)

            reqs, writes = extract_requirements_light(
                source, filepath, token_stream=stream
            )
            self._requirement_graph.add_file_requirements(filepath, reqs, writes)

            info = extract_exports_imports_light(source, filepath, token_stream=stream)
            with self._exports_lock:
                self._file_export_imports[filepath] = info

            # Cache after extraction so requirements/exports are stored too
            self._cache.put(
                filepath,
                None,
                symbols,
                includes,
                requirements=reqs,
                writes=writes,
                export_import_info=info,
            )

            slog.debug(
                "Indexed %s",
                filepath,
                extra={"event": LogEvent(LogCategory.ACTIVITY, "indexing")},
            )

    def _fast_index_parallel(
        self,
        files: List[str],
        num_workers: int,
        fallback_scan: Any,
    ) -> None:
        """Parallel fast index: scan in threads, merge on main thread.

        Thread workers perform I/O (file reads) and CPU-bound scanning
        (PLY lexer + regex extraction) without mutating shared state.
        The calling thread merges results into the symbol table, include
        graph, and requirement graph sequentially.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _process_file(
            filepath: str,
        ) -> Tuple[
            str,
            bool,
            Optional[list],
            Optional[List[str]],
            Optional[str],
            Optional[tuple],
            Optional[Any],
        ]:
            """Thread worker: read + scan + light extract.  No shared state mutation."""
            cached = self._cache.get(filepath)
            if cached is not None:
                # Restore requirements from cache if available
                if cached.requirements:
                    return (
                        filepath,
                        True,
                        cached.symbols,
                        cached.includes,
                        None,
                        (cached.requirements, cached.writes),
                        cached.export_import_info,
                    )
                # Fallback: re-extract if cache entry predates this feature
                source = self._read_source(filepath)
                reqs_writes = None
                info = None
                if source is not None:
                    # Tokenize once for requirement + export extraction
                    stream = _try_tokenize(source, filepath)
                    reqs, writes = extract_requirements_light(
                        source, filepath, token_stream=stream
                    )
                    reqs_writes = (reqs, writes)
                    info = extract_exports_imports_light(
                        source, filepath, token_stream=stream
                    )
                return (
                    filepath,
                    True,
                    cached.symbols,
                    cached.includes,
                    source,
                    reqs_writes,
                    info,
                )

            source = self._read_source(filepath)
            if source is None:
                return filepath, False, None, None, None, None, None

            # Tokenize once for symbol scanning + requirement + export extraction
            stream = _try_tokenize(source, filepath)
            symbols, _error_info = fallback_scan(source, filepath, token_stream=stream)
            includes = self._extract_includes(source)
            reqs, writes = extract_requirements_light(
                source, filepath, token_stream=stream
            )
            info = extract_exports_imports_light(source, filepath, token_stream=stream)
            return filepath, False, symbols, includes, source, (reqs, writes), info

        # Collect results from thread pool
        results = []
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = {pool.submit(_process_file, fp): fp for fp in files}
            for future in as_completed(futures):
                results.append(future.result())

        # Merge on main thread (all shared-state mutations)
        for (
            filepath,
            from_cache,
            symbols,
            includes,
            _source,
            reqs_writes,
            info,
        ) in results:
            if symbols is None and not from_cache:
                logger.warning("Cannot read %s; file will not be indexed", filepath)
                continue

            if not from_cache and symbols is not None:
                reqs_data = reqs_writes if reqs_writes is not None else ([], [])
                self._cache.put(
                    filepath,
                    None,
                    symbols,
                    includes or [],
                    requirements=reqs_data[0],
                    writes=reqs_data[1],
                    export_import_info=info,
                )

            with self._progress_lock:
                self._deep_index_progress.file_statuses[filepath] = FileIndexStatus(
                    filepath=filepath,
                    shallow_indexed=True,
                    last_indexed_at=time.time(),
                )

            if symbols is not None:
                for sym in symbols:
                    self._symbol_table.add_symbol(sym)

            if includes is not None:
                for inc_name in includes:
                    resolved = self._resolver.resolve(inc_name, filepath)
                    if resolved:
                        self._include_graph.add_edge(filepath, resolved)

            if reqs_writes is not None:
                reqs, writes = reqs_writes
                self._requirement_graph.add_file_requirements(filepath, reqs, writes)

            if info is not None:
                with self._exports_lock:
                    self._file_export_imports[filepath] = info

            slog.debug(
                "Indexed %s",
                filepath,
                extra={"event": LogEvent(LogCategory.ACTIVITY, "indexing")},
            )

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

        num_workers = int(os.environ.get("IVY_LSP_PARSE_WORKERS", "0"))
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
        if self._stop_requested.is_set():
            return

        from ivy_lsp.indexer.parallel_indexer import ParallelDeepIndexer
        from ivy_lsp.parsing.symbols import IvySymbol

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
                symbols = [IvySymbol.from_dict(d) for d in worker_result.symbols]
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
        # table or the new one — never a partially-built one.
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

    # ------------------------------------------------------------------
    # Single-file indexing (used by reindex_file for incremental updates)
    # ------------------------------------------------------------------

    def _index_single_file(self, filepath: str) -> List[IvySymbol]:
        """Parse and index one file, using the cache when possible."""
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
        from ivy_lsp.parsing.fallback_scanner import fallback_scan

        cached = self._cache.get(filepath)
        if cached is not None:
            return cached.symbols

        try:
            with open(filepath) as f:
                source = f.read()
        except OSError:
            logger.warning("Cannot read %s; file will not be indexed", filepath)
            return []

        result = self._parser.parse(source, filepath)
        if result.success:
            symbols = ast_to_symbols(result.ast, filepath, source)
        else:
            symbols, _error_info = fallback_scan(source, filepath)

        includes = self._extract_includes(source)
        self._cache.put(filepath, result, symbols, includes)

        for sym in symbols:
            self._symbol_table.add_symbol(sym)

        for inc_name in includes:
            resolved = self._resolver.resolve(inc_name, filepath)
            if resolved:
                self._include_graph.add_edge(filepath, resolved)

        # Requirement extraction
        self._extract_file_requirements(filepath, result, source)

        # Export/import extraction
        self._extract_file_exports_imports(filepath, result, source)

        return symbols

    # ------------------------------------------------------------------
    # Include extraction
    # ------------------------------------------------------------------

    def _extract_includes(self, source: str) -> List[str]:
        """Return bare include names from ``include <name>`` directives."""
        return re.findall(r"^include\s+(\w+)", source, re.MULTILINE)

    # ------------------------------------------------------------------
    # Incremental re-indexing
    # ------------------------------------------------------------------

    def _get_compiler_manager(self) -> Optional[Any]:
        """Return the compiler manager from the analysis pipeline, if available."""
        pipeline = self._analysis_pipeline
        if pipeline is not None:
            mgr = getattr(pipeline, "_compiler_manager", None)
            return mgr
        return None

    def reindex_file(self, filepath: str) -> None:
        """Re-index a single file after it has been modified on disk."""
        abs_path = os.path.abspath(filepath)
        self._remove_file_symbols(abs_path)
        self._requirement_graph.remove_file(abs_path)
        self._requirement_graph.invalidate_file(abs_path)
        with self._exports_lock:
            self._file_export_imports.pop(abs_path, None)
        self._cache.invalidate(abs_path)
        self._cache.invalidate_dependents(abs_path, self._include_graph)
        # Invalidate compiler cache so stale compilation results are purged
        compiler_mgr = self._get_compiler_manager()
        if compiler_mgr is not None:
            compiler_mgr.invalidate_dependents(abs_path, self._include_graph)
        self._index_single_file(abs_path)
        self._wire_requirement_graph()
        self._compute_test_scopes()

    def reindex_file_with_dependents(self, filepath: str) -> None:
        """Re-index a file and all files that transitively depend on it."""
        abs_path = os.path.abspath(filepath)
        # BFS to collect all transitive dependents
        dirty = {abs_path}
        queue = [abs_path]
        while queue:
            current = queue.pop(0)
            for dep in self._include_graph.get_included_by(current):
                if dep not in dirty:
                    dirty.add(dep)
                    queue.append(dep)

        # Invalidate compiler cache for all dirty files
        compiler_mgr = self._get_compiler_manager()
        if compiler_mgr is not None:
            for f in dirty:
                compiler_mgr.invalidate(f)

        # Re-index each dirty file
        for f in dirty:
            self._remove_file_symbols(f)
            self._cache.invalidate(f)
            self._index_single_file(f)

        self._wire_requirement_graph()
        self._compute_test_scopes()

    def _remove_file_symbols(self, filepath: str) -> None:
        """Rebuild the symbol table excluding all symbols from *filepath*."""
        with self._table_lock:
            old_symbols = list(self._symbol_table.all_symbols())
            new_table = SymbolTable()
            for sym in old_symbols:
                if sym.file_path != filepath:
                    new_table.add_symbol(sym)
            self._symbol_table = new_table

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_symbols(self, filepath: str) -> List[IvySymbol]:
        """Return symbols for *filepath*, preferring the cache."""
        abs_path = os.path.abspath(filepath)
        cached = self._cache.get(abs_path)
        if cached:
            return cached.symbols
        return self._symbol_table.symbols_in_file(abs_path)

    def lookup_symbol(self, name: str) -> List[SymbolLocation]:
        """Look up a symbol by name across the entire workspace.

        Uses :meth:`SymbolTable.lookup_qualified` for dotted names,
        :meth:`SymbolTable.lookup` otherwise.
        """
        if "." in name:
            symbols = self._symbol_table.lookup_qualified(name)
        else:
            symbols = self._symbol_table.lookup(name)
        return [
            SymbolLocation(
                symbol=sym,
                filepath=sym.file_path or "",
                range=sym.range,
            )
            for sym in symbols
        ]

    def get_symbols_in_scope(self, filepath: str) -> List[IvySymbol]:
        """Return own symbols plus transitive include symbols for *filepath*."""
        abs_path = os.path.abspath(filepath)
        own_symbols = list(self._symbol_table.symbols_in_file(abs_path))
        transitive = self._include_graph.get_transitive_includes(abs_path)
        for included_file in transitive:
            own_symbols.extend(self._symbol_table.symbols_in_file(included_file))
        return own_symbols

    # ------------------------------------------------------------------
    # Full re-index and stats
    # ------------------------------------------------------------------

    def get_all_ivy_file_paths(self) -> List[str]:
        """Return all ``.ivy`` file paths known to the resolver."""
        return self._resolver.find_all_ivy_files()

    def reindex(self) -> None:
        """Clear caches and fully re-index the workspace."""
        self._cache = FileCache()
        self.index_workspace()

    def detect_stale_files(self) -> List[str]:
        """Return indexed file paths that no longer exist on disk."""
        stale = []
        for filepath in list(self._symbol_table._by_file):
            if not os.path.exists(filepath):
                stale.append(filepath)
        return stale

    def get_stats(self) -> IndexerStats:
        """Return a snapshot of current indexer statistics."""
        all_symbols = len(self._symbol_table._all)
        file_count = len(self._symbol_table._by_file)

        # Count include edges from the _includes adjacency dict
        edge_count = sum(
            len(targets) for targets in list(self._include_graph._includes.values())
        )

        test_scope_count = len(getattr(self._requirement_graph, "_test_scopes", {}))

        last_time_str = None
        if self._last_index_time is not None:
            last_time_str = datetime.fromtimestamp(
                self._last_index_time, tz=timezone.utc
            ).isoformat()

        return IndexerStats(
            file_count=file_count,
            symbol_count=all_symbols,
            include_edge_count=edge_count,
            test_scope_count=test_scope_count,
            per_file_errors=list(self._index_errors),
            stale_files=self.detect_stale_files(),
            last_index_time=last_time_str,
            last_index_duration=self._last_index_duration,
        )

    # ------------------------------------------------------------------
    # Requirement graph
    # ------------------------------------------------------------------

    def _extract_file_requirements(
        self, filepath: str, result: Any, source: str
    ) -> None:
        """Extract requirements from a single file and add to the graph."""
        try:
            if result.success:
                reqs, writes = extract_requirements_full(result.ast, filepath, source)
            else:
                reqs, writes = extract_requirements_light(source, filepath)
            self._requirement_graph.add_file_requirements(filepath, reqs, writes)
        except Exception:
            logger.warning(
                "Requirement extraction failed for %s", filepath, exc_info=True
            )

    def _extract_file_exports_imports(
        self, filepath: str, result: Any, source: str
    ) -> None:
        """Extract export/import declarations and store in the index."""
        try:
            if result.success:
                info = extract_exports_imports_full(result.ast, filepath, source)
            else:
                info = extract_exports_imports_light(source, filepath)
            with self._exports_lock:
                self._file_export_imports[filepath] = info
        except Exception:
            logger.warning(
                "Export/import extraction failed for %s",
                filepath,
                exc_info=True,
            )

    def _wire_requirement_graph(self) -> None:
        """Wire requirement graph: populate nodes, wire edges."""
        from lsprotocol.types import SymbolKind

        all_symbols = self._symbol_table.all_symbols()

        # Populate ActionNodes from symbol table + requirement references
        self._requirement_graph.populate_actions_from_symbols(all_symbols)

        # Build known_vars set from existing state vars + symbol table
        known_vars = self._requirement_graph.get_all_state_var_names()
        for sym in all_symbols:
            if sym.kind in (SymbolKind.Variable, SymbolKind.Function):
                known_vars.add(sym.name)

        # Populate StateVarNodes
        self._requirement_graph.populate_state_vars(known_vars, all_symbols)

        # Clear stale wiring edges before re-wiring
        self._requirement_graph.clear_wiring_edges()

        # Wire edges
        self._requirement_graph.wire_state_var_edges(known_vars)
        self._requirement_graph.wire_dependency_edges()

        # Wire cross-file propagation edges
        self._requirement_graph.wire_propagation_edges(self._include_graph)

    def _load_requirement_manifests(self) -> None:
        """Load RFC requirement manifests from the workspace and add to the graph."""
        from ivy_lsp.semantic.rfc_annotations import (
            find_manifests,
            load_requirement_manifest,
        )

        manifests = find_manifests(self._workspace_root)
        for path in manifests:
            reqs = load_requirement_manifest(path)
            for req in reqs.values():
                self._requirement_graph.add_rfc_requirement(req)

    def _wire_coverage_edges(self) -> None:
        """Wire COVERS edges from requirement bracket tags to RFC requirements."""
        self._requirement_graph.wire_coverage_edges()

    def _compute_test_scopes(self) -> None:
        """Build a TestScope for each file that has exports and register it."""
        with self._exports_lock:
            export_items = list(self._file_export_imports.items())
        for filepath, info in export_items:
            if not info.has_exports:
                continue

            closure = {filepath}
            closure |= self._include_graph.get_transitive_includes(filepath)

            all_exports: list = []
            all_imports: list = []
            for f in closure:
                f_info = self._file_export_imports.get(f)
                if f_info is not None:
                    all_exports.extend(f_info.exports)
                    all_imports.extend(f_info.imports)

            frozen_closure = frozenset(closure)
            scope = TestScope(
                test_file=filepath,
                include_closure=frozen_closure,
                exported_actions=frozenset(all_exports),
                imported_actions=frozenset(all_imports),
                tester_role=detect_test_role(frozen_closure),
            )
            self._requirement_graph.register_test_scope(scope)
