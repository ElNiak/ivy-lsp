"""Workspace-wide Ivy file indexer and cross-file symbol lookup."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

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

    # ------------------------------------------------------------------
    # Full workspace indexing
    # ------------------------------------------------------------------

    def index_workspace(self) -> None:
        """Reset indices and parse every ``.ivy`` file in the workspace."""
        self._symbol_table = SymbolTable()
        self._include_graph = IncludeGraph()
        self._requirement_graph = ScopedRequirementModel()
        self._file_export_imports: Dict[str, ExportImportInfo] = {}

        files = self._resolver.find_all_ivy_files()
        for filepath in files:
            self._index_single_file(filepath)

        # Post-indexing: wire state var edges now that all vars are known
        self._wire_requirement_graph()
        self._load_requirement_manifests()
        self._wire_coverage_edges()

    # ------------------------------------------------------------------
    # Single-file indexing
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
        self._cache.invalidate(abs_path)
        self._cache.invalidate_dependents(abs_path, self._include_graph)
        self._index_single_file(abs_path)
        self._wire_requirement_graph()

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
