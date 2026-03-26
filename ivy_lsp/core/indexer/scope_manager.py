"""Scope management mixin for WorkspaceIndexer.

Extracts requirement graph wiring, test scope computation, incremental
re-indexing, and scope-aware symbol queries.  All methods operate on
``self`` which is a
:class:`~ivy_lsp.core.indexer.workspace_indexer.WorkspaceIndexer` instance at
runtime.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import TYPE_CHECKING, Any, List, Optional

from ivy_lsp.core.analysis.light_mode_extractor import (
    extract_exports_imports_light,
    extract_requirements_light,
)
from ivy_lsp.core.analysis.mirror import Mirror, MirrorId, MirrorRole
from ivy_lsp.core.analysis.requirement_extractor import (
    extract_exports_imports_full,
    extract_requirements_full,
)
from ivy_lsp.core.analysis.test_scope import TestScope, detect_test_role
from ivy_lsp.core.parsing.symbols import IvySymbol

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ScopeManagerMixin:
    """Requirement graph wiring, test scope computation, and incremental re-indexing."""

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

    @staticmethod
    def _compute_export_hash(info: Any) -> str:
        """Compute a deterministic hash of a file's export/import signature.

        The hash covers the sorted export and import action names.
        Returns a hex digest string.
        """
        exports = "\0".join(sorted(info.exports))
        imports = "\0".join(sorted(info.imports))
        payload = f"E:{exports}\nI:{imports}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reindex_file(self, filepath: str) -> None:
        """Re-index a single file after it has been modified on disk.

        Uses an export-hash cut-off: if the file's export/import
        signature hasn't changed since the last indexing, the expensive
        ``_wire_requirement_graph()`` and ``_compute_test_scopes()``
        calls are skipped.
        """
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

        # Cut-off: skip expensive re-wiring when exports haven't changed.
        old_hash = self._file_export_hashes.get(abs_path)
        with self._exports_lock:
            new_info = self._file_export_imports.get(abs_path)

        if new_info is not None:
            new_hash = self._compute_export_hash(new_info)
        else:
            # Extraction failed — be conservative and force re-wire.
            new_hash = None

        if new_hash is not None and old_hash == new_hash:
            # Exports unchanged — skip re-wiring.
            logger.debug("Export hash unchanged for %s; skipping re-wiring", abs_path)
            return

        # Update stored hash (or clear it on extraction failure).
        if new_hash is not None:
            self._file_export_hashes[abs_path] = new_hash
        else:
            self._file_export_hashes.pop(abs_path, None)

        self._wire_requirement_graph()
        self._compute_test_scopes(dirty_files={abs_path})

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
        self._compute_test_scopes(dirty_files=dirty)

    def _remove_file_symbols(self, filepath: str) -> None:
        """Remove all symbols from *filepath* from the symbol table."""
        with self._table_lock:
            self._symbol_table.remove_file(filepath)

    # ------------------------------------------------------------------
    # Scope-aware queries
    # ------------------------------------------------------------------

    def get_endpoint_mirrors_for_file(self, filepath: str) -> List[str]:
        """Return sorted endpoint mirror test files whose scope includes *filepath*.

        An endpoint mirror test is a test entry point (file with exports)
        whose transitive include closure contains *filepath*.
        """
        abs_path = os.path.abspath(filepath)
        return sorted(self._requirement_graph.get_tests_for_file(abs_path))

    def get_scope_files_for_file(self, filepath: str) -> set:
        """Return the union of all include closures that contain *filepath*.

        For shared modules, this returns the full set of files visible from
        any endpoint mirror test that includes this file.  For test entry
        points, returns the test's own include closure.
        """
        abs_path = os.path.abspath(filepath)
        mirrors = self._requirement_graph.get_tests_for_file(abs_path)
        if not mirrors:
            # Orphan file -- fall back to forward-only scope.
            result = {abs_path}
            result |= self._include_graph.get_transitive_includes(abs_path)
            return result

        scope_files: set = set()
        for test_file in mirrors:
            scope = self._requirement_graph.get_test_scope(test_file)
            if scope is not None:
                scope_files |= scope.include_closure
        return scope_files

    def get_symbols_in_scope(self, filepath: str) -> List[IvySymbol]:
        """Return symbols visible from *filepath*'s endpoint mirror scope.

        Uses the mirror-scope-aware algorithm:
        1. Find all endpoint mirror tests whose scope includes *filepath*.
        2. Union their include closures.
        3. Return symbols from all files in the union.

        Falls back to forward-only traversal for orphan files (not in any
        test scope).  Results are cached per filepath and invalidated when
        ``_compute_test_scopes`` runs.
        """
        abs_path = os.path.abspath(filepath)

        # Check cache first
        cached = self._mirror_scope_cache.get(abs_path)
        if cached is not None:
            return list(cached)

        scope_files = self.get_scope_files_for_file(abs_path)

        symbols: List[IvySymbol] = []
        for f in scope_files:
            symbols.extend(self._symbol_table.symbols_in_file(f))

        # Cache the result
        self._mirror_scope_cache[abs_path] = symbols
        return list(symbols)

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
            if sym.kind in (
                SymbolKind.Variable,
                SymbolKind.Function,
                SymbolKind.Method,
            ):
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
        from ivy_lsp.core.semantic.rfc_annotations import (
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

    def _compute_test_scopes(self, dirty_files: Optional[set] = None) -> None:
        """Build a TestScope for each file that has exports and register it.

        Args:
            dirty_files: If provided, only invalidate mirror-scope cache
                entries for files in the transitive scope of dirty files'
                test scopes. If None, clear the entire cache (full rebuild).
        """
        self._mirror_registry.clear()
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
            tester_role = scope.tester_role
            try:
                role = MirrorRole(tester_role)
            except ValueError:
                role = MirrorRole.UNKNOWN
            mirror = Mirror(
                id=MirrorId.from_test_file(filepath, protocol="unknown"),
                entry_file=filepath,
                include_closure=frozen_closure,
                exported_actions=frozenset(all_exports),
                imported_actions=frozenset(all_imports),
                role=role,
            )
            self._mirror_registry.register(mirror)

        # Selective or full cache invalidation
        if dirty_files is not None:
            # Use reverse index for O(|dirty|) instead of O(|all_scopes|)
            affected_tests: set = set()
            for df in dirty_files:
                affected_tests |= self._requirement_graph.get_tests_for_file(df)
            affected_files: set = set()
            for tf in affected_tests:
                scope = self._requirement_graph.get_test_scope(tf)
                if scope:
                    affected_files |= scope.include_closure
            for f in affected_files:
                self._mirror_scope_cache.pop(f, None)
        else:
            self._mirror_scope_cache.clear()

        # Build partitioned staging if there are basename collisions.
        if self._resolver.collision_map:
            test_closures = {
                scope.test_file: scope.include_closure
                for _, scope in self._requirement_graph.iter_test_scopes()
            }
            self._resolver.build_partitioned_staging(test_closures)
