"""Test scope model for per-test requirement scoping.

Provides data structures for export/import tracking, test scope
computation, and scoped requirement queries.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Set, Tuple

from ivy_lsp.core.analysis.requirement_graph import RequirementGraph, RequirementNode


@dataclass
class ExportImportInfo:
    """Export/import declarations extracted from a single Ivy file."""

    file: str
    exports: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    export_lines: Dict[str, int] = field(default_factory=dict)
    import_lines: Dict[str, int] = field(default_factory=dict)

    @property
    def has_exports(self) -> bool:
        """Return True if this file declares any exports."""
        return len(self.exports) > 0

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "file": self.file,
            "exports": list(self.exports),
            "imports": list(self.imports),
            "export_lines": dict(self.export_lines),
            "import_lines": dict(self.import_lines),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExportImportInfo":
        """Deserialize from a plain dictionary."""
        return cls(
            file=d["file"],
            exports=d.get("exports", []),
            imports=d.get("imports", []),
            export_lines=d.get("export_lines", {}),
            import_lines=d.get("import_lines", {}),
        )


@dataclass(frozen=True)
class TestScope:
    """Scope of a single Ivy test file."""

    test_file: str
    include_closure: FrozenSet[str]
    exported_actions: FrozenSet[str]
    imported_actions: FrozenSet[str]
    tester_role: Literal["client", "server", "mim", "unknown"]

    def is_action_exported(self, action_name: str) -> bool:
        """Return True if the action is in this scope's exports."""
        return action_name in self.exported_actions

    def is_file_in_scope(self, filepath: str) -> bool:
        """Return True if the file is in this scope's include closure."""
        return filepath in self.include_closure

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "test": os.path.basename(self.test_file).replace(".ivy", ""),
            "entry_file": self.test_file,
            "role": self.tester_role,
            "transitive_includes": sorted(self.include_closure),
            "exported_actions": sorted(self.exported_actions),
            "imported_actions": sorted(self.imported_actions),
            "file_count": len(self.include_closure),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TestScope":
        """Deserialize from a plain dictionary."""
        return cls(
            test_file=d["entry_file"],
            include_closure=frozenset(d.get("transitive_includes", [])),
            exported_actions=frozenset(d.get("exported_actions", [])),
            imported_actions=frozenset(d.get("imported_actions", [])),
            tester_role=d.get("role", "unknown"),
        )


def detect_test_role(
    include_closure: FrozenSet[str],
) -> Literal["client", "server", "mim", "unknown"]:
    """Derive tester role from included behavior files.

    Uses Ivy role inversion: testing a server means tester is client.
    Only files with ``_behavior`` in the basename are role signals —
    entity files (``ivy_quic_mim.ivy``) and shim files are ignored.

    When multiple role signals exist, MIM takes precedence over
    client/server because real MIM tests legitimately include both
    MIM behavior and a client or server behavior file.
    """
    has_client = False
    has_server = False
    has_mim = False

    for f in include_closure:
        basename = os.path.basename(f).replace(".ivy", "")
        if "_behavior" not in basename:
            continue

        if "server_behavior" in basename:
            has_client = True  # role inversion
        elif "client_behavior" in basename:
            has_server = True  # role inversion
        elif "mim_behavior" in basename or "man_in_the_middle" in basename:
            has_mim = True

    if has_mim:
        return "mim"
    if has_client and not has_server:
        return "client"
    if has_server and not has_client:
        return "server"
    return "unknown"


class NctClassification(Enum):
    """NCT role classification for a requirement node."""

    ASSUMPTION = "ASSUMPTION"
    GUARANTEE = "GUARANTEE"
    TESTER_ONLY = "TESTER_ONLY"


class ActionClassification(Enum):
    """Direction classification for an action within a test scope."""

    GENERATED = "GENERATED"
    RECEIVED = "RECEIVED"
    INTERNAL = "INTERNAL"


def classify_requirement(req: RequirementNode) -> NctClassification:
    """Classify a requirement as assumption, guarantee, or tester-only.

    Priority:
    1. _generating in formula      -> TESTER_ONLY
    2. mixin_kind == "after"       -> GUARANTEE
    3. mixin_kind == "around"      -> GUARANTEE (conservative)
    4. kind in (ensure, assert)    -> GUARANTEE
    5. otherwise                   -> ASSUMPTION
    """
    if "_generating" in req.formula_text:
        return NctClassification.TESTER_ONLY
    if req.mixin_kind in ("after", "around"):
        return NctClassification.GUARANTEE
    if req.kind in ("ensure", "assert"):
        return NctClassification.GUARANTEE
    return NctClassification.ASSUMPTION


def classify_action_direction(
    action_name: str, scope: TestScope
) -> ActionClassification:
    """Classify an action as generated, received, or internal."""
    if action_name in scope.exported_actions:
        return ActionClassification.GENERATED
    if action_name in scope.imported_actions:
        return ActionClassification.RECEIVED
    return ActionClassification.INTERNAL


logger = logging.getLogger(__name__)


class ScopedRequirementModel(RequirementGraph):
    """RequirementGraph with per-test scoping layer.

    Inherits all RequirementGraph methods (unscoped) and adds
    scoped query methods that filter by test scope.
    """

    def __init__(self) -> None:
        """Initialize with empty test scopes and caches."""
        super().__init__()
        self._test_scopes: Dict[str, TestScope] = {}
        self._file_to_tests: Dict[str, Set[str]] = defaultdict(set)
        self._active_test: Optional[str] = None
        self._scope_cache: Dict[Tuple[str, bool], list] = {}
        self._compilation_results: Dict[str, Any] = {}

    # -- Public accessors for test scopes / compilation results ----------

    def has_test_scope(self, test_file: str) -> bool:
        """Check if a test scope exists for the given file."""
        return test_file in self._test_scopes

    def get_test_scope(self, test_file: str) -> Optional[TestScope]:
        """Get the test scope for a file, or None."""
        return self._test_scopes.get(test_file)

    def list_test_files(self) -> List[str]:
        """Return a list of all test file paths with scopes."""
        return list(self._test_scopes.keys())

    def iter_test_scopes(self):
        """Iterate over (test_file, scope) pairs sorted by filename."""
        return sorted(self._test_scopes.items())

    def get_compilation_result(self, test_file: str) -> Any:
        """Get the compilation result for a test file, or None."""
        return self._compilation_results.get(test_file)

    def set_compilation_result(self, test_file: str, result: Any) -> None:
        """Store a compilation result for a test file."""
        self._compilation_results[test_file] = result

    # -- Registration / mutation -------------------------------------------

    def register_test_scope(self, scope: TestScope) -> None:
        """Register a test scope and update file-to-test mappings."""
        with self._lock:
            self._test_scopes[scope.test_file] = scope
            for f in scope.include_closure:
                self._file_to_tests[f].add(scope.test_file)
            self._scope_cache.pop((scope.test_file, False), None)
            self._scope_cache.pop((scope.test_file, True), None)

    def remap_paths(self, base_dir: str) -> None:
        """Convert relative paths in _test_scopes to absolute.

        Follows the same rel→abs pattern used by T1 (symbols),
        T2 (includes), and T3 (exports) in _prepopulate_from_offline_index.
        """
        with self._lock:
            remapped: Dict[str, TestScope] = {}
            for path, scope in self._test_scopes.items():
                if not os.path.isabs(path):
                    abs_path = os.path.join(base_dir, path)
                    scope = TestScope(
                        test_file=abs_path,
                        include_closure=frozenset(
                            os.path.join(base_dir, f) if not os.path.isabs(f) else f
                            for f in scope.include_closure
                        ),
                        exported_actions=scope.exported_actions,
                        imported_actions=scope.imported_actions,
                        tester_role=scope.tester_role,
                    )
                    remapped[abs_path] = scope
                else:
                    remapped[path] = scope
            self._test_scopes = remapped
            # Rebuild file_to_tests from remapped scopes
            self._file_to_tests.clear()
            for test_file, scope in self._test_scopes.items():
                for f in scope.include_closure:
                    self._file_to_tests[f].add(test_file)
            self._scope_cache.clear()

    def set_active_test(self, test_file: Optional[str]) -> None:
        """Set the active test file for scoped queries."""
        if test_file is None or test_file in self._test_scopes:
            self._active_test = test_file

    def get_active_scope(self) -> Optional[TestScope]:
        """Return the currently active test scope, or None."""
        if self._active_test is None:
            return None
        return self._test_scopes.get(self._active_test)

    def get_tests_for_file(self, filepath: str) -> Set[str]:
        """Return test files whose scope includes the given file."""
        return set(self._file_to_tests.get(filepath, set()))

    def get_scoped_requirements(
        self,
        test_file: str,
        include_imported: bool = False,
    ) -> List[RequirementNode]:
        """Return requirements filtered to a test's scope."""
        cache_key = (test_file, include_imported)
        with self._lock:
            cached = self._scope_cache.get(cache_key)
            if cached is not None:
                return cached
            scope = self._test_scopes.get(test_file)
            if scope is None:
                return []
            if include_imported:
                result = [
                    r
                    for r in self.requirements.values()
                    if r.file in scope.include_closure
                ]
            else:
                result = [
                    r
                    for r in self.requirements.values()
                    if r.file in scope.include_closure
                    and r.monitor_action in scope.exported_actions
                ]
            self._scope_cache[cache_key] = result
            return result

    def get_scoped_counts(self, test_file: str, action_name: str) -> Dict[str, int]:
        """Return requirement kind counts for an action within a test scope."""
        scope = self._test_scopes.get(test_file)
        if scope is None or action_name not in scope.exported_actions:
            return {}
        counts: Dict[str, int] = defaultdict(int)
        for req in self.get_scoped_requirements(test_file):
            if req.monitor_action == action_name:
                counts[req.kind] += 1
        return dict(counts)

    def get_scoped_nct_counts(
        self, test_file: str, action_name: str
    ) -> List[Dict[str, Any]]:
        """Get requirement counts with NCT classification for a scoped action.

        Returns a list of dicts with keys: kind, count, nct_tag.
        Uses per-requirement NCT classification via :func:`classify_requirement`
        so that individual requirements within the same action can have
        different NCT tags (e.g. a ``require`` in a ``before`` mixin is
        ASSUMPTION while an ``ensure`` is GUARANTEE).

        Only returns entries for exported or imported actions.
        Internal actions return [].
        """
        scope = self._test_scopes.get(test_file)
        if scope is None:
            return []

        direction = classify_action_direction(action_name, scope)
        if direction == ActionClassification.INTERNAL:
            return []

        if direction == ActionClassification.RECEIVED:
            matching_requirements = [
                req
                for req in self.requirements.values()
                if (
                    req.file in scope.include_closure
                    and req.monitor_action == action_name
                )
            ]
        else:
            matching_requirements = [
                req
                for req in self.get_scoped_requirements(test_file)
                if req.monitor_action == action_name
            ]

        if not matching_requirements:
            return []

        counts_by_nct: Dict[Tuple[str, str], int] = defaultdict(int)
        for req in matching_requirements:
            nct = classify_requirement(req)
            counts_by_nct[(req.kind, nct.value)] += 1

        return [
            {"kind": kind, "count": count, "nct_tag": nct_tag}
            for (kind, nct_tag), count in sorted(counts_by_nct.items())
        ]

    def add_requirement(self, node: RequirementNode) -> None:
        """Add a requirement and invalidate affected scope caches."""
        super().add_requirement(node)
        self._invalidate_scope_cache_for_file(node.file)

    def add_file_requirements(
        self,
        filepath: str,
        reqs: List[RequirementNode],
        writes: Optional[List[Tuple[str, str, int]]] = None,
    ) -> None:
        """Bulk-add file requirements and invalidate scope caches."""
        super().add_file_requirements(filepath, reqs, writes)
        self._invalidate_scope_cache_for_file(filepath)

    def invalidate_file(self, filepath: str) -> None:
        """Invalidate scope caches affected by changes to a file."""
        self._invalidate_scope_cache_for_file(filepath)

    def _invalidate_scope_cache_for_file(self, filepath: str) -> None:
        # Note: callers (add_requirement, add_file_requirements) already hold
        # self._lock (RLock), so this re-entrant acquisition is safe.
        with self._lock:
            for test_file in self._file_to_tests.get(filepath, set()):
                self._scope_cache.pop((test_file, False), None)
                self._scope_cache.pop((test_file, True), None)
