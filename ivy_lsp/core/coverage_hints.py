"""Coverage hint diagnostics for Ivy files.

Generates Hint-severity diagnostics for missing coverage:
- Actions with no before/after monitors
- State vars written but never guarded by a requirement
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def compute_coverage_hints(
    graph: Any,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Compute coverage hint diagnostics for a file.

    Parameters
    ----------
    graph:
        A :class:`RequirementGraph` instance (or ``None``).
    filepath:
        Absolute path of the file to produce hints for.

    Returns:
    -------
    List of dicts, each with keys:
        ``line``, ``message``, ``severity``, ``code``, and optionally
        ``template`` (a skeleton snippet the user can insert).
    """
    if graph is None:
        return []

    from ivy_lsp.core.analysis.requirement_graph import EdgeType

    hints: List[Dict[str, Any]] = []

    # -----------------------------------------------------------------
    # 1. Actions with no monitors (no before/after requirements)
    # -----------------------------------------------------------------
    for action_id, action_node in graph.actions.items():
        if action_node.file != filepath:
            continue
        reqs = graph.get_requirements_for_action(action_id)
        if not reqs:
            hints.append(
                {
                    "line": action_node.line,
                    "message": (
                        f"Action '{action_node.name}' has no monitor "
                        f"requirements (before/after)"
                    ),
                    "severity": "hint",
                    "code": "ivy.no-monitor",
                    "template": (
                        f"after {action_node.name} {{\n" f"    ensure ...\n" f"}}"
                    ),
                }
            )

    # -----------------------------------------------------------------
    # 2. State vars written but never guarded by a requirement
    # -----------------------------------------------------------------

    # Collect vars that are READS-targets of any requirement or property.
    guarded_vars: set[str] = set()
    for req_id in graph.requirements:
        for etype, target_id in graph.get_outgoing_edges(req_id):
            if etype == EdgeType.READS:
                guarded_vars.add(target_id)
    for prop_id in graph.properties:
        for etype, target_id in graph.get_outgoing_edges(prop_id):
            if etype == EdgeType.READS:
                guarded_vars.add(target_id)

    # Collect vars that are WRITES-targets (from any source).
    written_vars: set[str] = set()
    for _, etype, target_id in graph.edges:
        if etype == EdgeType.WRITES:
            written_vars.add(target_id)

    for var_id, var_node in graph.state_vars.items():
        if var_node.file != filepath:
            continue
        is_written = var_id in written_vars
        if is_written and var_id not in guarded_vars:
            hints.append(
                {
                    "line": var_node.line,
                    "message": (
                        f"State var '{var_node.name}' is written but "
                        f"not guarded by any requirement"
                    ),
                    "severity": "hint",
                    "code": "ivy.unguarded-write",
                    "template": f"require {var_node.name}(...) ",
                }
            )

    # -----------------------------------------------------------------
    # 3. Dead guards: require false as unreachability sentinel
    # -----------------------------------------------------------------
    for req in graph.requirements.values():
        if req.file != filepath:
            continue
        if req.formula_text.strip() == "false":
            hints.append(
                {
                    "line": req.line,
                    "message": (
                        "Dead guard: 'require false' marks this action as "
                        "unreachable. Called only through variant specializations."
                    ),
                    "severity": "info",
                    "code": "ivy.require.deadGuard",
                }
            )

    # -----------------------------------------------------------------
    # 4. Orphaned monitor hooks: monitors targeting backfill-only actions
    # -----------------------------------------------------------------
    for req in graph.requirements.values():
        if req.file != filepath:
            continue
        if not req.monitor_action:
            continue
        action = graph.actions.get(req.monitor_action)
        if action is None:
            continue
        # Backfill-only actions inherit the monitor's file/line, not a real declaration site.
        if action.file == req.file and action.line == req.line:
            hints.append(
                {
                    "line": req.line,
                    "message": (
                        f"Monitor targets action '{req.monitor_action}' "
                        "which has no definition in the include closure."
                    ),
                    "severity": "warning",
                    "code": "ivy.monitor.orphanedHook",
                }
            )

    # -----------------------------------------------------------------
    # 5. Unused state variables: no reads or writes in the graph
    # -----------------------------------------------------------------
    for var_id, var_node in graph.state_vars.items():
        if var_node.file != filepath:
            continue
        has_outgoing = len(graph.get_outgoing_edges(var_id)) > 0
        has_incoming = len(graph._incoming.get(var_id, [])) > 0
        if not has_outgoing and not has_incoming:
            hints.append(
                {
                    "line": var_node.line,
                    "message": (
                        f"State variable '{var_node.name}' has no reads or "
                        "writes in the requirement graph."
                    ),
                    "severity": "hint",
                    "code": "ivy.state.unusedStateVar",
                }
            )

    return hints
