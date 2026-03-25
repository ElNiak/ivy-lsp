"""Map IvySymbol instances to SemanticModel nodes.

Bridges the ``IvySymbol`` output of ``TieredExtractor`` with the
``SymbolNode`` / ``TypeNode`` nodes consumed by the ``SemanticModel``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, List, Literal, Optional, Tuple

from lsprotocol.types import SymbolKind

if TYPE_CHECKING:
    from ivy_lsp.core.parsing.symbols import IvySymbol
    from ivy_lsp.core.semantic.model import SemanticModel

logger = logging.getLogger(__name__)

# Detail strings that refine SymbolKind.Function
_FUNCTION_DETAIL_MAP: dict[str, str] = {
    "action": "action",
    "relation": "relation",
    "function": "function",
}

# Detail strings that refine SymbolKind.Module
_MODULE_DETAIL_MAP: dict[str, str] = {
    "object": "object",
    "module": "module",
    "isolate": "isolate",
}

# Regex to extract params from detail strings like "(x:nat, y:bool)"
_PARAMS_RE = re.compile(r"\(([^)]*)\)")
# Regex to extract return sort from detail strings like "returns (ok:bool)"
_RETURNS_RE = re.compile(r"returns\s*\(([^)]*)\)")


def _tier_label(tier_used: int) -> Literal["tier1", "tier2", "tier3"]:
    """Convert numeric tier to the Tier literal expected by node dataclasses."""
    if tier_used == 1:
        return "tier1"
    if tier_used == 2:
        return "tier2"
    return "tier3"


def _parse_params(detail: Optional[str]) -> Tuple[List[str], Optional[str]]:
    """Extract parameter list and return sort from a detail string.

    Returns ``(params, return_sort)`` where *params* is a list of
    stripped parameter strings and *return_sort* is the first return
    type string or ``None``.
    """
    params: List[str] = []
    return_sort: Optional[str] = None

    if not detail:
        return params, return_sort

    # Extract return sort first (so the returns(...) isn't captured as params)
    ret_match = _RETURNS_RE.search(detail)
    if ret_match:
        return_sort = ret_match.group(1).strip()

    # Extract params (first parenthesized group that is NOT the returns clause)
    param_match = _PARAMS_RE.search(detail)
    if param_match:
        raw = param_match.group(1)
        # Make sure this isn't the returns(...) group
        if ret_match and param_match.start() == ret_match.start() + len("returns "):
            pass  # skip, this is the returns group
        elif raw.strip():
            params = [p.strip() for p in raw.split(",") if p.strip()]

    return params, return_sort


def populate_model_from_symbols(
    model: SemanticModel,
    symbols: List[IvySymbol],
    filepath: str,
    tier_used: int = 3,
) -> int:
    """Populate a ``SemanticModel`` with nodes derived from ``IvySymbol`` instances.

    Parameters
    ----------
    model:
        The semantic model to populate.
    symbols:
        Flat or hierarchical list of ``IvySymbol`` from any extraction tier.
    filepath:
        Absolute path of the source file these symbols came from.
    tier_used:
        Which extraction tier produced these symbols (1, 2, or 3).

    Returns:
        The number of nodes added.
    """
    from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

    tier = _tier_label(tier_used)
    count = 0

    def _walk(syms: List[IvySymbol], prefix: str) -> None:
        nonlocal count
        for sym in syms:
            qname = f"{prefix}.{sym.name}" if prefix else sym.name
            count += _add_symbol_node(
                model,
                sym,
                filepath,
                tier,
                SymbolNode,
                TypeNode,
                qualified_name_override=qname if prefix else None,
            )
            _walk(sym.children, qname)

    _walk(symbols, "")
    return count


def _add_symbol_node(
    model: SemanticModel,
    sym: IvySymbol,
    filepath: str,
    tier: Literal["tier1", "tier2", "tier3"],
    SymbolNode: type,
    TypeNode: type,
    qualified_name_override: Optional[str] = None,
) -> int:
    """Add a single IvySymbol as the appropriate SemanticModel node.

    Returns 1 if a node was added, 0 otherwise.
    """
    name = sym.name
    qualified_name = qualified_name_override or name
    line = sym.range[0] + 1  # IvySymbol range is 0-based, nodes use 1-based
    node_id = f"{filepath}:{line}:{name}"
    detail = sym.detail

    # -- TypeNode: SymbolKind.Class -----------------------------------------
    if sym.kind == SymbolKind.Class:
        is_enum = bool(detail and detail.startswith("enum:"))
        variants: List[str] = []
        if is_enum and detail:
            variants_raw = detail[len("enum:") :].strip()
            variants = [v.strip() for v in variants_raw.split(",") if v.strip()]
        model.add_node(
            TypeNode(
                id=node_id,
                name=name,
                qualified_name=qualified_name,
                file=filepath,
                line=line,
                is_enum=is_enum,
                variants=variants,
                tier=tier,
            )
        )
        return 1

    # -- SymbolNode: Function-family ----------------------------------------
    if sym.kind in (SymbolKind.Function, SymbolKind.Method):
        # Determine kind from detail string
        kind = "action" if sym.kind == SymbolKind.Method else "function"
        if detail:
            detail_lower = detail.lower().strip()
            for key, mapped_kind in _FUNCTION_DETAIL_MAP.items():
                if detail_lower.startswith(key) or detail_lower.startswith("("):
                    kind = mapped_kind
                    break
            # If detail starts with "(" it's likely a relation with params
            if detail_lower.startswith("(") and ":" not in detail_lower.split(")")[0]:
                kind = "relation"

        params, return_sort = _parse_params(detail)
        model.add_node(
            SymbolNode(
                id=node_id,
                name=name,
                qualified_name=qualified_name,
                kind=kind,
                file=filepath,
                line=line,
                params=params,
                return_sort=return_sort,
                tier=tier,
            )
        )
        return 1

    # -- SymbolNode: Module-family ------------------------------------------
    if sym.kind in (SymbolKind.Module, SymbolKind.Namespace):
        kind = "object"  # default for Module
        if sym.kind == SymbolKind.Namespace:
            kind = "isolate"
        elif detail:
            for key, mapped_kind in _MODULE_DETAIL_MAP.items():
                if key in detail.lower():
                    kind = mapped_kind
                    break
        model.add_node(
            SymbolNode(
                id=node_id,
                name=name,
                qualified_name=qualified_name,
                kind=kind,
                file=filepath,
                line=line,
                tier=tier,
            )
        )
        return 1

    # -- SymbolNode: Variable (individual, alias, instance) -----------------
    if sym.kind == SymbolKind.Variable:
        sort_name = None
        if detail and detail.startswith(":"):
            sort_name = detail[1:].strip()
        model.add_node(
            SymbolNode(
                id=node_id,
                name=name,
                qualified_name=qualified_name,
                kind="individual",
                file=filepath,
                line=line,
                sort_name=sort_name,
                tier=tier,
            )
        )
        return 1

    # -- Skip non-model symbols (Property, File/include, Event, etc.) -------
    return 0
