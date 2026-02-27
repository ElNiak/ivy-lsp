"""Visualization data endpoint handlers for the Ivy LSP server.

Provides handlers for model visualization features: action boundary
requirements, summary tables, coverage gaps, dependency graphs, etc.
All handlers follow the pure-function pattern from monitoring.py.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from ivy_lsp.analysis.requirement_graph import (
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_requirement_graph(server: Any) -> Optional[RequirementGraph]:
    """Safely extract the requirement graph from the indexer."""
    try:
        indexer = server._indexer
        if indexer is None:
            return None
        graph = getattr(indexer, "_requirement_graph", None)
        return graph
    except AttributeError:
        return None


def _resolve_scope(graph: Any, params: dict) -> dict:
    """Determine active scope from params or active test."""
    from ivy_lsp.analysis.test_scope import ScopedRequirementModel

    test_file = params.get("testFile")
    scope = None
    if isinstance(graph, ScopedRequirementModel):
        if test_file is None:
            active = graph.get_active_scope()
            if active:
                test_file = active.test_file
                scope = active
        else:
            scope = graph._test_scopes.get(test_file)
    return {"testFile": test_file, "scoped": scope is not None, "_scope": scope}


def _serialize_requirement(req: RequirementNode, graph: RequirementGraph) -> dict:
    """Convert a RequirementNode to a JSON-serializable dict."""
    state_vars_read = [sv.name for sv in graph.get_state_vars_read_by(req.id)]

    nct = None
    try:
        from ivy_lsp.analysis.test_scope import classify_requirement

        nct = classify_requirement(req).value
    except Exception:
        pass

    return {
        "id": req.id,
        "kind": req.kind,
        "formulaText": req.formula_text,
        "line": req.line,
        "file": req.file,
        "bracketTags": list(req.bracket_tags),
        "stateVarsRead": state_vars_read,
        "nctClassification": nct,
    }


def _serialize_state_var(sv: StateVarNode) -> dict:
    """Convert a StateVarNode to a JSON-serializable dict."""
    return {
        "name": sv.name,
        "qualifiedName": sv.qualified_name,
        "file": sv.file,
        "line": sv.line,
        "isRelation": sv.is_relation,
    }


# ---------------------------------------------------------------------------
# Handler: ivy/actionRequirements
# ---------------------------------------------------------------------------


def handle_action_requirements(server: Any, params: dict) -> dict:
    """Handle ivy/actionRequirements request.

    Returns per-action requirement breakdown with before/after monitors,
    counts, RFC tags, and state variable information.
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "actions": [],
            "scopeInfo": {"testFile": None, "scoped": False},
            "modelReady": False,
        }

    scope_info = _resolve_scope(graph, params)
    action_filter = params.get("actionName")
    file_filter = params.get("filePath")

    actions_to_process = dict(graph.actions)
    if action_filter:
        actions_to_process = {
            k: v
            for k, v in actions_to_process.items()
            if v.name == action_filter
            or v.qualified_name == action_filter
            or k == action_filter
        }
    if file_filter:
        actions_to_process = {
            k: v for k, v in actions_to_process.items() if v.file == file_filter
        }

    result_actions = []
    for action_id, action_node in actions_to_process.items():
        reqs = graph.get_requirements_for_action(action_id)
        before = [r for r in reqs if r.mixin_kind in ("before", "direct")]
        after = [r for r in reqs if r.mixin_kind == "after"]
        implement = [r for r in reqs if r.mixin_kind == "implement"]

        all_tags: Set[str] = set()
        counts: Dict[str, int] = defaultdict(int)
        for r in reqs:
            counts[r.kind] += 1
            all_tags.update(r.bracket_tags)

        direction = None
        scope = scope_info.get("_scope")
        if scope:
            try:
                from ivy_lsp.analysis.test_scope import classify_action_direction

                direction = classify_action_direction(action_id, scope).value
            except Exception:
                pass

        state_vars_read: List[StateVarNode] = []
        state_vars_written = graph.get_state_vars_written_in_monitor(action_id)

        result_actions.append(
            {
                "actionName": action_node.name,
                "qualifiedName": action_node.qualified_name,
                "file": action_node.file,
                "line": action_node.line,
                "direction": direction,
                "monitors": {
                    "before": [_serialize_requirement(r, graph) for r in before],
                    "after": [_serialize_requirement(r, graph) for r in after],
                    "direct": [
                        _serialize_requirement(r, graph) for r in implement
                    ],
                },
                "stateVarsRead": [
                    _serialize_state_var(sv) for sv in state_vars_read
                ],
                "stateVarsWritten": [
                    _serialize_state_var(sv) for sv in state_vars_written
                ],
                "rfcTags": sorted(all_tags),
                "counts": {
                    "require": counts.get("require", 0),
                    "ensure": counts.get("ensure", 0),
                    "assume": counts.get("assume", 0),
                    "assert": counts.get("assert", 0),
                    "total": len(reqs),
                },
            }
        )

    return {
        "actions": result_actions,
        "scopeInfo": {
            "testFile": scope_info.get("testFile"),
            "scoped": scope_info.get("scoped", False),
        },
        "modelReady": True,
    }


# ---------------------------------------------------------------------------
# Handler: ivy/modelSummaryTable
# ---------------------------------------------------------------------------


def handle_model_summary_table(server: Any, params: dict) -> dict:
    """Handle ivy/modelSummaryTable request.

    Returns a flat summary table with one row per action and aggregated
    totals for requirements, state variables, and RFC coverage.
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "rows": [],
            "totals": {
                "actions": 0,
                "requirements": 0,
                "stateVars": 0,
                "rfcTagsCovered": 0,
                "rfcTagsTotal": 0,
            },
            "scopeInfo": {"testFile": None, "scoped": False},
        }

    scope_info = _resolve_scope(graph, params)
    rows: List[dict] = []
    total_reqs = 0

    for action_id, action_node in graph.actions.items():
        reqs = graph.get_requirements_for_action(action_id)
        before_reqs = [r for r in reqs if r.mixin_kind in ("before", "direct")]
        after_reqs = [r for r in reqs if r.mixin_kind == "after"]

        before_require = sum(1 for r in before_reqs if r.kind == "require")
        before_ensure = sum(1 for r in before_reqs if r.kind == "ensure")
        after_require = sum(1 for r in after_reqs if r.kind == "require")
        after_ensure = sum(1 for r in after_reqs if r.kind == "ensure")
        assume_count = sum(1 for r in reqs if r.kind == "assume")
        assert_count = sum(1 for r in reqs if r.kind == "assert")

        rfc_tags: Set[str] = set()
        for r in reqs:
            rfc_tags.update(r.bracket_tags)

        # Aggregate state vars read across all requirements for this action
        vars_read_ids: Set[str] = set()
        for r in reqs:
            for sv in graph.get_state_vars_read_by(r.id):
                vars_read_ids.add(sv.id)
        vars_written = graph.get_state_vars_written_in_monitor(action_id)

        direction = None
        scope = scope_info.get("_scope")
        if scope:
            try:
                from ivy_lsp.analysis.test_scope import classify_action_direction

                direction = classify_action_direction(action_id, scope).value
            except Exception:
                pass

        total_reqs += len(reqs)
        rows.append(
            {
                "actionName": action_node.name,
                "qualifiedName": action_node.qualified_name,
                "file": action_node.file,
                "line": action_node.line,
                "direction": direction,
                "beforeRequireCount": before_require,
                "beforeEnsureCount": before_ensure,
                "afterRequireCount": after_require,
                "afterEnsureCount": after_ensure,
                "assumeCount": assume_count,
                "assertCount": assert_count,
                "totalRequirements": len(reqs),
                "stateVarsRead": len(vars_read_ids),
                "stateVarsWritten": len(vars_written),
                "rfcTagsCovered": sorted(rfc_tags),
                "rfcCoverageCount": len(rfc_tags),
            }
        )

    coverage = graph.get_coverage_stats()
    return {
        "rows": rows,
        "totals": {
            "actions": len(graph.actions),
            "requirements": total_reqs,
            "stateVars": len(graph.state_vars),
            "rfcTagsCovered": coverage.get("covered", 0),
            "rfcTagsTotal": coverage.get("total", 0),
        },
        "scopeInfo": {
            "testFile": scope_info.get("testFile"),
            "scoped": scope_info.get("scoped", False),
        },
    }
