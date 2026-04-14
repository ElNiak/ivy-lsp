"""Coverage hint diagnostics for Ivy files.

Generates Hint-severity diagnostics for missing coverage:
- Actions with no before/after monitors
- State vars written but never guarded by a requirement
"""

from __future__ import annotations

import logging
from collections import defaultdict
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
    # 2b. Action-centric unguarded writes (names specific vars per action)
    # -----------------------------------------------------------------
    actions_by_file: Dict[str, List[Any]] = defaultdict(list)
    for action_node in graph.actions.values():
        actions_by_file[action_node.file].append(action_node)
    for file_actions in actions_by_file.values():
        file_actions.sort(key=lambda a: a.line)

    writes_by_file: Dict[str, List[tuple]] = defaultdict(list)
    for src, etype, dst in graph.edges:
        if etype != EdgeType.WRITES:
            continue
        marker = ":write:"
        marker_idx = src.find(marker)
        if marker_idx < 0:
            continue
        prefix = src[:marker_idx]
        last_colon = prefix.rfind(":")
        if last_colon <= 0:
            continue
        write_file = prefix[:last_colon]
        try:
            write_line = int(prefix[last_colon + 1 :])
        except ValueError:
            continue
        writes_by_file[write_file].append((write_line, dst))

    if filepath in actions_by_file:
        file_actions = actions_by_file[filepath]
        file_writes = sorted(writes_by_file.get(filepath, []), key=lambda w: w[0])

        for i, action_node in enumerate(file_actions):
            range_start = action_node.line
            range_end = (
                file_actions[i + 1].line if i + 1 < len(file_actions) else float("inf")
            )

            seen: set[str] = set()
            unguarded_vars: list[str] = []
            for write_line, var_id in file_writes:
                if write_line < range_start:
                    continue
                if write_line >= range_end:
                    break
                if var_id not in guarded_vars and var_id not in seen:
                    seen.add(var_id)
                    unguarded_vars.append(var_id)

            if unguarded_vars:
                var_names = [
                    graph.state_vars[v].name
                    for v in unguarded_vars
                    if v in graph.state_vars
                ]
                if var_names:
                    var_list = ", ".join(f"'{v}'" for v in var_names)
                    hints.append(
                        {
                            "line": action_node.line,
                            "message": (
                                f"Action '{action_node.name}' writes {var_list} "
                                f"without a 'require' precondition"
                            ),
                            "severity": "hint",
                            "code": "ivy.action.unguardedWrite",
                            "template": f"require {var_names[0]}(...) ",
                        }
                    )

    # -----------------------------------------------------------------
    # 3. Per-requirement checks: dead guards + orphaned monitor hooks
    # -----------------------------------------------------------------
    for req in graph.requirements.values():
        if req.file != filepath:
            continue

        # Dead guard: require false as unreachability sentinel
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

        # Orphaned hook: monitor targets a backfill-only action
        if req.monitor_action:
            action = graph.actions.get(req.monitor_action)
            # Backfill-only actions inherit the monitor's file/line,
            # not a real declaration site.
            if (
                action is not None
                and action.file == req.file
                and action.line == req.line
            ):
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
        has_incoming = len(graph.incoming.get(var_id, [])) > 0
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
