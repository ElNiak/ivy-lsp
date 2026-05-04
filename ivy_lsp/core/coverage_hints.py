"""Coverage hint diagnostics for Ivy files.

Generates Hint-severity diagnostics for missing coverage:
- Actions with no before/after monitors
- State vars written but never guarded by a requirement
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic

logger = logging.getLogger(__name__)


def _node_span(
    node: Any,
    name: str = "",
) -> Tuple[int, Optional[int], Optional[int]]:
    """Compute (character, end_line, end_character) for a graph node.

    Reads ``start_col`` (or ``col`` for RequirementNode) and ``end_col``
    from the node. Returns ``(0, None, None)`` when no column info is
    available, letting the diagnostic fall through to
    ``_DEFAULT_END_COLUMN`` at ``to_lsp()`` conversion. The ``name``
    fallback spans the named token when only ``start_col`` is populated.
    """
    start = getattr(node, "start_col", None)
    if start is None:
        start = getattr(node, "col", 0)
    end = getattr(node, "end_col", 0)
    if end > start:
        return start, node.line, end
    if start > 0 and name:
        return start, node.line, start + len(name)
    return 0, None, None


def compute_coverage_hints(
    graph: Any,
    filepath: str,
) -> List[IvyDiagnostic]:
    """Compute coverage hint diagnostics for a file.

    Args:
        graph: A RequirementGraph instance (or ``None``).
        filepath: Absolute path of the file to produce hints for.

    Returns:
        List of IvyDiagnostic instances with registry-validated codes.
    """
    if graph is None:
        return []

    from ivy_lsp.core.analysis.requirement_graph import EdgeType

    hints: List[IvyDiagnostic] = []

    # -----------------------------------------------------------------
    # 1. Actions with no monitors (no before/after requirements)
    # -----------------------------------------------------------------
    for action_id, action_node in graph.actions.items():
        if action_node.file != filepath:
            continue
        reqs = graph.get_requirements_for_action(action_id)
        if not reqs:
            char, end_line, end_char = _node_span(action_node, action_node.name)
            hints.append(
                IvyDiagnostic(
                    code="ivy.action.noMonitor",
                    message=(
                        f"Action '{action_node.name}' has no monitor "
                        f"requirements (before/after)"
                    ),
                    line=action_node.line,
                    character=char,
                    end_line=end_line,
                    end_character=end_char,
                    severity=lsp.DiagnosticSeverity.Hint,
                    source="ivy-semantic",
                    suggested_fix=(
                        f"after {action_node.name} {{\n" f"    ensure ...\n" f"}}"
                    ),
                    tags=[lsp.DiagnosticTag.Unnecessary],
                )
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

    # -----------------------------------------------------------------
    # 2a. Action-centric unguarded writes (names specific vars per action)
    # Runs FIRST so it can record which vars it covers; the var-centric
    # path below then skips those to avoid double-warning the same
    # (action, var) pair.
    # -----------------------------------------------------------------
    file_actions = sorted(
        (a for a in graph.actions.values() if a.file == filepath),
        key=lambda a: a.line,
    )

    file_writes: list[tuple[int, str]] = []
    prefix_match = filepath + ":"
    for src, etype, dst in graph.edges:
        if etype != EdgeType.WRITES or not src.startswith(prefix_match):
            continue
        marker = ":write:"
        marker_idx = src.find(marker)
        if marker_idx < 0:
            continue
        prefix = src[:marker_idx]
        last_colon = prefix.rfind(":")
        if last_colon <= 0:
            continue
        try:
            write_line = int(prefix[last_colon + 1 :])
        except ValueError:
            continue
        file_writes.append((write_line, dst))
    file_writes.sort(key=lambda w: w[0])

    vars_covered_by_action_emit: set[str] = set()

    if file_actions:

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
                    char, end_line, end_char = _node_span(action_node, action_node.name)
                    hints.append(
                        IvyDiagnostic(
                            code="ivy.action.unguardedWrite",
                            message=(
                                f"Action '{action_node.name}' writes {var_list} "
                                f"without a 'require' precondition"
                            ),
                            line=action_node.line,
                            character=char,
                            end_line=end_line,
                            end_character=end_char,
                            severity=lsp.DiagnosticSeverity.Hint,
                            source="ivy-semantic",
                            suggested_fix=f"require {var_names[0]}(...) ",
                            tags=[lsp.DiagnosticTag.Unnecessary],
                        )
                    )
                    vars_covered_by_action_emit.update(
                        v for v in unguarded_vars if v in graph.state_vars
                    )

    # -----------------------------------------------------------------
    # 2b. State vars written outside any in-file action's range — covered
    # by neither the action-centric emit above nor a `require`. Catches
    # writes from cross-file actions or module-level statements that the
    # action-centric path cannot reach. Skip vars already covered by 2a
    # via vars_covered_by_action_emit.
    # -----------------------------------------------------------------
    for var_id, var_node in graph.state_vars.items():
        if var_node.file != filepath:
            continue
        is_written = var_id in written_vars
        if is_written and var_id not in guarded_vars:
            if var_id in vars_covered_by_action_emit:
                continue
            char, end_line, end_char = _node_span(var_node, var_node.name)
            hints.append(
                IvyDiagnostic(
                    code="ivy.action.unguardedWrite",
                    message=(
                        f"State var '{var_node.name}' is written but "
                        f"not guarded by any requirement"
                    ),
                    line=var_node.line,
                    character=char,
                    end_line=end_line,
                    end_character=end_char,
                    severity=lsp.DiagnosticSeverity.Hint,
                    source="ivy-semantic",
                    suggested_fix=f"require {var_node.name}(...) ",
                    tags=[lsp.DiagnosticTag.Unnecessary],
                )
            )

    # -----------------------------------------------------------------
    # 3. Per-requirement checks: dead guards + orphaned monitor hooks
    # -----------------------------------------------------------------
    for req in graph.requirements.values():
        if req.file != filepath:
            continue

        # Dead guard: require false as unreachability sentinel
        if req.formula_text.strip() == "false":
            char, end_line, end_char = _node_span(req)
            hints.append(
                IvyDiagnostic(
                    code="ivy.require.deadGuard",
                    message=(
                        "Dead guard: 'require false' marks this action as "
                        "unreachable. Called only through variant specializations."
                    ),
                    line=req.line,
                    character=char,
                    end_line=end_line,
                    end_character=end_char,
                    severity=lsp.DiagnosticSeverity.Information,
                    source="ivy-lsp-coverage",
                    tags=[lsp.DiagnosticTag.Unnecessary],
                )
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
                char, end_line, end_char = _node_span(req)
                hints.append(
                    IvyDiagnostic(
                        code="ivy.monitor.orphanedHook",
                        message=(
                            f"Monitor targets action '{req.monitor_action}' "
                            "which has no definition in the include closure."
                        ),
                        line=req.line,
                        character=char,
                        end_line=end_line,
                        end_character=end_char,
                        severity=lsp.DiagnosticSeverity.Warning,
                        source="ivy-lsp-coverage",
                        tags=[lsp.DiagnosticTag.Unnecessary],
                    )
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
            char, end_line, end_char = _node_span(var_node, var_node.name)
            hints.append(
                IvyDiagnostic(
                    code="ivy.state.unusedStateVar",
                    message=(
                        f"State variable '{var_node.name}' has no reads or "
                        "writes in the requirement graph."
                    ),
                    line=var_node.line,
                    character=char,
                    end_line=end_line,
                    end_character=end_char,
                    severity=lsp.DiagnosticSeverity.Hint,
                    source="ivy-lsp-coverage",
                    tags=[lsp.DiagnosticTag.Unnecessary],
                )
            )

    return hints
