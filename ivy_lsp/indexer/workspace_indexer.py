"""Workspace-wide Ivy file indexer and cross-file symbol lookup."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._workspace_root = os.path.abspath(workspace_root)
        self._parser = parser
        self._resolver = resolver
        self._cache = FileCache()
        self._symbol_table = SymbolTable()
        self._include_graph = IncludeGraph()
        self._requirement_graph = ScopedRequirementModel()
        self._file_export_imports: Dict[str, ExportImportInfo] = {}
        self._index_errors: List[Dict[str, str]] = []
        self._last_index_duration: Optional[float] = None
        self._last_index_time: Optional[float] = None
        self._deep_index_running = False

    # ------------------------------------------------------------------
    # Full workspace indexing (two-mode: fast scan + background deep parse)
    # ------------------------------------------------------------------

    def index_workspace(self) -> None:
        """Index the workspace in two phases for responsiveness.

        Phase 1 (synchronous, fast): lexer-only scan of every ``.ivy`` file.
        No ``_ivy_state_lock`` is needed.  Populates the symbol table with
        degraded but usable symbols, builds the include graph, extracts
        requirements and export/import info with light-mode extractors.
        The server is marked "ready" immediately after this phase.

        Phase 2 (background, progressive): full-parse ONLY from test entry
        points (files with exports).  Runs in a daemon thread.  As each
        test file completes, its symbols are upgraded with AST-quality data.
        Lock contention is minimized because only ~10-20 files are parsed
        instead of all 238+.
        """
        start = time.time()
        self._index_errors = []
        self._symbol_table = SymbolTable()
        self._include_graph = IncludeGraph()
        self._requirement_graph = ScopedRequirementModel()
        self._file_export_imports = {}
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
            self._deep_index_running = True
            t = threading.Thread(
                target=self._deep_index_from_tests,
                daemon=True,
                name="ivy-deep-index",
            )
            t.start()

    # ------------------------------------------------------------------
    # Phase 1: Fast lexer-only scan (no _ivy_state_lock)
    # ------------------------------------------------------------------

    def _fast_index_all_files(self) -> None:
        """Scan every .ivy file using the fallback lexer scanner.

        This does NOT acquire ``_ivy_state_lock`` and completes in seconds.
        Provides usable symbols for completion, navigation, and document
        outline immediately.
        """
        from ivy_lsp.parsing.fallback_scanner import fallback_scan

        files = self._resolver.find_all_ivy_files()
        for filepath in files:
            try:
                with open(filepath) as f:
                    source = f.read()
            except OSError:
                logger.warning(
                    "Cannot read %s; file will not be indexed", filepath
                )
                continue

            symbols, _error_info = fallback_scan(source, filepath)
            includes = self._extract_includes(source)
            self._cache.put(filepath, None, symbols, includes)

            for sym in symbols:
                self._symbol_table.add_symbol(sym)

            for inc_name in includes:
                resolved = self._resolver.resolve(inc_name, filepath)
                if resolved:
                    self._include_graph.add_edge(filepath, resolved)

            # Light-mode requirement extraction (regex, no lock)
            reqs, writes = extract_requirements_light(source, filepath)
            self._requirement_graph.add_file_requirements(
                filepath, reqs, writes
            )

            # Light-mode export/import extraction (regex, no lock)
            info = extract_exports_imports_light(source, filepath)
            self._file_export_imports[filepath] = info

    # ------------------------------------------------------------------
    # Phase 2: Background full-parse from test entry points
    # ------------------------------------------------------------------

    def _deep_index_from_tests(self) -> None:
        """Full-parse from test entry points in a background thread.

        Only files with exports (test files) are parsed.  As each completes,
        its symbols are upgraded to AST quality.  This progressively enriches
        the symbol table while keeping lock contention to a minimum.
        """
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols

        test_files = [
            f
            for f, info in self._file_export_imports.items()
            if info.has_exports
        ]
        logger.info(
            "Deep index: %d test entry points out of %d files",
            len(test_files),
            len(self._file_export_imports),
        )

        for test_file in test_files:
            try:
                with open(test_file) as f:
                    source = f.read()
            except OSError:
                continue

            try:
                result = self._parser.parse(source, test_file)
            except Exception:
                logger.debug(
                    "Deep index parse failed for %s",
                    test_file,
                    exc_info=True,
                )
                continue

            if result.success and result.ast is not None:
                # Upgrade symbols for this file
                ast_symbols = ast_to_symbols(result.ast, test_file, source)
                self._upgrade_file_symbols(test_file, ast_symbols, result)

                # Upgrade requirement/export extraction with full parser data
                self._extract_file_requirements(test_file, result, source)
                self._extract_file_exports_imports(test_file, result, source)

        # Re-wire graphs after all upgrades
        self._wire_requirement_graph()
        self._compute_test_scopes()
        self._deep_index_running = False
        logger.info("Deep index complete for %d test files", len(test_files))

    def _upgrade_file_symbols(
        self,
        filepath: str,
        new_symbols: List[IvySymbol],
        parse_result: Any,
    ) -> None:
        """Replace fallback-scanned symbols with AST-quality symbols for a file."""
        # Remove old symbols for this file
        old_symbols = list(self._symbol_table.all_symbols())
        self._symbol_table = SymbolTable()
        for sym in old_symbols:
            if sym.file_path != filepath:
                self._symbol_table.add_symbol(sym)

        # Add new AST-quality symbols
        for sym in new_symbols:
            self._symbol_table.add_symbol(sym)

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

    def reindex_file(self, filepath: str) -> None:
        """Re-index a single file after it has been modified on disk."""
        abs_path = os.path.abspath(filepath)
        self._remove_file_symbols(abs_path)
        self._requirement_graph.remove_file(abs_path)
        self._requirement_graph.invalidate_file(abs_path)
        self._file_export_imports.pop(abs_path, None)
        self._cache.invalidate(abs_path)
        self._cache.invalidate_dependents(abs_path, self._include_graph)
        self._index_single_file(abs_path)
        self._wire_requirement_graph()
        self._compute_test_scopes()

    def _remove_file_symbols(self, filepath: str) -> None:
        """Rebuild the symbol table excluding all symbols from *filepath*."""
        old_symbols = list(self._symbol_table.all_symbols())
        self._symbol_table = SymbolTable()
        for sym in old_symbols:
            if sym.file_path != filepath:
                self._symbol_table.add_symbol(sym)

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

    def reindex(self) -> None:
        """Clear caches and fully re-index the workspace."""
        self._cache = FileCache()
        self.index_workspace()

    def detect_stale_files(self) -> List[str]:
        """Return indexed file paths that no longer exist on disk."""
        stale = []
        for filepath in self._symbol_table._by_file:
            if not os.path.exists(filepath):
                stale.append(filepath)
        return stale

    def get_stats(self) -> IndexerStats:
        """Return a snapshot of current indexer statistics."""
        all_symbols = len(self._symbol_table._all)
        file_count = len(self._symbol_table._by_file)

        # Count include edges from the _includes adjacency dict
        edge_count = sum(
            len(targets) for targets in self._include_graph._includes.values()
        )

        test_scope_count = len(
            getattr(self._requirement_graph, "_test_scopes", {})
        )

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
                reqs, writes = extract_requirements_full(
                    result.ast, filepath, source
                )
            else:
                reqs, writes = extract_requirements_light(source, filepath)
            self._requirement_graph.add_file_requirements(
                filepath, reqs, writes
            )
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
            self._file_export_imports[filepath] = info
        except Exception:
            logger.warning(
                "Export/import extraction failed for %s",
                filepath,
                exc_info=True,
            )

    def _wire_requirement_graph(self) -> None:
        """Wire state-variable READS edges and property DEPENDS_ON edges."""
        known_vars = self._requirement_graph.get_all_state_var_names()
        # Also gather variable names from the symbol table
        from lsprotocol.types import SymbolKind

        for sym in self._symbol_table.all_symbols():
            if sym.kind in (SymbolKind.Variable, SymbolKind.Function):
                known_vars.add(sym.name)
        self._requirement_graph.wire_state_var_edges(known_vars)
        self._requirement_graph.wire_dependency_edges()

    def _load_requirement_manifests(self) -> None:
        """Load RFC requirement manifests from the workspace and add to the graph."""
        from ivy_lsp.semantic.rfc_annotations import find_manifests, load_requirement_manifest

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
        for filepath, info in self._file_export_imports.items():
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
