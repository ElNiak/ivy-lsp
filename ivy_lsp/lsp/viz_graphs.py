"""Graph visualization handlers (dependency graph, state machine, layered overview)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Set

from ivy_lsp.core.analysis.requirement_graph import EdgeType
from ivy_lsp.core.protocols import IvyServerProtocol
from ivy_lsp.lsp.visualization import (
    _cap_response,
    _filter_by_protocol,
    _filter_by_scope,
    _get_requirement_graph,
    _resolve_scope,
)

logger = logging.getLogger(__name__)


def handle_action_dependency_graph(server: IvyServerProtocol, params: dict) -> dict:
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
            "reason": "requirement_graph_not_available",
        }

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
        return {
            "nodes": [],
            "edges": [],
            "scopeInfo": {"testFile": None, "scoped": False},
            "error": f"{type(exc).__name__}: {exc}",
        }


def handle_state_machine_view(server: IvyServerProtocol, params: dict) -> dict:
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
            "reason": "requirement_graph_not_available",
        }

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
        return {
            "nodes": [],
            "transitions": [],
            "scopeInfo": {"testFile": None, "scoped": False},
            "error": f"{type(exc).__name__}: {exc}",
        }


def _extract_module(qualified_name: str) -> str:
    """Extract module prefix from a qualified name like 'quic.send_pkt' -> 'quic'."""
    parts = qualified_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else qualified_name


def handle_layered_overview(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/layeredOverview request.

    Groups symbols, actions, state vars, and requirements by file or module.
    """
    graph = _get_requirement_graph(server)
    if graph is None:
        return {
            "layers": [],
            "scopeInfo": {"testFile": None, "scoped": False},
            "reason": "requirement_graph_not_available",
        }

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
        return {
            "layers": [],
            "scopeInfo": {"testFile": None, "scoped": False},
            "error": f"{type(exc).__name__}: {exc}",
        }
