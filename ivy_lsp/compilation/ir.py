"""Intermediate Representation dataclasses for subprocess serialization.

Leaf types (SortIR, SymbolIR, etc.) are frozen with immutable collections
(tuple, frozenset).  The top-level CompiledModuleIR is frozen at the
attribute level but contains Dict fields that are shallowly mutable.
All values survive pickle round-trips without requiring Z3 or Ivy imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Literal, Optional, Sequence, Tuple


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
    constructors: Tuple[str, ...] = field(default_factory=tuple)
    interpretation: Optional[str] = None


@dataclass(frozen=True)
class SymbolIR:
    """IR representation of an Ivy symbol (function/relation/destructor).

    Captures the symbol's signature including domain and range sorts,
    and classification flags.
    """

    name: str
    sort_str: str = ""
    domain_sorts: Tuple[str, ...] = field(default_factory=tuple)
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
    formal_params: Tuple[str, ...] = field(default_factory=tuple)
    formal_returns: Tuple[str, ...] = field(default_factory=tuple)
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
    kind: Literal["before", "after", "around"] = "before"


@dataclass(frozen=True)
class IsolateIR:
    """IR representation of an Ivy isolate (verification unit).

    An isolate partitions the specification into verified components
    (whose properties are checked) and present components (assumed correct).
    """

    name: str
    verified_components: Tuple[str, ...] = field(default_factory=tuple)
    present_components: Tuple[str, ...] = field(default_factory=tuple)


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
    kind: Literal["require", "ensure", "assume", "assert"] = "require"
    formula_str: str = ""
    mixin_kind: Literal["before", "after", "around", "implement", "direct"] = "direct"


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
    public_actions: FrozenSet[str] = field(default_factory=frozenset)
    mixins: Dict[str, Tuple[MixinIR, ...]] = field(default_factory=dict)
    isolates: Dict[str, IsolateIR] = field(default_factory=dict)

    # --- labeled formulas ---
    labeled_axioms: Tuple[LabeledFormulaIR, ...] = field(default_factory=tuple)
    labeled_properties: Tuple[LabeledFormulaIR, ...] = field(default_factory=tuple)
    labeled_conjectures: Tuple[LabeledFormulaIR, ...] = field(default_factory=tuple)
    definitions: Tuple[LabeledFormulaIR, ...] = field(default_factory=tuple)

    # --- requirements ---
    requirements: Tuple[RequirementIR, ...] = field(default_factory=tuple)

    # --- structural metadata ---
    hierarchy: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    exports: Tuple[str, ...] = field(default_factory=tuple)
    imports: Tuple[str, ...] = field(default_factory=tuple)
    aliases: Dict[str, str] = field(default_factory=dict)
    delegates: Tuple[str, ...] = field(default_factory=tuple)
    mixord: Tuple[str, ...] = field(default_factory=tuple)
    sort_order: Tuple[str, ...] = field(default_factory=tuple)
    symbol_order: Tuple[str, ...] = field(default_factory=tuple)

    # --- compilation metadata ---
    errors: Tuple[str, ...] = field(default_factory=tuple)
    success: bool = False
    source_file: str = ""
    compile_duration: float = 0.0

    def __post_init__(self) -> None:
        if self.success and self.errors:
            raise ValueError(
                "CompiledModuleIR: success=True is incompatible with non-empty errors"
            )

    @staticmethod
    def empty(
        source_file: str,
        errors: Optional[Sequence[str]] = None,
        duration: float = 0.0,
    ) -> "CompiledModuleIR":
        """Create a failed / empty IR with only metadata populated.

        Useful for returning a well-typed result when compilation fails
        or when a stub IR is needed for testing.
        """
        return CompiledModuleIR(
            source_file=source_file,
            errors=tuple(errors) if errors is not None else (),
            compile_duration=duration,
            success=False,
        )
