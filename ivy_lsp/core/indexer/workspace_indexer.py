"""Workspace-wide Ivy file indexer and cross-file symbol lookup."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ivy_lsp.core.analysis.light_mode_extractor import (
    extract_exports_imports_light,
    extract_requirements_light,
)
from ivy_lsp.core.analysis.test_scope import ExportImportInfo, ScopedRequirementModel
from ivy_lsp.core.indexer.deep_indexer import DeepIndexMixin
from ivy_lsp.core.indexer.file_cache import FileCache
from ivy_lsp.core.indexer.include_resolver import IncludeResolver
from ivy_lsp.core.indexer.scope_manager import ScopeManagerMixin
from ivy_lsp.core.parsing.symbols import IncludeGraph, IvySymbol, SymbolTable
from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter

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
        from ivy_lsp.core.parsing.token_stream import tokenize_ivy

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


class WorkspaceIndexer(DeepIndexMixin, ScopeManagerMixin):
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
            from ivy_lsp.core.indexer.file_cache import PersistentFileCache

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
        # Cache for mirror-scope symbol lookups (filepath → list of symbols).
        # Selectively invalidated when dirty_files is provided to
        # _compute_test_scopes; fully cleared on initial/full rebuilds.
        self._mirror_scope_cache: Dict[str, List[IvySymbol]] = {}

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
        self._mirror_scope_cache = {}
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
        from ivy_lsp.core.parsing.fallback_parser import FallbackOnlyParser

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
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        files = self._resolver.find_all_ivy_files()
        num_workers = get_config().fast_index_workers

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
                if sym.detail != "include":
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
                    if sym.detail != "include":
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
    # Single-file indexing (used by reindex_file for incremental updates)
    # ------------------------------------------------------------------

    def _index_single_file(self, filepath: str) -> List[IvySymbol]:
        """Parse and index one file, using the cache when possible."""
        from ivy_lsp.core.parsing.ast_to_symbols import ast_to_symbols
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

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
            if sym.detail != "include":
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
        :meth:`SymbolTable.lookup` otherwise.  Falls back to
        :meth:`SymbolTable.lookup_unqualified` for plain names that
        are only registered as children (e.g. nested object members).
        """
        if "." in name:
            symbols = self._symbol_table.lookup_qualified(name)
        else:
            symbols = self._symbol_table.lookup(name)
            if not symbols:
                symbols = self._symbol_table.lookup_unqualified(name)
        return [
            SymbolLocation(
                symbol=sym,
                filepath=sym.file_path or "",
                range=sym.range,
            )
            for sym in symbols
        ]

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
