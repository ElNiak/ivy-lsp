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
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from ivy_lsp.analysis.requirement_graph import RequirementGraph, RequirementNode


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
        return len(self.exports) > 0

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "exports": list(self.exports),
            "imports": list(self.imports),
            "export_lines": dict(self.export_lines),
            "import_lines": dict(self.import_lines),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExportImportInfo":
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
    tester_role: str  # "client" | "server" | "mim" | "unknown"

    def is_action_exported(self, action_name: str) -> bool:
        return action_name in self.exported_actions

    def is_file_in_scope(self, filepath: str) -> bool:
        return filepath in self.include_closure


def detect_test_role(include_closure: FrozenSet[str]) -> str:
    """Derive tester role from included behavior files.

    Uses Ivy role inversion: testing a server means tester is client.
    """
    for f in include_closure:
        basename = os.path.basename(f).replace(".ivy", "")
        if "server_behavior" in basename:
            return "client"
        if "client_behavior" in basename:
            return "server"
        if "mim" in basename:
            return "mim"
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
        super().__init__()
        self._test_scopes: Dict[str, TestScope] = {}
        self._file_to_tests: Dict[str, Set[str]] = defaultdict(set)
        self._active_test: Optional[str] = None
        self._scope_cache: Dict[Tuple[str, bool], list] = {}
        self._compilation_results: Dict[str, Any] = {}

    def register_test_scope(self, scope: TestScope) -> None:
        self._test_scopes[scope.test_file] = scope
        for f in scope.include_closure:
            self._file_to_tests[f].add(scope.test_file)
        self._scope_cache.pop((scope.test_file, False), None)
        self._scope_cache.pop((scope.test_file, True), None)

    def set_active_test(self, test_file: Optional[str]) -> None:
        if test_file is None or test_file in self._test_scopes:
            self._active_test = test_file

    def get_active_scope(self) -> Optional[TestScope]:
        if self._active_test is None:
            return None
        return self._test_scopes.get(self._active_test)

    def get_tests_for_file(self, filepath: str) -> Set[str]:
        return set(self._file_to_tests.get(filepath, set()))

    def get_scoped_requirements(
        self, test_file: str, include_imported: bool = False,
    ) -> List[RequirementNode]:
        cache_key = (test_file, include_imported)
        if cache_key in self._scope_cache:
            return self._scope_cache[cache_key]
        scope = self._test_scopes.get(test_file)
        if scope is None:
            return []
        if include_imported:
            result = [
                r for r in self.requirements.values()
                if r.file in scope.include_closure
            ]
        else:
            result = [
                r for r in self.requirements.values()
                if r.file in scope.include_closure
                and r.monitor_action in scope.exported_actions
            ]
        self._scope_cache[cache_key] = result
        return result

    def get_scoped_counts(self, test_file: str, action_name: str) -> Dict[str, int]:
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
                req for req in self.requirements.values()
                if (
                    req.file in scope.include_closure
                    and req.monitor_action == action_name
                )
            ]
        else:
            matching_requirements = [
                req for req in self.get_scoped_requirements(test_file)
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
        super().add_requirement(node)
        self._invalidate_scope_cache_for_file(node.file)

    def add_file_requirements(
        self,
        filepath: str,
        reqs: List[RequirementNode],
        writes: Optional[List[Tuple[str, str, int]]] = None,
    ) -> None:
        super().add_file_requirements(filepath, reqs, writes)
        self._invalidate_scope_cache_for_file(filepath)

    def invalidate_file(self, filepath: str) -> None:
        self._invalidate_scope_cache_for_file(filepath)

    def _invalidate_scope_cache_for_file(self, filepath: str) -> None:
        for test_file in self._file_to_tests.get(filepath, set()):
            self._scope_cache.pop((test_file, False), None)
            self._scope_cache.pop((test_file, True), None)
