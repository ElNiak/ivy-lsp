"""Test scope model for per-test requirement scoping.

Provides data structures for export/import tracking, test scope
computation, and scoped requirement queries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


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
