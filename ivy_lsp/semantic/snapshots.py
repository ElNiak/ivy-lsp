"""Compiler snapshot dataclasses for Tier 3 analysis results.

These capture the state of the Ivy compiler after processing a module,
including sort information, symbol metadata, and module structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SortInfo:
    """Information about an Ivy sort (type)."""

    name: str
    arity: int = 0
    params: List[str] = field(default_factory=list)
    is_uninterpreted: bool = False


@dataclass(frozen=True)
class SymbolInfo:
    """Information about a symbol in the Ivy signature."""

    name: str
    sort: Optional[str] = None
    params: List[str] = field(default_factory=list)
    is_action: bool = False
    is_relation: bool = False


@dataclass(frozen=True)
class SignatureSnapshot:
    """Snapshot of the Ivy module signature after compilation."""

    sorts: Dict[str, SortInfo] = field(default_factory=dict)
    symbols: Dict[str, SymbolInfo] = field(default_factory=dict)
    actions: List[str] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModuleSnapshot:
    """Snapshot of a compiled Ivy module."""

    signature: Optional[SignatureSnapshot] = None
    axioms: List[str] = field(default_factory=list)
    conjectures: List[str] = field(default_factory=list)
    isolates: List[str] = field(default_factory=list)
    raw_module: Optional[Any] = None

    @classmethod
    def from_ir(cls, ir: Any) -> ModuleSnapshot:
        """Create a ModuleSnapshot from a CompiledModuleIR."""
        sig_sorts = {
            name: SortInfo(
                name=name,
                arity=s.arity,
                is_uninterpreted=s.is_uninterpreted,
            )
            for name, s in ir.sorts.items()
        }
        sig_symbols = {
            name: SymbolInfo(
                name=name,
                sort=s.sort_str,
                is_relation=s.is_relation,
            )
            for name, s in ir.symbols.items()
        }
        sig = SignatureSnapshot(
            sorts=sig_sorts,
            symbols=sig_symbols,
            actions=list(ir.actions.keys()),
            relations=[n for n, s in ir.symbols.items() if s.is_relation],
        )
        return cls(
            signature=sig,
            axioms=[lf.formula_str for lf in ir.labeled_axioms],
            conjectures=[lf.formula_str for lf in ir.labeled_conjectures],
            isolates=list(ir.isolates.keys()),
        )
