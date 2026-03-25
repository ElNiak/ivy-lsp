"""Coverage gap visualization handler."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Set

from ivy_lsp.core.analysis.requirement_graph import EdgeType
from ivy_lsp.core.protocols import IvyServerProtocol
from ivy_lsp.lsp.visualization import (
    _cap_response,
    _get_requirement_graph,
    _resolve_scope,
)

logger = logging.getLogger(__name__)


def handle_coverage_gaps(server: IvyServerProtocol, params: dict) -> dict:
    """Handle ivy/coverageGaps request.

    Identifies coverage gaps in the formal model:
    - Unguarded state variables: written but not read by any requirement
    - Orphan requirements: monitor_action references a non-existent action
    - Uncovered RFC requirements: RFC requirements with no matching bracket tag
    """
    _not_ready: Dict[str, Any] = {
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

        result: Dict[str, Any] = {
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
        # Apply protocol filter if specified
        protocol_filter = params.get("protocolFilter", "")
        if protocol_filter:
            result["uncoveredRfcRequirements"] = [
                r
                for r in result["uncoveredRfcRequirements"]
                if not r.get("file") or protocol_filter in r.get("file", "")
            ]
            result["unguardedStateVars"] = [
                v
                for v in result["unguardedStateVars"]
                if not v.get("file") or protocol_filter in v.get("file", "")
            ]
            # Update summary counts after filtering
            result["summary"]["uncoveredRfcCount"] = len(
                result["uncoveredRfcRequirements"]
            )
            result["summary"]["unguardedCount"] = len(result["unguardedStateVars"])
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
            from ivy_lsp.core.analysis.pattern_library import (
                PatternCrossReferencer,
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
