"""Enrich RequirementGraph and SemanticModel from CompiledModuleIR.

Called after a subprocess compilation completes to upgrade existing
Tier 1/2 data with Tier 3 compiled information.
"""
from __future__ import annotations

import logging
from typing import Any, List, Tuple

from ivy_lsp.compilation.ir import CompiledModuleIR
from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.nodes import SymbolNode, TypeNode

logger = logging.getLogger(__name__)


def enrich_semantic_model(
    model: Any,
    ir: CompiledModuleIR,
    filepath: str,
) -> None:
    """Update the SemanticModel with Tier 3 compiled data.

    Creates TypeNode entries for each sort, SymbolNode entries for each
    symbol and action, and HAS_PARAM edges linking symbols to their
    domain sorts.  Calls ``model.update_file()`` to atomically replace
    any existing tier3 data for *filepath*.

    Gracefully returns without mutation when ``ir.success`` is False.
    """
    if not ir.success:
        logger.info(
            "Skipping semantic model enrichment for %s: compilation failed",
            filepath,
        )
        return

    nodes: List[Any] = []
    edges: List[Tuple[str, SemanticEdgeType, str]] = []

    # --- Sorts -> TypeNode ---
    for sort_name, sort_ir in ir.sorts.items():
        node_id = f"compiled:{filepath}:{sort_name}"
        nodes.append(
            TypeNode(
                id=node_id,
                name=sort_name.rsplit(".", 1)[-1],
                qualified_name=sort_name,
                file=filepath,
                line=0,
                sort_name=sort_name,
                is_enum=sort_ir.is_enumerated,
                variants=list(sort_ir.constructors),
                tier="tier3",
            )
        )

    # --- Symbols -> SymbolNode ---
    for sym_name, sym_ir in ir.symbols.items():
        kind: str = "relation" if sym_ir.is_relation else "function"
        if sym_ir.is_destructor:
            kind = "destructor"
        elif sym_ir.is_constructor:
            kind = "constructor"

        node_id = f"compiled:{filepath}:{sym_name}"
        nodes.append(
            SymbolNode(
                id=node_id,
                name=sym_name.rsplit(".", 1)[-1],
                qualified_name=sym_name,
                kind=kind,
                file=filepath,
                line=0,
                sort_name=sym_ir.sort_str,
                arity=len(sym_ir.domain_sorts),
                params=[
                    f"arg{i}:{s}" for i, s in enumerate(sym_ir.domain_sorts)
                ],
                return_sort=sym_ir.range_sort,
                tier="tier3",
            )
        )

        for ds in sym_ir.domain_sorts:
            edges.append((node_id, SemanticEdgeType.HAS_PARAM, ds))

    # --- Actions -> SymbolNode (kind="action") ---
    for action_name, action_ir in ir.actions.items():
        node_id = f"compiled:{filepath}:{action_name}"
        nodes.append(
            SymbolNode(
                id=node_id,
                name=action_name.rsplit(".", 1)[-1],
                qualified_name=action_name,
                kind="action",
                file=filepath,
                line=0,
                sort_name="action",
                arity=len(action_ir.formal_params),
                params=list(action_ir.formal_params),
                return_sort=(
                    action_ir.formal_returns[0]
                    if action_ir.formal_returns
                    else None
                ),
                tier="tier3",
            )
        )

    model.update_file(filepath, nodes, edges, "tier3")
    logger.debug(
        "Enriched semantic model for %s: %d nodes, %d edges",
        filepath,
        len(nodes),
        len(edges),
    )


def enrich_requirement_graph(
    graph: Any,
    ir: CompiledModuleIR,
) -> None:
    """Apply compiled module data to the RequirementGraph.

    Creates ActionNode entries for each action in the IR that is not
    already present in the graph.  Uses the public ``add_action()``
    API which handles locking internally.

    Gracefully returns without mutation when ``ir.success`` is False.
    """
    if not ir.success:
        logger.info(
            "Skipping requirement graph enrichment for %s: compilation failed",
            ir.source_file,
        )
        return

    from ivy_lsp.analysis.requirement_graph import ActionNode

    for action_name in ir.actions:
        graph.add_action_if_absent(
            ActionNode(
                id=action_name,
                name=action_name.rsplit(".", 1)[-1],
                qualified_name=action_name,
                file=ir.source_file,
                line=0,
            )
        )

    logger.debug(
        "Enriched requirement graph from %s: %d actions",
        ir.source_file,
        len(ir.actions),
    )
