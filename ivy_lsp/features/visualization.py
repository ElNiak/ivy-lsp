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


# ---------------------------------------------------------------------------
# Handler: ivy/coverageGaps
# ---------------------------------------------------------------------------


def handle_coverage_gaps(server: Any, params: dict) -> dict:
    """Handle ivy/coverageGaps request.

    Identifies coverage gaps in the formal model:
    - Unguarded state variables: written but not read by any requirement
    - Orphan requirements: monitor_action references a non-existent action
    - Uncovered RFC requirements: RFC requirements with no matching bracket tag
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "unguardedStateVars": [],
            "uncoveredRfcRequirements": [],
            "orphanRequirements": [],
            "summary": {
                "totalActions": 0,
                "totalRequirements": 0,
                "totalStateVars": 0,
                "unguardedCount": 0,
                "totalRfcReqs": 0,
                "uncoveredRfcCount": 0,
                "orphanReqCount": 0,
            },
            "scopeInfo": {"testFile": None, "scoped": False},
        }

    scope_info = _resolve_scope(graph, params)

    # -- Unguarded state variables ------------------------------------------
    # A state var is "guarded" if any requirement or property READS it.
    guarded_vars: Set[str] = set()
    for req_id in graph.requirements:
        for etype, target_id in graph._outgoing.get(req_id, []):
            if etype == EdgeType.READS:
                guarded_vars.add(target_id)
    for prop_id in graph.properties:
        for etype, target_id in graph._outgoing.get(prop_id, []):
            if etype == EdgeType.READS:
                guarded_vars.add(target_id)

    # Collect state vars that are written (via WRITES edges)
    written_vars: Set[str] = set()
    for _, etype, target_id in graph.edges:
        if etype == EdgeType.WRITES:
            written_vars.add(target_id)

    unguarded: List[dict] = []
    for var_id, var_node in graph.state_vars.items():
        is_guarded = var_id in guarded_vars
        if not is_guarded:
            is_written = var_id in written_vars
            severity = "high" if is_written else "low"
            unguarded.append(
                {
                    "name": var_node.name,
                    "qualifiedName": var_node.qualified_name,
                    "file": var_node.file,
                    "line": var_node.line,
                    "isWritten": is_written,
                    "guardedByRequirements": 0,
                    "severity": severity,
                }
            )

    # -- Uncovered RFC requirements -----------------------------------------
    uncovered_rfc = graph.get_uncovered_requirements()
    uncovered_rfc_list: List[dict] = []
    for rfc_req in uncovered_rfc:
        uncovered_rfc_list.append(
            {
                "id": rfc_req.id,
                "rfc": getattr(rfc_req, "rfc", ""),
                "section": getattr(rfc_req, "section", ""),
                "level": getattr(rfc_req, "level", ""),
                "text": getattr(rfc_req, "text", ""),
            }
        )

    # -- Orphan requirements ------------------------------------------------
    # Requirements whose monitor_action does not match any known action.
    orphans: List[dict] = []
    for req in graph.requirements.values():
        if req.monitor_action and req.monitor_action not in graph.actions:
            orphans.append(
                {
                    "id": req.id,
                    "kind": req.kind,
                    "formulaText": req.formula_text,
                    "file": req.file,
                    "line": req.line,
                    "reason": (
                        f"Action '{req.monitor_action}' not found in graph"
                    ),
                }
            )

    return {
        "unguardedStateVars": unguarded,
        "uncoveredRfcRequirements": uncovered_rfc_list,
        "orphanRequirements": orphans,
        "summary": {
            "totalActions": len(graph.actions),
            "totalRequirements": len(graph.requirements),
            "totalStateVars": len(graph.state_vars),
            "unguardedCount": len(unguarded),
            "totalRfcReqs": len(graph.rfc_requirements),
            "uncoveredRfcCount": len(uncovered_rfc_list),
            "orphanReqCount": len(orphans),
        },
        "scopeInfo": {
            "testFile": scope_info.get("testFile"),
            "scoped": scope_info.get("scoped", False),
        },
    }


# ---------------------------------------------------------------------------
# Handler: ivy/actionDependencyGraph
# ---------------------------------------------------------------------------


def handle_action_dependency_graph(server: Any, params: dict) -> dict:
    """Handle ivy/actionDependencyGraph request.

    Builds a graph where actions are nodes and edges represent shared
    state variables (action A writes a var that action B reads).
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "nodes": [],
            "edges": [],
            "scopeInfo": {"testFile": None, "scoped": False},
        }

    scope_info = _resolve_scope(graph, params)
    include_state_vars = params.get("includeStateVars", False)

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    # Build action nodes
    for action_id, action_node in graph.actions.items():
        reqs = graph.get_requirements_for_action(action_id)
        nodes.append(
            {
                "id": action_id,
                "label": action_node.name,
                "type": "action",
                "file": action_node.file,
                "line": action_node.line,
                "requirementCount": len(reqs),
            }
        )

    # Build writer/reader maps: state_var_id -> set of action_ids
    writers: Dict[str, Set[str]] = defaultdict(set)
    readers: Dict[str, Set[str]] = defaultdict(set)

    for action_id in graph.actions:
        reqs = graph.get_requirements_for_action(action_id)
        for req in reqs:
            for etype, target_id in graph._outgoing.get(req.id, []):
                if etype == EdgeType.WRITES:
                    writers[target_id].add(action_id)
                elif etype == EdgeType.READS:
                    readers[target_id].add(action_id)

    # Create edges between actions that share state vars (writer -> reader)
    seen_edges: Set[tuple] = set()
    for var_id in set(writers.keys()) | set(readers.keys()):
        writer_actions = writers.get(var_id, set())
        reader_actions = readers.get(var_id, set())
        for w in writer_actions:
            for r in reader_actions:
                if w != r:
                    edge_key = (w, r)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        var_node = graph.state_vars.get(var_id)
                        label = var_node.name if var_node else var_id
                        edges.append(
                            {
                                "source": w,
                                "target": r,
                                "label": label,
                                "type": "shared_state",
                            }
                        )

    # Optionally include state var nodes with writes/reads edges
    if include_state_vars:
        for var_id, var_node in graph.state_vars.items():
            if var_id in writers or var_id in readers:
                nodes.append(
                    {
                        "id": var_id,
                        "label": var_node.name,
                        "type": "stateVar",
                        "file": var_node.file,
                        "line": var_node.line,
                    }
                )
                for w in writers.get(var_id, set()):
                    edges.append(
                        {
                            "source": w,
                            "target": var_id,
                            "type": "writes",
                        }
                    )
                for r in readers.get(var_id, set()):
                    edges.append(
                        {
                            "source": var_id,
                            "target": r,
                            "type": "reads",
                        }
                    )

    return {
        "nodes": nodes,
        "edges": edges,
        "scopeInfo": {
            "testFile": scope_info.get("testFile"),
            "scoped": scope_info.get("scoped", False),
        },
    }


# ---------------------------------------------------------------------------
# Handler: ivy/stateMachineView
# ---------------------------------------------------------------------------


def handle_state_machine_view(server: Any, params: dict) -> dict:
    """Handle ivy/stateMachineView request.

    Models the Ivy specification as a state machine where:
    - State variables are state nodes
    - Actions are transitions between state nodes (via READS/WRITES edges)
    - Guards are require/assume clauses on the action's monitors
    - Invariants are properties that constrain active state variables
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "nodes": [],
            "transitions": [],
            "scopeInfo": {"testFile": None, "scoped": False},
        }

    scope_info = _resolve_scope(graph, params)
    state_var_filter = params.get("stateVarFilter")

    nodes: List[Dict[str, Any]] = []
    transitions: List[Dict[str, Any]] = []

    # Identify state vars that participate in action monitors
    active_vars: Set[str] = set()
    for action_id in graph.actions:
        reqs = graph.get_requirements_for_action(action_id)
        for req in reqs:
            for etype, target_id in graph._outgoing.get(req.id, []):
                if etype in (EdgeType.READS, EdgeType.WRITES):
                    active_vars.add(target_id)

    # Build state nodes
    for var_id, var_node in graph.state_vars.items():
        if var_id not in active_vars:
            continue
        if state_var_filter and var_node.name != state_var_filter:
            continue
        nodes.append(
            {
                "id": var_id,
                "label": var_node.name,
                "type": "state",
                "file": var_node.file,
                "line": var_node.line,
            }
        )

    # Add invariant nodes (properties that constrain active state vars)
    for prop_id, prop_node in graph.properties.items():
        prop_vars = {
            target
            for etype, target in graph._outgoing.get(prop_id, [])
            if etype == EdgeType.READS
        }
        if prop_vars & active_vars:
            nodes.append(
                {
                    "id": prop_id,
                    "label": prop_node.name or prop_node.formula_text[:40],
                    "type": "invariant",
                    "file": prop_node.file,
                    "line": prop_node.line,
                }
            )

    active_var_ids = {n["id"] for n in nodes if n["type"] == "state"}

    # Build transitions: action connects source state vars to target state vars
    for action_id, action_node in graph.actions.items():
        reqs = graph.get_requirements_for_action(action_id)

        read_vars: Set[str] = set()
        write_vars: Set[str] = set()
        guards: List[str] = []

        for req in reqs:
            for etype, target_id in graph._outgoing.get(req.id, []):
                if etype == EdgeType.READS and target_id in active_var_ids:
                    read_vars.add(target_id)
                elif etype == EdgeType.WRITES and target_id in active_var_ids:
                    write_vars.add(target_id)
            if req.kind in ("require", "assume"):
                guards.append(req.formula_text)

        # Create transitions: from each read var to each written var
        sources = read_vars if read_vars else write_vars
        targets = write_vars if write_vars else read_vars

        for src in sources:
            for tgt in targets:
                transitions.append(
                    {
                        "source": src,
                        "target": tgt,
                        "action": action_node.name,
                        "guards": guards,
                    }
                )

    return {
        "nodes": nodes,
        "transitions": transitions,
        "scopeInfo": {
            "testFile": scope_info.get("testFile"),
            "scoped": scope_info.get("scoped", False),
        },
    }


# ---------------------------------------------------------------------------
# LSP wiring
# ---------------------------------------------------------------------------


def register(server: Any) -> None:
    """Register visualization request handlers on the server."""

    @server.feature("ivy/actionRequirements")
    def on_action_requirements(params: Any = None) -> Dict[str, Any]:
        return handle_action_requirements(server, params or {})

    @server.feature("ivy/modelSummaryTable")
    def on_model_summary_table(params: Any = None) -> Dict[str, Any]:
        return handle_model_summary_table(server, params or {})

    @server.feature("ivy/coverageGaps")
    def on_coverage_gaps(params: Any = None) -> Dict[str, Any]:
        return handle_coverage_gaps(server, params or {})

    @server.feature("ivy/actionDependencyGraph")
    def on_action_dependency_graph(params: Any = None) -> Dict[str, Any]:
        return handle_action_dependency_graph(server, params or {})

    @server.feature("ivy/stateMachineView")
    def on_state_machine_view(params: Any = None) -> Dict[str, Any]:
        return handle_state_machine_view(server, params or {})
