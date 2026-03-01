"""Module IR extractor for subprocess-based compilation.

Runs INSIDE the forked subprocess after ``ivy_from_string()`` completes.
Walks the populated Module and Sig objects, converting every Z3-dependent
object to plain-Python IR dataclasses that survive ``pickle`` round-trips.

The top-level function :func:`extract_compiled_module_ir` **never raises**.
On any exception it returns a failed ``CompiledModuleIR`` via
:meth:`CompiledModuleIR.empty`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from ivy_lsp.compilation.ir import (
    ActionIR,
    CompiledModuleIR,
    IsolateIR,
    LabeledFormulaIR,
    MixinIR,
    RequirementIR,
    SortIR,
    SymbolIR,
)

logger = logging.getLogger(__name__)

# Action body type names that map to requirement kinds.
_REQUIREMENT_TYPES: Dict[str, str] = {
    "RequiresAction": "require",
    "EnsuresAction": "ensure",
    "AssumeAction": "assume",
    "AssertAction": "assert",
}

_requirement_types_verified = False


def verify_requirement_types() -> bool:
    """Verify that _REQUIREMENT_TYPES keys match actual Ivy AST classes.

    Called once at first extraction.  Returns True if all classes exist,
    False (with a warning) if any are missing.
    """
    global _requirement_types_verified
    if _requirement_types_verified:
        return True
    _requirement_types_verified = True
    try:
        import ivy.ivy_actions as ia

        missing = [
            name for name in _REQUIREMENT_TYPES
            if not hasattr(ia, name)
        ]
        if missing:
            logger.warning(
                "Ivy AST class mismatch -- missing: %s. "
                "Requirement extraction may be incomplete.",
                missing,
            )
            return False
        return True
    except ImportError:
        logger.warning(
            "Could not import ivy.ivy_actions -- "
            "requirement type verification skipped"
        )
        return False


def extract_compiled_module_ir(
    mod: Any,
    sig: Any,
    source_file: str,
    duration: float,
) -> CompiledModuleIR:
    """Extract a :class:`CompiledModuleIR` from a compiled Ivy module.

    This is the sole entry point.  It **never raises** -- all exceptions
    are caught and a failed IR is returned instead.

    Parameters
    ----------
    mod:
        The Ivy ``Module`` object (``ivy_module.module``), or ``None``.
    sig:
        The Ivy ``Sig`` object (``ivy_logic.sig``), or ``None``.
    source_file:
        Path to the ``.ivy`` source file that was compiled.
    duration:
        Wall-clock seconds that compilation took.

    Returns
    -------
    CompiledModuleIR
        Always returns a well-typed result.
    """
    try:
        verify_requirement_types()
        return _extract(mod, sig, source_file, duration)
    except Exception as exc:
        logger.warning(
            "Module IR extraction failed for %s: %s",
            source_file,
            exc,
            exc_info=True,
        )
        return CompiledModuleIR.empty(
            source_file=source_file,
            errors=[f"extraction failed: {exc}"],
            duration=duration,
        )


# ---------------------------------------------------------------------------
# Internal extraction pipeline
# ---------------------------------------------------------------------------


def _extract(
    mod: Any,
    sig: Any,
    source_file: str,
    duration: float,
) -> CompiledModuleIR:
    """Core extraction logic -- may raise."""
    if mod is None:
        return CompiledModuleIR.empty(
            source_file=source_file,
            errors=["module is None"],
            duration=duration,
        )

    # Use the sig from the module if the caller-provided sig is None.
    effective_sig = sig if sig is not None else getattr(mod, "sig", None)

    # --- sorts ---
    sorts = _extract_sorts(effective_sig)

    # --- symbols ---
    symbols = _extract_symbols(effective_sig)

    # --- actions ---
    actions_dict = getattr(mod, "actions", {}) or {}
    public_actions_raw = getattr(mod, "public_actions", set()) or set()
    exports_raw = getattr(mod, "exports", []) or []
    imports_raw = getattr(mod, "imports", []) or []

    export_names = _relname_set(exports_raw)
    import_names = _relname_set(imports_raw)

    actions = _extract_actions(actions_dict, public_actions_raw, export_names, import_names)
    public_actions = _safe_set_of_str(public_actions_raw)

    # --- mixins ---
    raw_mixins = getattr(mod, "mixins", {}) or {}
    mixins = _extract_mixins(raw_mixins)

    # --- build mixer->kind mapping for requirement extraction ---
    mixer_kind_map = _build_mixer_kind_map(raw_mixins)

    # --- isolates ---
    isolates = _extract_isolates(getattr(mod, "isolates", {}) or {})

    # --- labeled formulas ---
    labeled_axioms = _extract_labeled_formulas(
        getattr(mod, "labeled_axioms", []) or []
    )
    labeled_properties = _extract_labeled_formulas(
        getattr(mod, "labeled_props", []) or []
    )
    labeled_conjectures = _extract_labeled_formulas(
        getattr(mod, "labeled_conjs", []) or []
    )
    definitions = _extract_labeled_formulas(
        getattr(mod, "definitions", []) or []
    )

    # --- requirements (from action bodies, with mixin kind propagation) ---
    requirements = _extract_all_requirements(actions_dict, mixer_kind_map)

    # --- structural metadata ---
    hierarchy = _extract_hierarchy(getattr(mod, "hierarchy", {}) or {})
    exports = _relname_list(exports_raw)
    imports = _relname_list(imports_raw)
    aliases = _safe_dict_str_str(getattr(mod, "aliases", {}) or {})
    delegates = _safe_list_of_str(getattr(mod, "delegates", []) or [])
    mixord = _safe_list_of_str(getattr(mod, "mixord", []) or [])
    sort_order = _safe_list_of_str(getattr(mod, "sort_order", []) or [])
    symbol_order = _safe_list_of_str(getattr(mod, "symbol_order", []) or [])

    return CompiledModuleIR(
        sorts=sorts,
        symbols=symbols,
        actions=actions,
        public_actions=public_actions,
        mixins=mixins,
        isolates=isolates,
        labeled_axioms=labeled_axioms,
        labeled_properties=labeled_properties,
        labeled_conjectures=labeled_conjectures,
        definitions=definitions,
        requirements=requirements,
        hierarchy=hierarchy,
        exports=exports,
        imports=imports,
        aliases=aliases,
        delegates=delegates,
        mixord=mixord,
        sort_order=sort_order,
        symbol_order=symbol_order,
        errors=[],
        success=True,
        source_file=source_file,
        compile_duration=duration,
    )


# ---------------------------------------------------------------------------
# Sort extraction
# ---------------------------------------------------------------------------


def _extract_sorts(sig: Any) -> Dict[str, SortIR]:
    """Walk ``sig.sorts`` and produce ``SortIR`` instances."""
    if sig is None:
        return {}

    raw_sorts: Dict[str, Any] = getattr(sig, "sorts", {}) or {}
    result: Dict[str, SortIR] = {}

    for name, sort_obj in raw_sorts.items():
        try:
            result[name] = _sort_to_ir(name, sort_obj)
        except Exception:
            logger.debug("Failed to extract sort %s", name, exc_info=True)
            result[name] = SortIR(name=str(name))

    return result


def _sort_to_ir(name: str, sort_obj: Any) -> SortIR:
    """Convert a single Ivy sort object to ``SortIR``."""
    type_name = type(sort_obj).__name__
    arity = getattr(sort_obj, "arity", 0)

    is_enumerated = type_name == "EnumeratedSort"
    is_uninterpreted = type_name == "UninterpretedSort"

    constructors: List[str] = []
    if is_enumerated:
        try:
            defines = sort_obj.defines()
            constructors = [str(getattr(d, "name", d)) for d in defines]
        except Exception:
            logger.debug(
                "Failed to get constructors for sort %s", name, exc_info=True
            )

    interpretation: Optional[str] = getattr(sort_obj, "_interpretation", None)
    if interpretation is None:
        interpretation = getattr(sort_obj, "interpretation", None)
    if interpretation is not None:
        interpretation = str(interpretation)

    return SortIR(
        name=str(name),
        arity=int(arity) if arity is not None else 0,
        is_uninterpreted=is_uninterpreted,
        is_enumerated=is_enumerated,
        constructors=constructors,
        interpretation=interpretation,
    )


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


def _extract_symbols(sig: Any) -> Dict[str, SymbolIR]:
    """Walk ``sig.symbols`` and produce ``SymbolIR`` instances."""
    if sig is None:
        return {}

    raw_symbols: Dict[str, Any] = getattr(sig, "symbols", {}) or {}
    destructor_sorts: Dict[str, Any] = getattr(sig, "destructor_sorts", {}) or {}
    constructor_sorts: Dict[str, Any] = getattr(sig, "constructor_sorts", {}) or {}
    result: Dict[str, SymbolIR] = {}

    for name, sym_obj in raw_symbols.items():
        try:
            result[name] = _symbol_to_ir(
                name, sym_obj, destructor_sorts, constructor_sorts
            )
        except Exception:
            logger.debug("Failed to extract symbol %s", name, exc_info=True)
            result[name] = SymbolIR(name=str(name))

    return result


def _symbol_to_ir(
    name: str,
    sym_obj: Any,
    destructor_sorts: Dict[str, Any],
    constructor_sorts: Dict[str, Any],
) -> SymbolIR:
    """Convert a single Ivy symbol object to ``SymbolIR``."""
    sort_obj = getattr(sym_obj, "sort", None)
    sort_str = str(sort_obj) if sort_obj is not None else ""

    domain_sorts: List[str] = []
    range_sort = ""

    if sort_obj is not None:
        dom = getattr(sort_obj, "dom", []) or []
        domain_sorts = [str(d) for d in dom]
        rng = getattr(sort_obj, "rng", None)
        range_sort = str(rng) if rng is not None else ""

    is_destructor = str(name) in destructor_sorts
    is_constructor = str(name) in constructor_sorts
    is_relation = range_sort.lower() == "bool"
    if not is_relation and sort_obj is not None:
        # Fallback: check Ivy's own is_relation() if available.
        _is_rel = getattr(sort_obj, "is_relation", None)
        if callable(_is_rel):
            try:
                is_relation = bool(_is_rel())
            except Exception:
                pass

    return SymbolIR(
        name=str(name),
        sort_str=sort_str,
        domain_sorts=domain_sorts,
        range_sort=range_sort,
        is_destructor=is_destructor,
        is_constructor=is_constructor,
        is_relation=is_relation,
    )


# ---------------------------------------------------------------------------
# Action extraction
# ---------------------------------------------------------------------------


def _extract_actions(
    actions_dict: Dict[str, Any],
    public_actions: set,
    export_names: Set[str],
    import_names: Set[str],
) -> Dict[str, ActionIR]:
    """Walk ``mod.actions`` and produce ``ActionIR`` instances."""
    result: Dict[str, ActionIR] = {}

    for name, action_obj in actions_dict.items():
        try:
            result[name] = _action_to_ir(
                name, action_obj, public_actions, export_names, import_names
            )
        except Exception:
            logger.debug("Failed to extract action %s", name, exc_info=True)
            result[name] = ActionIR(name=str(name))

    return result


def _action_to_ir(
    name: str,
    action_obj: Any,
    public_actions: set,
    export_names: Set[str],
    import_names: Set[str],
) -> ActionIR:
    """Convert a single Ivy action object to ``ActionIR``."""
    formal_params = _stringify_params(
        getattr(action_obj, "formal_params", []) or []
    )
    formal_returns = _stringify_params(
        getattr(action_obj, "formal_returns", []) or []
    )

    is_exported = str(name) in export_names
    is_imported = str(name) in import_names

    return ActionIR(
        name=str(name),
        formal_params=formal_params,
        formal_returns=formal_returns,
        is_exported=is_exported,
        is_imported=is_imported,
    )


def _stringify_params(params: list) -> List[str]:
    """Convert a list of parameter objects to string representations."""
    result: List[str] = []
    for p in params:
        try:
            p_name = getattr(p, "name", None)
            p_sort = getattr(p, "sort", None)
            if p_name is not None and p_sort is not None:
                sort_name = getattr(p_sort, "name", str(p_sort))
                result.append(f"{p_name}:{sort_name}")
            else:
                result.append(str(p))
        except Exception:
            result.append(str(p))
    return result


# ---------------------------------------------------------------------------
# Mixin extraction
# ---------------------------------------------------------------------------


def _extract_mixins(
    mixins_dict: Dict[str, list],
) -> Dict[str, List[MixinIR]]:
    """Walk ``mod.mixins`` and produce ``MixinIR`` instances."""
    result: Dict[str, List[MixinIR]] = {}

    for mixee_name, mixin_list in mixins_dict.items():
        ir_list: List[MixinIR] = []
        for mixin_obj in (mixin_list or []):
            try:
                ir_list.append(_mixin_to_ir(str(mixee_name), mixin_obj))
            except Exception:
                logger.debug(
                    "Failed to extract mixin for %s", mixee_name, exc_info=True
                )
        if ir_list:
            result[str(mixee_name)] = ir_list

    return result


def _mixin_to_ir(mixee_name: str, mixin_obj: Any) -> MixinIR:
    """Convert a single Ivy mixin object to ``MixinIR``."""
    # mixer name: args[0].relname
    args = getattr(mixin_obj, "args", []) or []
    if args:
        mixer = str(getattr(args[0], "relname", args[0]))
    else:
        mixer = ""

    # mixee name: from the mixin_obj.mixee.relname or use the dict key
    mixee_obj = getattr(mixin_obj, "mixee", None)
    if mixee_obj is not None:
        mixee = str(getattr(mixee_obj, "relname", mixee_obj))
    else:
        mixee = mixee_name

    # Detect kind from the Ivy AST class type (MixinAfterDef, MixinBeforeDef).
    type_name = type(mixin_obj).__name__
    if "After" in type_name:
        kind = "after"
    elif "Before" in type_name:
        kind = "before"
    else:
        kind = "around"

    return MixinIR(mixer=mixer, mixee=mixee, kind=kind)


# ---------------------------------------------------------------------------
# Isolate extraction
# ---------------------------------------------------------------------------


def _extract_isolates(
    isolates_dict: Dict[str, Any],
) -> Dict[str, IsolateIR]:
    """Walk ``mod.isolates`` and produce ``IsolateIR`` instances."""
    result: Dict[str, IsolateIR] = {}

    for name, iso_obj in isolates_dict.items():
        try:
            result[name] = _isolate_to_ir(str(name), iso_obj)
        except Exception:
            logger.debug("Failed to extract isolate %s", name, exc_info=True)
            result[name] = IsolateIR(name=str(name))

    return result


def _isolate_to_ir(name: str, iso_obj: Any) -> IsolateIR:
    """Convert a single Ivy isolate object to ``IsolateIR``."""
    verified: List[str] = []
    present: List[str] = []

    if hasattr(iso_obj, "verified") and callable(iso_obj.verified):
        try:
            verified = [
                str(getattr(v, "relname", v)) for v in iso_obj.verified()
            ]
        except Exception:
            logger.debug("Failed to get verified for %s", name, exc_info=True)

    if hasattr(iso_obj, "present") and callable(iso_obj.present):
        try:
            present = [
                str(getattr(p, "relname", p)) for p in iso_obj.present()
            ]
        except Exception:
            logger.debug("Failed to get present for %s", name, exc_info=True)

    return IsolateIR(
        name=name,
        verified_components=verified,
        present_components=present,
    )


# ---------------------------------------------------------------------------
# Labeled formula extraction
# ---------------------------------------------------------------------------


def _extract_labeled_formulas(
    formulas: list,
) -> List[LabeledFormulaIR]:
    """Convert a list of labeled formulas to ``LabeledFormulaIR``."""
    result: List[LabeledFormulaIR] = []

    for f in formulas:
        try:
            result.append(_labeled_formula_to_ir(f))
        except Exception:
            logger.debug("Failed to extract labeled formula", exc_info=True)

    return result


def _labeled_formula_to_ir(f: Any) -> LabeledFormulaIR:
    """Convert a single labeled formula to ``LabeledFormulaIR``."""
    # Label may be an object with .relname or a plain string.
    label_obj = getattr(f, "label", None)
    if label_obj is not None:
        label = str(getattr(label_obj, "relname", label_obj))
    else:
        label = ""

    formula_str = str(getattr(f, "formula", ""))

    # Line number: prefer label.lineno, then f.lineno.
    lineno: Optional[int] = None
    if label_obj is not None:
        lineno = getattr(label_obj, "lineno", None)
    if lineno is None:
        lineno = getattr(f, "lineno", None)

    temporal = bool(getattr(f, "temporal", False))
    is_assumed = bool(getattr(f, "assumed", False))

    return LabeledFormulaIR(
        label=label,
        formula_str=formula_str,
        lineno=lineno,
        temporal=temporal,
        is_assumed=is_assumed,
    )


# ---------------------------------------------------------------------------
# Mixer-to-kind mapping (for mixin_kind propagation to requirements)
# ---------------------------------------------------------------------------


def _build_mixer_kind_map(
    raw_mixins: Dict[str, list],
) -> Dict[str, str]:
    """Build a mapping from mixer action names to their mixin kind.

    Walks ``mod.mixins`` and records each mixer's kind (before/after/around)
    so that requirements extracted from mixer action bodies can be tagged
    with the correct ``mixin_kind`` instead of ``"direct"``.
    """
    result: Dict[str, str] = {}
    for _mixee_name, mixin_list in raw_mixins.items():
        for mixin_obj in (mixin_list or []):
            try:
                args = getattr(mixin_obj, "args", []) or []
                if args:
                    mixer = str(getattr(args[0], "relname", args[0]))
                else:
                    continue

                type_name = type(mixin_obj).__name__
                if "After" in type_name:
                    kind = "after"
                elif "Before" in type_name:
                    kind = "before"
                else:
                    kind = "around"

                result[mixer] = kind
            except Exception:
                logger.debug(
                    "Failed to map mixer kind for %s",
                    _mixee_name,
                    exc_info=True,
                )
    return result


# ---------------------------------------------------------------------------
# Requirement extraction (walking action bodies)
# ---------------------------------------------------------------------------


def _extract_all_requirements(
    actions_dict: Dict[str, Any],
    mixer_kind_map: Optional[Dict[str, str]] = None,
) -> List[RequirementIR]:
    """Walk all action bodies and extract requirement nodes.

    Uses *mixer_kind_map* to set the correct ``mixin_kind`` for
    requirements found inside mixer action bodies.
    """
    if mixer_kind_map is None:
        mixer_kind_map = {}
    result: List[RequirementIR] = []

    for action_name, action_obj in actions_dict.items():
        try:
            mixin_kind = mixer_kind_map.get(str(action_name), "direct")
            _walk_action_body(str(action_name), action_obj, result, mixin_kind)
        except Exception:
            logger.debug(
                "Failed to walk action body for %s",
                action_name,
                exc_info=True,
            )

    return result


def _walk_action_body(
    action_name: str,
    node: Any,
    result: List[RequirementIR],
    mixin_kind: str = "direct",
) -> None:
    """Recursively walk an action's AST body to find requirement nodes."""
    if node is None:
        return

    type_name = type(node).__name__
    kind = _REQUIREMENT_TYPES.get(type_name)

    if kind is not None:
        # Extract the formula from the requirement.
        formula_str = _safe_str(node)
        result.append(
            RequirementIR(
                action_name=action_name,
                kind=kind,
                formula_str=formula_str,
                mixin_kind=mixin_kind,
            )
        )

    # Recurse into children via .args.
    args = getattr(node, "args", []) or []
    for child in args:
        _walk_action_body(action_name, child, result, mixin_kind)


# ---------------------------------------------------------------------------
# Structural metadata helpers
# ---------------------------------------------------------------------------


def _extract_hierarchy(
    raw_hierarchy: Dict[str, Any],
) -> Dict[str, Set[str]]:
    """Convert module hierarchy to plain dict of sets."""
    result: Dict[str, Set[str]] = {}
    for key, value in raw_hierarchy.items():
        try:
            result[str(key)] = {str(v) for v in value}
        except Exception:
            result[str(key)] = set()
    return result


def _relname_set(items: list) -> Set[str]:
    """Extract a set of .relname strings from a list of objects."""
    result: Set[str] = set()
    for i, item in enumerate(items):
        try:
            name = getattr(item, "relname", None)
            if name is not None:
                result.add(str(name))
            else:
                result.add(str(item))
        except Exception:
            logger.warning(
                "Failed to extract relname from item %d (type=%s)",
                i, type(item).__name__, exc_info=True,
            )
    return result


def _relname_list(items: list) -> List[str]:
    """Extract a list of .relname strings from a list of objects."""
    result: List[str] = []
    for i, item in enumerate(items):
        try:
            name = getattr(item, "relname", None)
            if name is not None:
                result.append(str(name))
            else:
                result.append(str(item))
        except Exception:
            logger.warning(
                "Failed to extract relname from item %d (type=%s)",
                i, type(item).__name__, exc_info=True,
            )
    return result


def _safe_set_of_str(raw: Any) -> Set[str]:
    """Convert any iterable to a set of strings."""
    try:
        return {str(item) for item in raw}
    except Exception:
        logger.warning(
            "Failed to convert iterable to set of strings (type=%s)",
            type(raw).__name__, exc_info=True,
        )
        return set()


def _safe_list_of_str(raw: Any) -> List[str]:
    """Convert any iterable to a list of strings."""
    try:
        return [str(item) for item in raw]
    except Exception:
        logger.warning(
            "Failed to convert iterable to list of strings (type=%s)",
            type(raw).__name__, exc_info=True,
        )
        return []


def _safe_dict_str_str(raw: Any) -> Dict[str, str]:
    """Convert a mapping to Dict[str, str]."""
    try:
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        logger.warning(
            "Failed to convert mapping to dict (type=%s)",
            type(raw).__name__, exc_info=True,
        )
        return {}


def _safe_str(obj: Any) -> str:
    """Safely stringify any object."""
    try:
        return str(obj)
    except Exception:
        return "<unprintable>"
