"""Visualization data endpoint handlers for the Ivy LSP server.

Provides handlers for model visualization features: action boundary
requirements, summary tables, coverage gaps, dependency graphs, etc.
All handlers follow the pure-function pattern from monitoring.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from ivy_lsp.analysis.requirement_graph import (
    EdgeType,
    GraphSnapshot,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.protocols import IvyServerProtocol
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


# ---------------------------------------------------------------------------
# Response size cap
# ---------------------------------------------------------------------------

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB


def _cap_response(response: dict, list_key: str) -> dict:
    """Truncate the main list in a response if serialized size exceeds MAX_RESPONSE_BYTES."""
    encoded = json.dumps(response)
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return response
    items = response.get(list_key, [])
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        response[list_key] = items[:mid]
        if len(json.dumps(response)) <= MAX_RESPONSE_BYTES:
            lo = mid
        else:
            hi = mid - 1
    response[list_key] = items[:lo]
    response["truncated"] = True
    response["totalCount"] = len(items)
    return response


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_requirement_graph(server: IvyServerProtocol) -> Optional[RequirementGraph]:
    """Safely extract the requirement graph from the indexer."""
    try:
        indexer = server.indexer
        if indexer is None:
            if server.initializing:
                logger.debug("_get_requirement_graph: server still initializing")
            else:
                logger.warning(
                    "_get_requirement_graph: indexer is None (server not initialized?)"
                )
            return None
        graph = indexer.requirement_graph
        if graph is None:
            logger.warning("_get_requirement_graph: requirement_graph is None")
        return graph
    except AttributeError:
        logger.warning(
            "_get_requirement_graph: AttributeError accessing server.indexer"
        )
        return None


def _resolve_scope(graph: Any, params: dict) -> dict:
    """Determine active scope from params or active test or filePath."""
    from ivy_lsp.analysis.test_scope import ScopedRequirementModel

    test_file = params.get("testFile")
    file_path = params.get("filePath", "")
    scope = None
    if isinstance(graph, ScopedRequirementModel):
        if test_file is None:
            # Try to derive scope from filePath
            if file_path:
                tests = graph.get_tests_for_file(file_path)
                if tests:
                    test_file = next(iter(tests))  # Use first matching endpoint mirror
                    scope = graph.get_test_scope(test_file)
            # Fall back to active scope
            if scope is None:
                active = graph.get_active_scope()
                if active:
                    test_file = active.test_file
                    scope = active
        else:
            scope = graph.get_test_scope(test_file)
    return {"testFile": test_file, "scoped": scope is not None, "_scope": scope}


def _filter_by_scope(items: dict, scope_info: dict) -> dict:
    """Filter a dict of ActionNode/StateVarNode by scope's include_closure.

    Returns unmodified dict if no scope is active.
    """
    scope = scope_info.get("_scope")
    if scope is None:
        return items
    closure = getattr(scope, "include_closure", None)
    if not closure:
        return items
    return {k: v for k, v in items.items() if v.file in closure}


def _filter_by_protocol(items: dict, protocol_filter: Optional[str]) -> dict:
    """Filter items by protocol directory path substring."""
    if not protocol_filter:
        return items
    return {k: v for k, v in items.items() if protocol_filter in v.file}


def _rel(path: str, server: Any) -> str:
    """Relativize a path against server's workspace root."""
    ws_root = getattr(server, "workspace_root", "")
    if not path or not ws_root:
        return path
    if path.startswith(ws_root):
        rel = path[len(ws_root) :]
        return rel.lstrip(os.sep)
    return path


def _classify_direction(action_id: str, scope_info: dict) -> Optional[str]:
    """Classify an action's direction (send/receive) within a test scope."""
    scope = scope_info.get("_scope")
    if not scope:
        return None
    try:
        from ivy_lsp.analysis.test_scope import classify_action_direction

        return classify_action_direction(action_id, scope).value
    except Exception:
        logger.debug("Direction classification failed for %s", action_id, exc_info=True)
        return None


def _serialize_requirement(req: RequirementNode, snap: GraphSnapshot) -> dict:
    """Convert a RequirementNode to a JSON-serializable dict."""
    state_vars_read = [sv.name for sv in snap.get_state_vars_read_by(req.id)]

    nct = None
    try:
        from ivy_lsp.analysis.test_scope import classify_requirement

        nct = classify_requirement(req).value
    except Exception:
        logger.debug("NCT classification failed for %s", req.id, exc_info=True)

    formula = req.formula_text
    if len(formula) > 200:
        formula = formula[:200] + "..."

    return {
        "id": req.id,
        "kind": req.kind,
        "mixin_kind": req.mixin_kind,
        "formulaText": formula,
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


def handle_action_requirements(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/actionRequirements request.

    Returns per-action requirement breakdown with before/after monitors,
    counts, RFC tags, and state variable information.
    """
    _not_ready = {
        "actions": [],
        "scopeInfo": {"testFile": None, "scoped": False},
        "modelReady": False,
    }
    graph = _get_requirement_graph(server)
    if graph is None:
        indexer = server.indexer
        logger.warning(
            "handle_action_requirements: graph is None, returning modelReady=False"
        )
        _not_ready["_debug"] = (
            f"graph=None, indexer={type(indexer).__name__ if indexer != 'MISSING' else 'MISSING'}"
        )
        return _not_ready

    try:
        t0 = time.monotonic()
        snap = graph.snapshot()
        t_snap = time.monotonic()
        logger.info(
            "handle_action_requirements: snapshot in %.1fms, %d actions, %d reqs",
            (t_snap - t0) * 1000,
            len(snap.actions),
            len(snap.requirements),
        )
        slog.info(
            "handle_action_requirements: snapshot has %d actions, %d requirements",
            len(snap.actions),
            len(snap.requirements),
            extra={
                "event": LogEvent(
                    LogCategory.PERFORMANCE,
                    "analysis",
                    {
                        "actions": len(snap.actions),
                        "requirements": len(snap.requirements),
                    },
                )
            },
        )
        scope_info = _resolve_scope(graph, params)
        action_filter = params.get("actionName")
        file_filter = params.get("filePath")

        actions_to_process = dict(snap.actions)
        # C2: Apply scope filtering from test_file
        actions_to_process = _filter_by_scope(actions_to_process, scope_info)
        # H1: Apply protocol filtering
        actions_to_process = _filter_by_protocol(
            actions_to_process, params.get("protocolFilter")
        )
        if action_filter:
            actions_to_process = {
                k: v
                for k, v in actions_to_process.items()
                if v.name == action_filter
                or v.qualified_name == action_filter
                or k == action_filter
            }
        if file_filter:
            # C8: Match by exact, basename, or suffix
            exact = {
                k: v for k, v in actions_to_process.items() if v.file == file_filter
            }
            if exact:
                actions_to_process = exact
            else:
                filter_base = os.path.basename(file_filter)
                actions_to_process = {
                    k: v
                    for k, v in actions_to_process.items()
                    if os.path.basename(v.file) == filter_base
                    or v.file.endswith("/" + file_filter)
                }

        # Pagination: limit/offset to cap response size.
        # Default to returning all actions when no limit is specified,
        # so clients that don't send pagination params get complete data.
        all_action_items = list(actions_to_process.items())
        total_actions = len(all_action_items)
        offset = params.get("offset", 0)
        limit = params.get("limit", total_actions)
        paginated_items = all_action_items[offset : offset + limit]

        result_actions = []
        for action_id, action_node in paginated_items:
            reqs = snap.get_requirements_for_action(action_id)
            before = [r for r in reqs if r.mixin_kind == "before"]
            after = [r for r in reqs if r.mixin_kind == "after"]
            around = [r for r in reqs if r.mixin_kind == "around"]
            implement = [r for r in reqs if r.mixin_kind == "implement"]
            direct = [r for r in reqs if r.mixin_kind == "direct"]

            all_tags: Set[str] = set()
            counts: Dict[str, int] = defaultdict(int)
            for r in reqs:
                counts[r.kind] += 1
                all_tags.update(r.bracket_tags)

            direction = _classify_direction(action_id, scope_info)

            seen_read_ids: Set[str] = set()
            state_vars_read: List[StateVarNode] = []
            for r in reqs:
                for sv in snap.get_state_vars_read_by(r.id):
                    if sv.id not in seen_read_ids:
                        seen_read_ids.add(sv.id)
                        state_vars_read.append(sv)
            state_vars_written = snap.get_state_vars_written_by_action(action_id)

            result_actions.append(
                {
                    "actionName": action_node.name,
                    "qualifiedName": action_node.qualified_name,
                    "file": action_node.file,
                    "line": action_node.line,
                    "direction": direction,
                    "monitors": {
                        "before": [_serialize_requirement(r, snap) for r in before],
                        "after": [_serialize_requirement(r, snap) for r in after],
                        "around": [_serialize_requirement(r, snap) for r in around],
                        "implement": [
                            _serialize_requirement(r, snap) for r in implement
                        ],
                        "direct": [_serialize_requirement(r, snap) for r in direct],
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

        result = {
            "actions": result_actions,
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
            "modelReady": True,
            "pagination": {
                "total": total_actions,
                "offset": offset,
                "limit": limit,
                "hasMore": offset + limit < total_actions,
            },
        }
        logger.info(
            "handle_action_requirements: total %.1fms (snapshot %.1fms)",
            (time.monotonic() - t0) * 1000,
            (t_snap - t0) * 1000,
        )
        return _cap_response(result, "actions")
    except Exception as exc:
        logger.exception("handle_action_requirements failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# Handler: ivy/modelSummaryTable
# ---------------------------------------------------------------------------


def handle_model_summary_table(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/modelSummaryTable request.

    Returns a flat summary table with one row per action and aggregated
    totals for requirements, state variables, and RFC coverage.
    """
    _not_ready = {
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
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        t0 = time.monotonic()
        snap = graph.snapshot()
        t_snap = time.monotonic()
        scope_info = _resolve_scope(graph, params)
        rows: List[dict] = []
        total_reqs = 0

        # C2/H1: Apply scope and protocol filtering
        actions_to_iter = dict(snap.actions)
        actions_to_iter = _filter_by_scope(actions_to_iter, scope_info)
        actions_to_iter = _filter_by_protocol(
            actions_to_iter, params.get("protocolFilter")
        )

        for action_id, action_node in actions_to_iter.items():
            reqs = snap.get_requirements_for_action(action_id)
            before_reqs = [r for r in reqs if r.mixin_kind == "before"]
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

            vars_read_ids: Set[str] = set()
            for r in reqs:
                for sv in snap.get_state_vars_read_by(r.id):
                    vars_read_ids.add(sv.id)
            vars_written = snap.get_state_vars_written_by_action(action_id)

            direction = _classify_direction(action_id, scope_info)

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

        coverage = snap.get_coverage_stats()
        result = {
            "rows": rows,
            "totals": {
                "actions": len(snap.actions),
                "requirements": total_reqs,
                "stateVars": len(snap.state_vars),
                "rfcTagsCovered": coverage.get("covered", 0),
                "rfcTagsTotal": coverage.get("total", 0),
            },
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
        }
        logger.info(
            "handle_model_summary_table: total %.1fms (snapshot %.1fms)",
            (time.monotonic() - t0) * 1000,
            (t_snap - t0) * 1000,
        )
        return _cap_response(result, "rows")
    except Exception as exc:
        logger.exception("handle_model_summary_table failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# Handler: ivy/coverageGaps
# ---------------------------------------------------------------------------


def handle_coverage_gaps(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/coverageGaps request.

    Identifies coverage gaps in the formal model:
    - Unguarded state variables: written but not read by any requirement
    - Orphan requirements: monitor_action references a non-existent action
    - Uncovered RFC requirements: RFC requirements with no matching bracket tag
    """
    _not_ready = {
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
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        t0 = time.monotonic()
        snap = graph.snapshot()
        t_snap = time.monotonic()
        scope_info = _resolve_scope(graph, params)

        # -- Unguarded state variables --------------------------------------
        guarded_vars: Set[str] = set()
        for req_id in snap.requirements:
            for etype, target_id in snap.outgoing.get(req_id, []):
                if etype == EdgeType.READS:
                    guarded_vars.add(target_id)
        for prop_id in snap.properties:
            for etype, target_id in snap.outgoing.get(prop_id, []):
                if etype == EdgeType.READS:
                    guarded_vars.add(target_id)

        written_vars: Set[str] = set()
        for _, etype, target_id in snap.edges:
            if etype == EdgeType.WRITES:
                written_vars.add(target_id)

        unguarded: List[dict] = []
        for var_id, var_node in snap.state_vars.items():
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

        # -- Uncovered RFC requirements -------------------------------------
        uncovered_rfc = snap.get_uncovered_requirements()
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

        # -- Orphan requirements --------------------------------------------
        orphans: List[dict] = []
        for req in snap.requirements.values():
            if req.monitor_action and req.monitor_action not in snap.actions:
                orphans.append(
                    {
                        "id": req.id,
                        "kind": req.kind,
                        "formulaText": req.formula_text,
                        "file": req.file,
                        "line": req.line,
                        "reason": (f"Action '{req.monitor_action}' not found in graph"),
                    }
                )

        result = {
            "unguardedStateVars": unguarded,
            "uncoveredRfcRequirements": uncovered_rfc_list,
            "orphanRequirements": orphans,
            "summary": {
                "totalActions": len(snap.actions),
                "totalRequirements": len(snap.requirements),
                "totalStateVars": len(snap.state_vars),
                "unguardedCount": len(unguarded),
                "totalRfcReqs": len(snap.rfc_requirements),
                "uncoveredRfcCount": len(uncovered_rfc_list),
                "orphanReqCount": len(orphans),
            },
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
        }
        logger.info(
            "handle_coverage_gaps: total %.1fms (snapshot %.1fms)",
            (time.monotonic() - t0) * 1000,
            (t_snap - t0) * 1000,
        )
        result = _cap_response(result, "unguardedStateVars")
        result = _cap_response(result, "uncoveredRfcRequirements")
        result = _cap_response(result, "orphanRequirements")

        # --- Pattern coverage gaps (lightweight) ---
        pattern_gaps: dict = {"serdesGaps": [], "monitorGaps": [], "shimGaps": []}
        try:
            from ivy_lsp.analysis.pattern_library import (
                PatternCrossReferencer,
                PatternKind,
                analyze_protocol,
            )

            # Only run if we can find a protocol directory
            if scope_info.get("_scope"):
                scope_path = scope_info["_scope"]
                if os.path.isdir(scope_path):
                    prot_result = analyze_protocol(scope_path)
                    xref = PatternCrossReferencer(prot_result)
                    for issue in xref.validate_serdes_coverage():
                        pattern_gaps["serdesGaps"].append(
                            {
                                "message": issue.message,
                                "file": issue.file,
                                "related": issue.related,
                            }
                        )
                    for issue in xref.validate_monitor_coverage():
                        pattern_gaps["monitorGaps"].append(
                            {
                                "message": issue.message,
                                "related": issue.related,
                            }
                        )
                    for issue in xref.validate_shim_completeness():
                        pattern_gaps["shimGaps"].append(
                            {
                                "message": issue.message,
                            }
                        )
        except ImportError:
            pass  # pattern_library not available

        result["patternCoverage"] = pattern_gaps

        return result
    except Exception as exc:
        logger.exception("handle_coverage_gaps failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# Handler: ivy/actionDependencyGraph
# ---------------------------------------------------------------------------


def handle_action_dependency_graph(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/actionDependencyGraph request.

    Builds a graph where actions are nodes and edges represent shared
    state variables (action A writes a var that action B reads).
    """
    _not_ready = {
        "nodes": [],
        "edges": [],
        "scopeInfo": {"testFile": None, "scoped": False},
    }
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        t0 = time.monotonic()
        snap = graph.snapshot()
        t_snap = time.monotonic()
        scope_info = _resolve_scope(graph, params)
        include_state_vars = params.get("includeStateVars", False)

        # C2/H1: Apply scope and protocol filtering
        actions_filtered = dict(snap.actions)
        actions_filtered = _filter_by_scope(actions_filtered, scope_info)
        actions_filtered = _filter_by_protocol(
            actions_filtered, params.get("protocolFilter")
        )

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        for action_id, action_node in actions_filtered.items():
            reqs = snap.get_requirements_for_action(action_id)
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

        writers: Dict[str, Set[str]] = defaultdict(set)
        readers: Dict[str, Set[str]] = defaultdict(set)

        for action_id in actions_filtered:
            reqs = snap.get_requirements_for_action(action_id)
            for req in reqs:
                for etype, target_id in snap.outgoing.get(req.id, []):
                    if etype == EdgeType.WRITES:
                        writers[target_id].add(action_id)
                    elif etype == EdgeType.READS:
                        readers[target_id].add(action_id)

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
                            var_node = snap.state_vars.get(var_id)
                            label = var_node.name if var_node else var_id
                            edges.append(
                                {
                                    "source": w,
                                    "target": r,
                                    "label": label,
                                    "type": "shared_state",
                                }
                            )

        if include_state_vars:
            for var_id, var_node in snap.state_vars.items():
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

        # Cap edges to prevent O(n^2) blowup in large models
        try:
            max_edges = int(params.get("maxEdges", 500))
        except (TypeError, ValueError):
            max_edges = 500
        truncated = len(edges) > max_edges
        if truncated:
            edges = edges[:max_edges]

        result = {
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
        }
        logger.info(
            "handle_action_dependency_graph: total %.1fms (snapshot %.1fms)",
            (time.monotonic() - t0) * 1000,
            (t_snap - t0) * 1000,
        )
        return _cap_response(result, "nodes")
    except Exception as exc:
        logger.exception("handle_action_dependency_graph failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# Handler: ivy/stateMachineView
# ---------------------------------------------------------------------------


def handle_state_machine_view(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/stateMachineView request.

    Models the Ivy specification as a state machine where:
    - State variables are state nodes
    - Actions are transitions between state nodes (via READS/WRITES edges)
    - Guards are require/assume clauses on the action's monitors
    - Invariants are properties that constrain active state variables
    """
    _not_ready = {
        "nodes": [],
        "transitions": [],
        "scopeInfo": {"testFile": None, "scoped": False},
    }
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        t0 = time.monotonic()
        snap = graph.snapshot()
        t_snap = time.monotonic()
        scope_info = _resolve_scope(graph, params)
        state_var_filter = params.get("stateVarFilter")

        # C2/H1: Apply scope and protocol filtering
        actions_filtered = dict(snap.actions)
        actions_filtered = _filter_by_scope(actions_filtered, scope_info)
        actions_filtered = _filter_by_protocol(
            actions_filtered, params.get("protocolFilter")
        )

        nodes: List[Dict[str, Any]] = []
        transitions: List[Dict[str, Any]] = []

        active_vars: Set[str] = set()
        for action_id in actions_filtered:
            reqs = snap.get_requirements_for_action(action_id)
            for req in reqs:
                for etype, target_id in snap.outgoing.get(req.id, []):
                    if etype in (EdgeType.READS, EdgeType.WRITES):
                        active_vars.add(target_id)

        for var_id, var_node in snap.state_vars.items():
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

        for prop_id, prop_node in snap.properties.items():
            prop_vars = {
                target
                for etype, target in snap.outgoing.get(prop_id, [])
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

        for action_id, action_node in snap.actions.items():
            reqs = snap.get_requirements_for_action(action_id)

            read_vars: Set[str] = set()
            write_vars: Set[str] = set()
            guards: List[str] = []

            for req in reqs:
                for etype, target_id in snap.outgoing.get(req.id, []):
                    if etype == EdgeType.READS and target_id in active_var_ids:
                        read_vars.add(target_id)
                    elif etype == EdgeType.WRITES and target_id in active_var_ids:
                        write_vars.add(target_id)
                if req.kind in ("require", "assume"):
                    guards.append(req.formula_text)

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

        # Cap transitions to prevent O(n^2) blowup
        try:
            max_transitions = int(
                params.get("maxItems", params.get("maxTransitions", 500))
            )
        except (TypeError, ValueError):
            max_transitions = 500
        truncated = len(transitions) > max_transitions
        if truncated:
            transitions = transitions[:max_transitions]

        result = {
            "nodes": nodes,
            "transitions": transitions,
            "truncated": truncated,
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
        }
        logger.info(
            "handle_state_machine_view: total %.1fms (snapshot %.1fms)",
            (time.monotonic() - t0) * 1000,
            (t_snap - t0) * 1000,
        )
        return _cap_response(result, "nodes")
    except Exception as exc:
        logger.exception("handle_state_machine_view failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# Handler: ivy/layeredOverview
# ---------------------------------------------------------------------------


def handle_layered_overview(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/layeredOverview request.

    Groups symbols, actions, state vars, and requirements by file or module.
    """
    _not_ready = {"layers": [], "scopeInfo": {"testFile": None, "scoped": False}}
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        snap = graph.snapshot()
        scope_info = _resolve_scope(graph, params)
        group_by = params.get("groupBy", "file")

        # C2/H1: Apply scope and protocol filtering
        actions_filtered = dict(snap.actions)
        actions_filtered = _filter_by_scope(actions_filtered, scope_info)
        actions_filtered = _filter_by_protocol(
            actions_filtered, params.get("protocolFilter")
        )

        by_group: Dict[str, Dict[str, Any]] = {}

        for action_id, action_node in actions_filtered.items():
            key = (
                action_node.file
                if group_by == "file"
                else _extract_module(action_node.qualified_name)
            )
            if key not in by_group:
                by_group[key] = {
                    "file": action_node.file if group_by == "file" else None,
                    "module": key if group_by == "module" else None,
                    "actions": [],
                    "stateVars": [],
                    "requirements": 0,
                }
            reqs = snap.get_requirements_for_action(action_id)
            by_group[key]["actions"].append(action_node.name)
            by_group[key]["requirements"] += len(reqs)

        for _, var_node in snap.state_vars.items():
            key = (
                var_node.file
                if group_by == "file"
                else _extract_module(var_node.qualified_name)
            )
            if key not in by_group:
                by_group[key] = {
                    "file": var_node.file if group_by == "file" else None,
                    "module": key if group_by == "module" else None,
                    "actions": [],
                    "stateVars": [],
                    "requirements": 0,
                }
            by_group[key]["stateVars"].append(var_node.name)

        layers = list(by_group.values())

        result = {
            "layers": layers,
            "scopeInfo": {
                "testFile": scope_info.get("testFile"),
                "scoped": scope_info.get("scoped", False),
            },
        }
        return _cap_response(result, "layers")
    except Exception as exc:
        logger.exception("handle_layered_overview failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


def _extract_module(qualified_name: str) -> str:
    """Extract module prefix from a qualified name like 'quic.send_pkt' -> 'quic'."""
    parts = qualified_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else qualified_name


# ---------------------------------------------------------------------------
# Handler: ivy/smartSuggestions
# ---------------------------------------------------------------------------


def handle_smart_suggestions(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/smartSuggestions request.

    Returns context-aware suggestions based on cursor position and
    the semantic model's requirement graph.
    """
    _not_ready = {"suggestions": [], "context": None}
    graph = _get_requirement_graph(server)
    if graph is None:
        return _not_ready

    try:
        snap = graph.snapshot()
        file_path = params.get("filePath", "")
        line = params.get("line", 0)
        action_name = params.get("actionName")
        _context_hint = params.get("context", "")  # reserved for future use

        suggestions: List[Dict[str, Any]] = []

        if action_name:
            # --- Action-specific suggestions ---
            reqs = snap.get_requirements_for_action(action_name)
            seen_vars: Set[str] = set()
            for req in reqs:
                for etype, target_id in snap.outgoing.get(req.id, []):
                    if etype == EdgeType.READS:
                        seen_vars.add(target_id)

            all_vars_for_related: Set[str] = set()
            for var_id in seen_vars:
                sharing = snap.get_requirements_sharing_state_var(var_id)
                for s in sharing:
                    for etype, target_id in snap.outgoing.get(s.id, []):
                        if etype == EdgeType.READS:
                            all_vars_for_related.add(target_id)

            missing_vars = all_vars_for_related - seen_vars
            for var_id in missing_vars:
                var_node = snap.state_vars.get(var_id)
                if var_node:
                    suggestions.append(
                        {
                            "type": "state_var",
                            "name": var_node.name,
                            "qualifiedName": var_node.qualified_name,
                            "reason": (
                                f"Used by related requirements but not yet "
                                f"referenced in {action_name}"
                            ),
                            "priority": "medium",
                        }
                    )

            written_vars = snap.get_state_vars_written_by_action(action_name)
            for sv in written_vars:
                guarded = any(
                    etype == EdgeType.READS and target == sv.id
                    for req in reqs
                    for etype, target in snap.outgoing.get(req.id, [])
                )
                if not guarded:
                    suggestions.append(
                        {
                            "type": "missing_guard",
                            "name": sv.name,
                            "reason": (
                                f"State var '{sv.name}' is written by "
                                f"{action_name} but not guarded"
                            ),
                            "priority": "high",
                            "template": f"require {sv.name}(...) ",
                        }
                    )
        else:
            # --- Workspace/file-level suggestions (no specific action) ---
            # C2/H1: Apply scope and protocol filtering
            scoped_actions = dict(snap.actions)
            scoped_actions = _filter_by_scope(
                scoped_actions, _resolve_scope(graph, params)
            )
            scoped_actions = _filter_by_protocol(
                scoped_actions, params.get("protocolFilter")
            )
            # Uncovered actions: actions with no CONSTRAINS edges
            for action_id, action_node in scoped_actions.items():
                incoming = snap.incoming.get(action_id, [])
                has_reqs = any(etype == EdgeType.CONSTRAINS for etype, _ in incoming)
                if not has_reqs:
                    suggestions.append(
                        {
                            "type": "uncovered_action",
                            "name": action_node.name,
                            "file": action_node.file,
                            "reason": (
                                f"Action '{action_node.name}' has no monitor "
                                f"requirements — consider adding before/after clauses"
                            ),
                            "priority": "high",
                        }
                    )

            # State vars written but never read by any requirement
            all_read_vars: Set[str] = set()
            for req_id in snap.requirements:
                for etype, target_id in snap.outgoing.get(req_id, []):
                    if etype == EdgeType.READS:
                        all_read_vars.add(target_id)
            for sv_id, sv_node in snap.state_vars.items():
                if sv_id not in all_read_vars:
                    suggestions.append(
                        {
                            "type": "unguarded_state",
                            "name": sv_node.name,
                            "reason": (
                                f"State var '{sv_node.name}' is never read "
                                f"by any requirement — may lack invariant checks"
                            ),
                            "priority": "medium",
                        }
                    )

            # C3: File-scoped filtering with fuzzy path matching
            if file_path:
                file_base = os.path.basename(file_path)

                def _file_matches(f: str) -> bool:
                    return (
                        f == file_path
                        or os.path.basename(f) == file_base
                        or f.endswith("/" + file_path)
                    )

                file_actions = {
                    aid for aid, a in snap.actions.items() if _file_matches(a.file)
                }
                file_vars = {
                    vid for vid, v in snap.state_vars.items() if _file_matches(v.file)
                }
                file_action_names = {
                    a.name for a in snap.actions.values() if a.id in file_actions
                }
                file_var_names = {
                    v.name for v in snap.state_vars.values() if v.id in file_vars
                }
                if file_actions or file_vars:
                    suggestions = [
                        s
                        for s in suggestions
                        if _file_matches(s.get("file", ""))
                        or s.get("name") in file_action_names
                        or s.get("name") in file_var_names
                    ]
                else:
                    # File has no actions/state vars — return only
                    # file-matching suggestions, not the entire workspace
                    suggestions = [
                        s for s in suggestions if _file_matches(s.get("file", ""))
                    ]

            # C3: Sort by line proximity when both file_path and line given
            if file_path and line:

                def _proximity(s: dict) -> int:
                    s_line = s.get("line", 0)
                    return abs(s_line - line) if s_line else 9999

                suggestions.sort(key=_proximity)

        # --- Pattern-based suggestions ---
        try:
            if file_path:
                basename = os.path.basename(file_path)
                if "_frame" in basename or "_message" in basename:
                    suggestions.append(
                        {
                            "type": "pattern_hint",
                            "message": (
                                "This looks like a variant/message definition file. "
                                "Consider using /nct-add-pattern to scaffold "
                                "monitors and serdes."
                            ),
                            "priority": "low",
                        }
                    )
                elif "_ser" in basename or "_deser" in basename:
                    suggestions.append(
                        {
                            "type": "pattern_hint",
                            "message": (
                                "This is a serialization file. Ensure enum states "
                                "match variant tags 1:1 (use ivy_pattern_analysis "
                                "to validate)."
                            ),
                            "priority": "low",
                        }
                    )
                elif "_behavior" in basename:
                    suggestions.append(
                        {
                            "type": "pattern_hint",
                            "message": (
                                "This is a behavior specification. Verify all "
                                "exported actions have before/after monitors "
                                "(use ivy_pattern_analysis mode=validate)."
                            ),
                            "priority": "low",
                        }
                    )
                elif "_shim" in basename:
                    suggestions.append(
                        {
                            "type": "pattern_hint",
                            "message": (
                                "This is a shim file. Ensure all entity roles "
                                "have dispatch branches (use ivy_pattern_analysis "
                                "mode=validate)."
                            ),
                            "priority": "low",
                        }
                    )
        except Exception:
            pass  # Don't let pattern hints break suggestions

        return {
            "suggestions": suggestions,
            "context": {
                "file": file_path,
                "line": line,
                "action": action_name,
            },
        }
    except Exception as exc:
        logger.exception("handle_smart_suggestions failed")
        _not_ready["error"] = f"{type(exc).__name__}: {exc}"
        return _not_ready


# ---------------------------------------------------------------------------
# LSP wiring
# ---------------------------------------------------------------------------


def register(server: Any) -> None:
    """Register visualization request handlers on the server."""

    @server.feature("ivy/actionRequirements")
    async def on_action_requirements(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_action_requirements,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/modelSummaryTable")
    async def on_model_summary_table(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_model_summary_table,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/coverageGaps")
    async def on_coverage_gaps(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_coverage_gaps,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/actionDependencyGraph")
    async def on_action_dependency_graph(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_action_dependency_graph,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/stateMachineView")
    async def on_state_machine_view(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_state_machine_view,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/layeredOverview")
    async def on_layered_overview(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_layered_overview,
            server,
            params if isinstance(params, dict) else {},
        )

    @server.feature("ivy/smartSuggestions")
    async def on_smart_suggestions(params: Any = None) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            handle_smart_suggestions,
            server,
            params if isinstance(params, dict) else {},
        )
