"""Intermediate Representation dataclasses for subprocess serialization.

These frozen dataclasses represent the wire format for serializing Ivy's
Module object across process boundaries. They use only Python primitives
(str, int, bool, list, dict, set, Optional, None) so they survive pickle
round-trips without requiring Z3 or Ivy imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass(frozen=True)
class SortIR:
    """IR representation of an Ivy sort (type).

    Captures sort metadata including whether it is enumerated
    (finite set of named constructors) or uninterpreted (abstract).
    """

    name: str
    arity: int = 0
    is_uninterpreted: bool = False
    is_enumerated: bool = False
    constructors: List[str] = field(default_factory=list)
    interpretation: Optional[str] = None


@dataclass(frozen=True)
class SymbolIR:
    """IR representation of an Ivy symbol (function/relation/destructor).

    Captures the symbol's signature including domain and range sorts,
    and classification flags.
    """

    name: str
    sort_str: str = ""
    domain_sorts: List[str] = field(default_factory=list)
    range_sort: str = ""
    is_destructor: bool = False
    is_constructor: bool = False
    is_relation: bool = False


@dataclass(frozen=True)
class ActionIR:
    """IR representation of an Ivy action.

    Captures formal parameters and returns as string representations,
    plus export/import status.
    """

    name: str
    formal_params: List[str] = field(default_factory=list)
    formal_returns: List[str] = field(default_factory=list)
    is_exported: bool = False
    is_imported: bool = False


@dataclass(frozen=True)
class MixinIR:
    """IR representation of an Ivy mixin (before/after/around advice).

    A mixin attaches a mixer action to run before, after, or around a
    mixee action.
    """

    mixer: str
    mixee: str
    kind: str  # "before" | "after" | "around"


@dataclass(frozen=True)
class IsolateIR:
    """IR representation of an Ivy isolate (verification unit).

    An isolate partitions the specification into verified components
    (whose properties are checked) and present components (assumed correct).
    """

    name: str
    verified_components: List[str] = field(default_factory=list)
    present_components: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LabeledFormulaIR:
    """IR representation of a labeled formula (invariant/axiom/conjecture).

    Captures the formula as a string along with metadata about its
    classification and source location.
    """

    label: str
    formula_str: str
    lineno: Optional[int] = None
    temporal: bool = False
    is_assumed: bool = False


@dataclass(frozen=True)
class RequirementIR:
    """IR representation of a requirement (pre/postcondition/assertion).

    Links a formula to a specific action, with classification of the
    requirement kind and how it is attached (via mixin or directly).
    """

    action_name: str
    kind: str  # "require" | "ensure" | "assume" | "assert"
    formula_str: str
    mixin_kind: str = "direct"  # "before" | "after" | "around" | "implement" | "direct"


@dataclass(frozen=True)
class CompiledModuleIR:
    """Top-level container holding the full IR of a compiled Ivy module.

    This is the wire format sent from the compilation subprocess back to
    the LSP process via ``multiprocessing.Pipe``.  Every field uses only
    Python primitives so the dataclass survives ``pickle`` round-trips
    without requiring Z3 or Ivy imports on the receiving side.
    """

    # --- collected sub-IRs ---
    sorts: Dict[str, SortIR] = field(default_factory=dict)
    symbols: Dict[str, SymbolIR] = field(default_factory=dict)
    actions: Dict[str, ActionIR] = field(default_factory=dict)
    public_actions: Set[str] = field(default_factory=set)
    mixins: Dict[str, List[MixinIR]] = field(default_factory=dict)
    isolates: Dict[str, IsolateIR] = field(default_factory=dict)

    # --- labeled formulas ---
    labeled_axioms: List[LabeledFormulaIR] = field(default_factory=list)
    labeled_properties: List[LabeledFormulaIR] = field(default_factory=list)
    labeled_conjectures: List[LabeledFormulaIR] = field(default_factory=list)
    definitions: List[LabeledFormulaIR] = field(default_factory=list)

    # --- requirements ---
    requirements: List[RequirementIR] = field(default_factory=list)

    # --- structural metadata ---
    hierarchy: Dict[str, Set[str]] = field(default_factory=dict)
    exports: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    delegates: List[str] = field(default_factory=list)
    mixord: List[str] = field(default_factory=list)
    sort_order: List[str] = field(default_factory=list)
    symbol_order: List[str] = field(default_factory=list)

    # --- compilation metadata ---
    errors: List[str] = field(default_factory=list)
    success: bool = False
    source_file: str = ""
    compile_duration: float = 0.0

    @staticmethod
    def empty(
        source_file: str,
        errors: Optional[List[str]] = None,
        duration: float = 0.0,
    ) -> "CompiledModuleIR":
        """Create a failed / empty IR with only metadata populated.

        Useful for returning a well-typed result when compilation fails
        or when a stub IR is needed for testing.
        """
        return CompiledModuleIR(
            source_file=source_file,
            errors=errors if errors is not None else [],
            compile_duration=duration,
            success=False,
        )
