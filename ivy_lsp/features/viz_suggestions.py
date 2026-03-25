"""Smart suggestions visualization handler."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Set

from ivy_lsp.analysis.requirement_graph import EdgeType
from ivy_lsp.features.visualization import (
    _filter_by_protocol,
    _filter_by_scope,
    _get_requirement_graph,
    _resolve_scope,
)
from ivy_lsp.protocols import IvyServerProtocol

logger = logging.getLogger(__name__)


def handle_smart_suggestions(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/smartSuggestions request.

    Returns context-aware suggestions based on cursor position and
    the semantic model's requirement graph.
    """
    _not_ready: Dict[str, Any] = {"suggestions": [], "context": None}
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
                                'match variant tags 1:1 (use ivy_patterns(mode="validate") '
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
                                '(use ivy_patterns(mode="validate")).'
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
                                "have dispatch branches (use ivy_patterns"
                                '(mode="validate")).'
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
