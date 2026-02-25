"""Compiler snapshot dataclasses for Tier 3 analysis results.

These capture the state of the Ivy compiler after processing a module,
including sort information, symbol metadata, and module structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SortInfo:
    """Information about an Ivy sort (type)."""

    name: str
    arity: int = 0
    params: List[str] = field(default_factory=list)
    is_uninterpreted: bool = False


@dataclass
class SymbolInfo:
    """Information about a symbol in the Ivy signature."""

    name: str
    sort: Optional[str] = None
    params: List[str] = field(default_factory=list)
    is_action: bool = False
    is_relation: bool = False


@dataclass
class SignatureSnapshot:
    """Snapshot of the Ivy module signature after compilation."""

    sorts: Dict[str, SortInfo] = field(default_factory=dict)
    symbols: Dict[str, SymbolInfo] = field(default_factory=dict)
    actions: List[str] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)


@dataclass
class ModuleSnapshot:
    """Snapshot of a compiled Ivy module."""

    signature: Optional[SignatureSnapshot] = None
    axioms: List[str] = field(default_factory=list)
    conjectures: List[str] = field(default_factory=list)
    isolates: List[str] = field(default_factory=list)
    raw_module: Optional[Any] = None
