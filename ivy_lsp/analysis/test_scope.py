"""Test scope model for per-test requirement scoping.

Provides data structures for export/import tracking, test scope
computation, and scoped requirement queries.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List


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
