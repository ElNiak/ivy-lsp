"""Convert Ivy AST declarations to IvySymbol trees.

Walks the flat declaration list produced by ``ivy_parser.parse()`` and
emits a list of :class:`IvySymbol` instances.  Dot-prefixed names
(produced by the parser for nested ``object`` bodies) are reassembled
into a parent/child hierarchy via :func:`_reconstruct_hierarchy`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol, SymbolReference
from ivy_lsp.utils.position_utils import ivy_location_to_range

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ast_to_symbols(ivy_obj: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a parsed Ivy AST into a hierarchical list of symbols.

    Args:
        ivy_obj: The result of ``ivy_parser.parse()`` (has ``.decls``).
        filename: The originating file path.
        source: The full source text (needed for line-length lookups).

    Returns:
        A list of top-level ``IvySymbol`` instances with nested children.
    """
    if ivy_obj is None or not hasattr(ivy_obj, "decls"):
        return []

    flat_symbols: List[IvySymbol] = []
    abs_filename = os.path.abspath(filename) if filename else filename
    for decl in ivy_obj.decls:
        try:
            if is_from_included_file(decl, abs_filename):
                continue
            syms = _convert_decl(decl, filename, source)
            flat_symbols.extend(syms)
        except Exception:
            logger.warning(
                "Failed to convert declaration %s in %s; symbol will be missing from outline",
                type(decl).__name__,
                filename,
                exc_info=True,
            )

    return _reconstruct_hierarchy(flat_symbols)


# ---------------------------------------------------------------------------
# Hierarchy reconstruction
# ---------------------------------------------------------------------------


def _reconstruct_hierarchy(flat_symbols: List[IvySymbol]) -> List[IvySymbol]:
    """Nest dot-prefixed symbols under their parent.

    Given flat symbols ``["frame", "frame.ack", "frame.ack.largest_acked"]``,
    produces ``"frame"`` with child ``"ack"`` with child ``"largest_acked"``.

    When multiple symbols share the same name (e.g. an ObjectDecl and a
    TypeDecl both named ``"bit"``), the Module/Namespace symbol is
    preferred as the parent target.  The TypeDecl for inner ``type this``
    is itself nested as a child.
    """
    root_symbols: List[IvySymbol] = []
    # Maps full dotted name to the *preferred parent* symbol.
    # Module/Namespace kinds win over others because children should
    # nest under the object/module, not the inner ``type this``.
    by_name: Dict[str, IvySymbol] = {}

    _CONTAINER_KINDS = {SymbolKind.Module, SymbolKind.Namespace}

    # First pass: index symbols, preferring container kinds.
    for sym in flat_symbols:
        existing = by_name.get(sym.name)
        if existing is None:
            by_name[sym.name] = sym
        elif sym.kind in _CONTAINER_KINDS and existing.kind not in _CONTAINER_KINDS:
            # New sym is a container, existing is not — replace.
            by_name[sym.name] = sym
        # Otherwise keep the existing entry (first container wins, or
        # first non-container if no container is seen).

    # Second pass: nest children and collect roots.
    for sym in flat_symbols:
        if "." in sym.name:
            # Check whether a container symbol with the *same* full
            # dotted name already exists.  If so, this is the inner
            # ``type this`` that should be nested *inside* the existing
            # container child, not added as a sibling under the parent.
            existing_child = by_name.get(sym.name)
            if (
                existing_child is not None
                and existing_child is not sym
                and existing_child.kind in _CONTAINER_KINDS
            ):
                child_name = sym.name.rsplit(".", 1)[1]
                child = IvySymbol(
                    name=child_name,
                    kind=sym.kind,
                    range=sym.range,
                    children=sym.children,
                    detail=sym.detail,
                    file_path=sym.file_path,
                )
                existing_child.children.append(child)
                continue

            parent_name = sym.name.rsplit(".", 1)[0]
            if parent_name in by_name:
                child_name = sym.name.rsplit(".", 1)[1]
                child = IvySymbol(
                    name=child_name,
                    kind=sym.kind,
                    range=sym.range,
                    children=sym.children,
                    detail=sym.detail,
                    file_path=sym.file_path,
                )
                by_name[parent_name].children.append(child)
                # Register the child under its full name so deeper
                # nesting can find it (e.g. "frame.ack" is parent of
                # "frame.ack.largest_acked").  Only register if no
                # container already owns this name slot.
                if by_name.get(sym.name) is sym:
                    by_name[sym.name] = child
                continue

        # Non-dotted name: check if this is the canonical entry or a
        # duplicate that should be nested as a child of the container.
        if sym.name in by_name and by_name[sym.name] is not sym:
            # This is the *non-preferred* duplicate (e.g., TypeDecl
            # ``"bit"`` when an ObjectDecl ``"bit"`` is the container).
            parent = by_name[sym.name]
            if parent.kind in _CONTAINER_KINDS:
                child = IvySymbol(
                    name=sym.name,
                    kind=sym.kind,
                    range=sym.range,
                    children=sym.children,
                    detail=sym.detail,
                    file_path=sym.file_path,
                )
                parent.children.append(child)
                continue

        root_symbols.append(sym)

    return root_symbols


# ---------------------------------------------------------------------------
# Per-declaration converters
# ---------------------------------------------------------------------------


def _convert_decl(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Dispatch a single declaration to the appropriate handler.

    Returns a (possibly empty) list of flat ``IvySymbol`` instances.
    """
    import ivy.ivy_ast as ia

    if isinstance(decl, ia.ObjectDecl):
        return _convert_object(decl, filename, source)
    if isinstance(decl, ia.ActionDecl):
        return _convert_action(decl, filename, source)
    if isinstance(decl, ia.TypeDecl):
        return _convert_type(decl, filename, source)
    if isinstance(decl, ia.DefinitionDecl):
        return _convert_definition(decl, filename, source)
    if isinstance(decl, ia.PropertyDecl):
        return _convert_labeled(
            decl, filename, source, SymbolKind.Property, keyword="property"
        )
    if isinstance(decl, ia.AxiomDecl):
        return _convert_labeled(
            decl, filename, source, SymbolKind.Property, keyword="axiom"
        )
    if isinstance(decl, ia.ConjectureDecl):
        return _convert_labeled(
            decl, filename, source, SymbolKind.Property, keyword="conjecture"
        )
    if isinstance(decl, ia.IsolateDecl):
        return _convert_isolate(decl, filename, source)
    if isinstance(decl, ia.ModuleDecl):
        return _convert_module(decl, filename, source)
    if isinstance(decl, ia.AliasDecl):
        return _convert_alias(decl, filename, source)
    if isinstance(decl, ia.DerivedDecl):
        return _convert_derived(decl, filename, source)
    if isinstance(decl, ia.InterpretDecl):
        return _convert_interpret(decl, filename, source)
    if isinstance(decl, ia.SchemaDecl):
        return _convert_schema(decl, filename, source)
    if isinstance(decl, ia.TheoremDecl):
        return _convert_theorem(decl, filename, source)
    # ConstantDecl check must come after subclass checks (DestructorDecl,
    # ConstructorDecl inherit from ConstantDecl).
    if isinstance(decl, ia.DestructorDecl):
        return _convert_constant(decl, filename, source, SymbolKind.Field)
    if isinstance(decl, ia.ConstructorDecl):
        return _convert_constant(decl, filename, source, SymbolKind.EnumMember)
    if isinstance(decl, ia.ConstantDecl):
        return _convert_constant_or_relation(decl, filename, source)
    if isinstance(decl, ia.InstantiateDecl):
        return _convert_instantiate(decl, filename, source)
    if isinstance(decl, ia.VariantDecl):
        return []
    if isinstance(decl, ia.MixinDecl):
        return _convert_mixin(decl, filename, source)
    if isinstance(decl, ia.ExportDecl):
        return _convert_export(decl, filename, source)
    if isinstance(decl, ia.ImportDecl):
        return _convert_import(decl, filename, source)
    if isinstance(decl, ia.NativeDecl):
        return _convert_native(decl, filename, source)
    if isinstance(decl, ia.AttributeDecl):
        return _convert_attribute(decl, filename, source)

    logger.debug("No converter for %s", type(decl).__name__)
    return []


# ---------------------------------------------------------------------------
# Type declarations
# ---------------------------------------------------------------------------


def _convert_type(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a TypeDecl to one IvySymbol (SymbolKind.Class).

    For enum types (``type sk = {a, b}``), includes a detail string
    listing the variant names.
    """
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="type")
    detail = _extract_enum_detail(decl)

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Class,
            range=rng,
            detail=detail,
            file_path=filename,
        )
    ]


def _extract_enum_detail(decl: Any) -> Optional[str]:
    """If the TypeDecl defines an enumerated sort, return 'enum: a, b, ...'."""
    import ivy.ivy_ast as ia

    try:
        type_def = decl.args[0]  # TypeDef
        if len(type_def.args) >= 2:
            sort_part = type_def.args[1]
            if isinstance(sort_part, ia.EnumeratedSort):
                variant_names = [getattr(a, "relname", str(a)) for a in sort_part.args]
                return "enum: " + ", ".join(variant_names)
    except (IndexError, AttributeError):
        logger.debug("Could not extract enum detail from %s", type(decl).__name__)
    return None


# ---------------------------------------------------------------------------
# Object declarations
# ---------------------------------------------------------------------------


def _convert_object(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an ObjectDecl to SymbolKind.Module."""
    defs = decl.defines()
    if not defs:
        # Fallback: try args[0].relname
        name = _name_from_args(decl)
        if name is None:
            return []
        return [
            IvySymbol(
                name=name,
                kind=SymbolKind.Module,
                range=(0, 0, 0, 0),
                file_path=filename,
            )
        ]

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="object")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Module,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Action declarations
# ---------------------------------------------------------------------------


def _convert_action(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an ActionDecl to SymbolKind.Method with param/return detail.

    Actions are stateful procedures that can modify state, unlike pure
    functions/relations.  We use Method to distinguish them in the LSP
    symbol outline.
    """
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    # Use the leaf name (after last dot) for source scanning, since nested
    # actions have dotted names like "quic_packet_type.next".
    leaf_name = name.rsplit(".", 1)[-1] if "." in name else name
    rng = _loc_to_tuple(loc, source, name=leaf_name, keyword="action")
    detail = _extract_action_detail(decl)

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Method,
            range=rng,
            detail=detail,
            file_path=filename,
        )
    ]


def _extract_action_detail(decl: Any) -> Optional[str]:
    """Build a signature detail string from an ActionDecl's formal params/returns."""
    try:
        action_def = decl.args[0]  # ActionDef
        parts = []

        params = getattr(action_def, "formal_params", None)
        if params:
            param_strs = []
            for p in params:
                pname = getattr(p, "rep", str(p))
                # Strip the 'fml:' prefix
                pname = pname.replace("fml:", "")
                sort = getattr(p, "sort", None)
                if sort:
                    param_strs.append(f"{pname}:{sort}")
                else:
                    param_strs.append(pname)
            parts.append("(" + ", ".join(param_strs) + ")")

        returns = getattr(action_def, "formal_returns", None)
        if returns:
            ret_strs = []
            for r in returns:
                rname = getattr(r, "rep", str(r))
                rname = rname.replace("fml:", "")
                sort = getattr(r, "sort", None)
                if sort:
                    ret_strs.append(f"{rname}:{sort}")
                else:
                    ret_strs.append(rname)
            parts.append("returns (" + ", ".join(ret_strs) + ")")

        if parts:
            return " ".join(parts)
    except (IndexError, AttributeError):
        logger.debug("Could not extract action detail from %s", type(decl).__name__)
    return None


def _extract_constant_detail(atom: Any) -> Optional[str]:
    """Build a detail string from a constant/relation's sort and parameter sorts.

    For a relation ``connected(X:cid, Y:cid)`` returns
    ``"(X:cid, Y:cid) : bool"``.  For a plain constant ``val : nat``
    returns ``": nat"``.
    """
    try:
        parts = []
        # Extract parameter sorts (args of the atom, if any)
        args = getattr(atom, "args", None)
        if args and len(args) > 0:
            param_strs = []
            for p in args:
                p_name = (
                    getattr(p, "rep", None) or getattr(p, "relname", None) or str(p)
                )
                p_sort = getattr(p, "sort", None)
                if p_sort:
                    param_strs.append(f"{p_name}:{p_sort}")
                else:
                    param_strs.append(str(p_name))
            parts.append("(" + ", ".join(param_strs) + ")")

        # Extract the sort of the atom itself
        sort = getattr(atom, "sort", None)
        if sort:
            parts.append(f": {sort}")

        if parts:
            return " ".join(parts)
    except (IndexError, AttributeError):
        logger.debug("Could not extract constant detail")
    return None


# ---------------------------------------------------------------------------
# Constant declarations (including relations)
# ---------------------------------------------------------------------------


def _convert_constant_or_relation(
    decl: Any, filename: str, source: str
) -> List[IvySymbol]:
    """Convert a ConstantDecl, detecting relations (bool sort with args)."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None

    # Detect relation: ConstantDecl whose atom has args and bool sort
    kind = SymbolKind.Variable
    detail: Optional[str] = None
    try:
        arg0 = decl.args[0]
        has_args = hasattr(arg0, "args") and len(arg0.args) > 0
        sort = getattr(arg0, "sort", None)
        if has_args and str(sort) == "bool":
            kind = SymbolKind.Function
        # Extract sort info for the detail string
        detail = _extract_constant_detail(arg0)
    except (IndexError, AttributeError):
        logger.debug("Could not determine relation kind for %s", type(decl).__name__)

    kw = "relation" if kind == SymbolKind.Function else "function"
    rng = _loc_to_tuple(loc, source, name=name, keyword=kw)

    return [
        IvySymbol(
            name=name,
            kind=kind,
            range=rng,
            detail=detail,
            file_path=filename,
        )
    ]


def _convert_constant(
    decl: Any,
    filename: str,
    source: str,
    kind: SymbolKind,
) -> List[IvySymbol]:
    """Convert a ConstantDecl subclass (Destructor, Constructor)."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source)

    return [
        IvySymbol(
            name=name,
            kind=kind,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Alias declarations
# ---------------------------------------------------------------------------


def _convert_alias(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an AliasDecl to SymbolKind.Variable."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="alias")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Variable,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Isolate declarations
# ---------------------------------------------------------------------------


def _convert_isolate(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an IsolateDecl to SymbolKind.Namespace."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="isolate")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Namespace,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Module declarations
# ---------------------------------------------------------------------------


def _convert_module(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a ModuleDecl to SymbolKind.Module."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="module")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Module,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Labeled declarations (Axiom, Property, Conjecture)
# ---------------------------------------------------------------------------


def _convert_labeled(
    decl: Any,
    filename: str,
    source: str,
    kind: SymbolKind,
    keyword: Optional[str] = None,
) -> List[IvySymbol]:
    """Convert a LabeledDecl (Axiom/Property/Conjecture) to the given kind."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword=keyword)

    return [
        IvySymbol(
            name=name,
            kind=kind,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Definition declarations
# ---------------------------------------------------------------------------


def _convert_definition(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a DefinitionDecl to SymbolKind.Function."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    leaf_name = name.rsplit(".", 1)[-1] if "." in name else name
    rng = _loc_to_tuple(loc, source, name=leaf_name, keyword="definition")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Function,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Derived declarations
# ---------------------------------------------------------------------------


def _convert_derived(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a DerivedDecl to SymbolKind.Function."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    leaf_name = name.rsplit(".", 1)[-1] if "." in name else name
    rng = _loc_to_tuple(loc, source, name=leaf_name, keyword="derived")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Function,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Interpret declarations
# ---------------------------------------------------------------------------


def _convert_interpret(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an InterpretDecl to SymbolKind.TypeParameter."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="interpret")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.TypeParameter,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Schema declarations
# ---------------------------------------------------------------------------


def _convert_schema(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a SchemaDecl to SymbolKind.Interface."""
    defs = decl.defines()
    if not defs:
        return []

    symbols: List[IvySymbol] = []
    for defn in defs:
        name = defn[0]
        loc = defn[1] if len(defn) >= 2 else None
        rng = _loc_to_tuple(loc, source, name=name, keyword="schema")
        symbols.append(
            IvySymbol(
                name=name,
                kind=SymbolKind.Interface,
                range=rng,
                file_path=filename,
            )
        )
    return symbols


# ---------------------------------------------------------------------------
# Theorem declarations
# ---------------------------------------------------------------------------


def _convert_theorem(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a TheoremDecl to SymbolKind.Property."""
    defs = decl.defines()
    if not defs:
        return []

    symbols: List[IvySymbol] = []
    for defn in defs:
        name = defn[0]
        loc = defn[1] if len(defn) >= 2 else None
        rng = _loc_to_tuple(loc, source, name=name, keyword="theorem")
        symbols.append(
            IvySymbol(
                name=name,
                kind=SymbolKind.Property,
                range=rng,
                file_path=filename,
            )
        )
    return symbols


# ---------------------------------------------------------------------------
# Instantiate declarations
# ---------------------------------------------------------------------------


def _convert_instantiate(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an InstantiateDecl to SymbolKind.Variable."""
    defs = decl.defines()
    if not defs:
        return []

    name = defs[0][0]
    loc = defs[0][1] if len(defs[0]) >= 2 else None
    rng = _loc_to_tuple(loc, source, name=name, keyword="instance")

    return [
        IvySymbol(
            name=name,
            kind=SymbolKind.Variable,
            range=rng,
            file_path=filename,
        )
    ]


# ---------------------------------------------------------------------------
# Mixin declarations (before/after/implement)
# ---------------------------------------------------------------------------


def _convert_mixin(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a MixinDecl to SymbolKind.Method with detail showing relationship.

    Mixins (before/after/implement) are action monitors — they execute around
    an action call and can modify state, so they get Method like actions.
    MixinDecl.args[0] is a MixinDef subclass with mixer() and mixee() methods.
    """
    import ivy.ivy_ast as ia

    try:
        mixin_def = decl.args[0] if decl.args else None
        if mixin_def is None:
            return []

        mixee_name = mixin_def.mixee()
        if not mixee_name:
            return []

        # Determine the kind of mixin (before/after/implement)
        if isinstance(mixin_def, ia.MixinAfterDef):
            detail = f"after {mixee_name}"
        elif isinstance(mixin_def, ia.MixinImplementDef):
            detail = f"implement {mixee_name}"
        else:
            detail = f"before {mixee_name}"

        rng = _loc_to_tuple(getattr(decl, "lineno", None), source)

        return [
            IvySymbol(
                name=mixee_name,
                kind=SymbolKind.Method,
                range=rng,
                detail=detail,
                file_path=filename,
            )
        ]
    except (IndexError, AttributeError):
        logger.debug("Could not convert MixinDecl in %s", filename)
        return []


# ---------------------------------------------------------------------------
# Export/Import declarations
# ---------------------------------------------------------------------------


def _convert_export(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an ExportDecl to IvySymbol(s) with kind=Event.

    ExportDecl does not implement ``defines()``; instead we iterate
    ``decl.args`` (list of ExportDef) and extract the action name
    from each ``defn.args[0]`` atom.
    """
    symbols: List[IvySymbol] = []
    rng = _loc_to_tuple(getattr(decl, "lineno", None), source)
    for defn in getattr(decl, "args", []):
        atom = defn.args[0] if getattr(defn, "args", None) else None
        name = _atom_name(atom) if atom else None
        if name:
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Event,
                    range=rng,
                    detail=f"export {name}",
                    file_path=filename,
                )
            )
    return symbols


def _convert_import(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an ImportDecl to IvySymbol(s) with kind=Event.

    Same structure as ``_convert_export`` but for import declarations.
    """
    symbols: List[IvySymbol] = []
    rng = _loc_to_tuple(getattr(decl, "lineno", None), source)
    for defn in getattr(decl, "args", []):
        atom = defn.args[0] if getattr(defn, "args", None) else None
        name = _atom_name(atom) if atom else None
        if name:
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Event,
                    range=rng,
                    detail=f"import {name}",
                    file_path=filename,
                )
            )
    return symbols


# ---------------------------------------------------------------------------
# Native declarations (<<<...>>> C++ blocks)
# ---------------------------------------------------------------------------


def _convert_native(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert a NativeDecl (<<<...>>> C++ block) to SymbolKind.String.

    Uses the auto-generated label (e.g. 'native3') from NativeDef.args[0].
    """
    try:
        native_def = decl.args[0]
        label_atom = native_def.args[0]
        name = (
            getattr(label_atom, "rep", None)
            or getattr(label_atom, "relname", None)
            or "native"
        )
        rng = _loc_to_tuple(getattr(decl, "lineno", None), source)
        return [
            IvySymbol(
                name=name,
                kind=SymbolKind.String,
                range=rng,
                detail="native",
                file_path=filename,
            )
        ]
    except (IndexError, AttributeError):
        return []


# ---------------------------------------------------------------------------
# Attribute declarations (attribute foo = bar)
# ---------------------------------------------------------------------------


def _convert_attribute(decl: Any, filename: str, source: str) -> List[IvySymbol]:
    """Convert an AttributeDecl (attribute foo = bar) to SymbolKind.Constant."""
    try:
        attr_def = decl.args[0]
        name_atom = attr_def.args[0]
        val_atom = attr_def.args[1]
        name = (
            getattr(name_atom, "rep", None)
            or getattr(name_atom, "relname", None)
            or str(name_atom)
        )
        val = str(val_atom) if val_atom else ""
        rng = _loc_to_tuple(getattr(decl, "lineno", None), source)
        return [
            IvySymbol(
                name=name,
                kind=SymbolKind.Constant,
                range=rng,
                detail=f"attribute {name} = {val}",
                file_path=filename,
            )
        ]
    except (IndexError, AttributeError):
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_from_included_file(decl: Any, abs_filename: Optional[str]) -> bool:
    """Return True if *decl* originated from a different file via ``include``.

    When the Ivy parser processes ``include foo``, declarations from
    ``foo.ivy`` are merged into the importing file's AST.  Each AST
    node preserves its original source file in location objects returned
    by ``defines()`` and on inner AST nodes (``lineno.filename``).
    We skip those foreign declarations here because the workspace
    indexer will index the included file separately.
    """
    if abs_filename is None:
        return False

    # Strategy 1: check location from defines() — most reliable
    try:
        defs = decl.defines()
        if defs and len(defs[0]) >= 2:
            loc = defs[0][1]
            decl_filename = getattr(loc, "filename", None)
            if decl_filename is not None:
                return os.path.abspath(decl_filename) != abs_filename
    except (AttributeError, IndexError, TypeError, ValueError):
        pass

    # Strategy 2: check decl.lineno directly
    lineno = getattr(decl, "lineno", None)
    if lineno is not None:
        decl_filename = getattr(lineno, "filename", None)
        if decl_filename is not None:
            try:
                return os.path.abspath(decl_filename) != abs_filename
            except (TypeError, ValueError):
                pass

    # Strategy 3: check inner args for lineno (TypeDecl wraps TypeDef)
    for arg in getattr(decl, "args", ()):
        lineno = getattr(arg, "lineno", None)
        if lineno is not None:
            decl_filename = getattr(lineno, "filename", None)
            if decl_filename is not None:
                try:
                    return os.path.abspath(decl_filename) != abs_filename
                except (TypeError, ValueError):
                    pass

    return False


def _loc_to_tuple(
    loc: Any,
    source: str,
    name: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    """Convert an Ivy location to a 0-based 4-int tuple.

    Uses :func:`ivy_location_to_range` to handle the 1-based to 0-based
    conversion and line-length computation.  When the Ivy AST node has no
    line info (returns line 0), falls back to scanning the source text for
    the *keyword* + *name* pattern to determine the correct line.
    """
    rng = ivy_location_to_range(loc, source)
    if rng.start.line == 0 and rng.end.line == 0 and name and keyword:
        pattern = re.compile(
            r"^\s*" + re.escape(keyword) + r"\s+" + re.escape(name) + r"\b"
        )
        for i, line in enumerate(source.split("\n")):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                line_len = len(line)
                return (i, 0, i, line_len)
    return (
        rng.start.line,
        rng.start.character,
        rng.end.line,
        rng.end.character,
    )


def _name_from_args(decl: Any) -> Optional[str]:
    """Try to extract a name from ``decl.args[0].relname``."""
    try:
        return decl.args[0].relname
    except (IndexError, AttributeError):
        logger.debug("Could not extract name from args for %s", type(decl).__name__)
        return None


def _atom_name(atom: Any) -> Optional[str]:
    """Extract a name from an Ivy AST atom node.

    Checks ``relname`` first (preferred), then ``rep`` as fallback.
    Used by export/import converters where the atom is the action reference.
    """
    if atom is None:
        return None
    relname = getattr(atom, "relname", None)
    if relname:
        return relname
    rep = getattr(atom, "rep", None)
    if rep:
        return rep
    return None


# ---------------------------------------------------------------------------
# Reference extraction (Tier 1 — AST-based)
# ---------------------------------------------------------------------------


def extract_references_from_ast(
    ivy_obj: Any, filename: str, source: str
) -> List[SymbolReference]:
    """Extract symbol references (calls, instances, monitors) from a parsed Ivy AST.

    Args:
        ivy_obj: The result of ``ivy_parser.parse()`` (has ``.decls``).
        filename: The originating file path.
        source: The full source text.

    Returns:
        A list of ``SymbolReference`` instances.
    """
    if ivy_obj is None or not hasattr(ivy_obj, "decls"):
        return []

    references: List[SymbolReference] = []
    abs_filename = os.path.abspath(filename) if filename else filename

    for decl in ivy_obj.decls:
        try:
            if is_from_included_file(decl, abs_filename):
                continue
            refs = _extract_refs_from_decl(decl, filename)
            references.extend(refs)
        except Exception:
            logger.debug(
                "Failed to extract references from %s in %s",
                type(decl).__name__,
                filename,
                exc_info=True,
            )

    return references


def _extract_refs_from_decl(decl: Any, filename: str) -> List[SymbolReference]:
    """Extract references from a single AST declaration."""
    import ivy.ivy_ast as ia

    refs: List[SymbolReference] = []

    # 3a: CALLS from ActionDecl bodies
    if isinstance(decl, ia.ActionDecl):
        defs = decl.defines()
        if defs:
            action_name = defs[0][0]
            # ActionDecl.args[0] is ActionDef.  The body is either
            # in ActionDef.body (newer Ivy) or ActionDef.args[1] (the RHS).
            try:
                action_def = decl.args[0]
                body = getattr(action_def, "body", None)
                if body is None and len(getattr(action_def, "args", [])) >= 2:
                    body = action_def.args[1]
                if body is not None:
                    _extract_calls_from_body(body, action_name, filename, refs)
            except (IndexError, AttributeError):
                pass

    # 3b: USES from InstantiateDecl
    if isinstance(decl, ia.InstantiateDecl):
        defs = decl.defines()
        if defs:
            inst_name = defs[0][0]
            try:
                # args[0] is the instantiation expression (AppExpr or similar)
                app = decl.args[0]
                module_name = _atom_name(app) or getattr(app, "relname", None)
                if module_name:
                    lineno = getattr(decl, "lineno", None)
                    line = _get_line_number(lineno)
                    refs.append(
                        SymbolReference(
                            source_name=inst_name,
                            target_name=str(module_name),
                            kind="instance",
                            line=line,
                            file_path=filename,
                        )
                    )
            except (IndexError, AttributeError):
                pass

    # 3c: MONITORS from MixinDecl
    # MixinDecl.args[0] is MixinBeforeDef/MixinAfterDef, which has:
    #   args[0] = mixer Atom (e.g. connect[before2])
    #   args[1] = mixee Atom (e.g. connect)
    if isinstance(decl, ia.MixinDecl):
        try:
            mixin_def = decl.args[0] if decl.args else None
            if mixin_def is not None:
                mixin_args = getattr(mixin_def, "args", [])
                mixer_atom = mixin_args[0] if len(mixin_args) >= 1 else None
                mixee_atom = mixin_args[1] if len(mixin_args) >= 2 else None

                mixer_name = _atom_name(mixer_atom) if mixer_atom else None
                mixee_name = _atom_name(mixee_atom) if mixee_atom else None

                if mixer_name and mixee_name:
                    lineno = getattr(decl, "lineno", None)
                    line = _get_line_number(lineno)
                    refs.append(
                        SymbolReference(
                            source_name=str(mixer_name),
                            target_name=str(mixee_name),
                            kind="monitor",
                            line=line,
                            file_path=filename,
                        )
                    )
        except (IndexError, AttributeError):
            pass

    return refs


def _get_line_number(lineno: Any) -> int:
    """Extract 0-based line number from an Ivy lineno object."""
    if lineno is None:
        return 0
    if isinstance(lineno, int):
        return max(0, lineno - 1)  # Ivy uses 1-based
    line = getattr(lineno, "line", None)
    if line is not None:
        return max(0, int(line) - 1)
    return 0


def _extract_calls_from_body(
    body: Any, action_name: str, filename: str, refs: List[SymbolReference]
) -> None:
    """Recursively extract call references from an action body AST node."""
    if body is None:
        return

    try:
        import ivy.ivy_actions as iact
    except ImportError:
        return

    # CallAction: explicit call
    if isinstance(body, getattr(iact, "CallAction", type(None))):
        try:
            callee = body.args[0]
            callee_name = _atom_name(callee) or getattr(callee, "relname", None)
            if callee_name:
                lineno = getattr(body, "lineno", None)
                line = _get_line_number(lineno)
                refs.append(
                    SymbolReference(
                        source_name=action_name,
                        target_name=str(callee_name),
                        kind="call",
                        line=line,
                        file_path=filename,
                    )
                )
        except (IndexError, AttributeError):
            pass

    # AssignAction: check RHS for applications (function calls)
    if isinstance(body, getattr(iact, "AssignAction", type(None))):
        try:
            # RHS is args[1] typically
            if len(body.args) >= 2:
                rhs = body.args[1]
                rhs_name = _atom_name(rhs) or getattr(rhs, "relname", None)
                if rhs_name and hasattr(rhs, "args") and rhs.args:
                    lineno = getattr(body, "lineno", None)
                    line = _get_line_number(lineno)
                    refs.append(
                        SymbolReference(
                            source_name=action_name,
                            target_name=str(rhs_name),
                            kind="call",
                            line=line,
                            file_path=filename,
                        )
                    )
        except (IndexError, AttributeError):
            pass

    # Recurse into compound actions (Sequence, IfAction, WhileAction, etc.)
    for arg in getattr(body, "args", []):
        _extract_calls_from_body(arg, action_name, filename, refs)
