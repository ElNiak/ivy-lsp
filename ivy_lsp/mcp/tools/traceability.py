"""Traceability tools: ivy_coverage (unified coverage dispatcher).

Coverage sub-functions:
- _ivy_traceability_matrix  -- requirement-to-annotation mapping
- _ivy_requirement_coverage -- stats by level/layer
- _ivy_coverage_gaps        -- unguarded state vars, uncovered requirements
- _ivy_coverage_diff        -- regression diff against cached baseline

Extraction and manifest tools live in ``traceability_extraction.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import error_response, inject_scope_metadata, safe_tool

logger = logging.getLogger(__name__)


def register_traceability_tools(mcp: Any, ctx: Any) -> None:
    """Register traceability-related MCP tools."""
    # Coverage baseline cache: stores last coverage stats result per scope.
    # Key is the relative_path (or "__global__" when None).
    _coverage_baselines: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Private helpers (former standalone tool bodies)
    # ------------------------------------------------------------------

    def _model_unavailable_response() -> dict:
        """Error response with indexing status note."""
        status = ctx.get_model_status()
        if status.get("state") == "not_built":
            return {
                "success": False,
                "message": "Semantic model unavailable",
                "note": "LSP is still indexing. Results may be incomplete. Try again shortly.",
            }
        if status.get("state") == "building":
            return {
                "success": False,
                "message": "Semantic model is currently building",
                "note": "The model is being built (this can take 2-4 minutes on first use). Try again shortly.",
            }
        if status.get("state") == "failed":
            return {
                "success": False,
                "message": "Semantic model unavailable",
                "note": f"Model build failed: {status.get('error', 'unknown')}. "
                f"Retry in {status.get('retry_in_seconds', '?')}s.",
            }
        return error_response("Semantic model unavailable")

    async def _ivy_traceability_matrix(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> dict:
        """RFC requirement-to-annotation traceability matrix."""
        # Check model status first to avoid blocking
        status = ctx.get_model_status()
        if status.get("state") not in ("ready", "not_built"):
            return _model_unavailable_response()
        model = await ctx.get_model()
        if model is None:
            return _model_unavailable_response()

        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_with_diagnostics

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        # Fallback: if semantic model has no requirements, load from manifests
        if not requirements:
            from ivy_lsp.core.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
            )

            for manifest_path in find_manifests(ctx.root):
                manifest_reqs = load_requirement_manifest(manifest_path)
                requirements.extend(manifest_reqs.values())

        if test_file:
            # Endpoint-mirror scoping: filter to include closure of test file.
            try:
                abs_test = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
            graph = await ctx.get_req_graph()
            if graph is not None:
                scope = graph.get_test_scope(abs_test)
                if scope is not None:
                    scope_files = scope.include_closure
                    annotations = [a for a in annotations if a.file in scope_files]
                else:
                    annotations = [a for a in annotations if a.file == abs_test]
        elif relative_path:
            try:
                abs_path = ctx.validate_path(relative_path)
            except ValueError as exc:
                return error_response(str(exc))
            if os.path.isdir(abs_path):
                prefix = abs_path.rstrip(os.sep) + os.sep
                annotations = [a for a in annotations if a.file.startswith(prefix)]
            else:
                annotations = [a for a in annotations if a.file == abs_path]

        req_ids = {r.id for r in requirements}
        covered_tags: dict[str, list[dict]] = {}
        warnings: list[str] = []
        for ann in annotations:
            for tag in ann.tags:
                resolution = normalize_tag_with_diagnostics(tag, req_ids)
                warnings.extend(resolution.warnings)
                for rfc_id in resolution.matched_ids:
                    if rfc_id not in covered_tags:
                        covered_tags[rfc_id] = []
                    covered_tags[rfc_id].append(
                        {
                            "file": ann.file,
                            "line": ann.line,
                        }
                    )

        # Warn if annotations exist but no requirements loaded
        if annotations and not requirements:
            warnings.append(
                "RFC annotations found but no requirement manifests loaded. "
                "Coverage cannot be computed."
            )

        matrix = []
        for req in requirements:
            matrix.append(
                {
                    "id": req.id,
                    "rfc": req.rfc,
                    "section": req.section,
                    "level": req.level,
                    "text": req.text[:120],
                    "covered": req.id in covered_tags,
                    "assertions": covered_tags.get(req.id, []),
                }
            )

        result: dict[str, Any] = {
            "total_requirements": len(requirements),
            "covered": sum(1 for m in matrix if m["covered"]),
            "uncovered": sum(1 for m in matrix if not m["covered"]),
            "matrix": matrix,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def _ivy_requirement_coverage(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> dict:
        """RFC requirement coverage statistics by level and layer."""
        # Check model status first to avoid blocking
        status = ctx.get_model_status()
        if status.get("state") not in ("ready", "not_built"):
            return _model_unavailable_response()
        model = await ctx.get_model()
        if model is None:
            return _model_unavailable_response()

        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_with_diagnostics

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        # Fallback: if semantic model has no requirements, load from manifests
        if not requirements:
            from ivy_lsp.core.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
            )

            for manifest_path in find_manifests(ctx.root):
                manifest_reqs = load_requirement_manifest(manifest_path)
                requirements.extend(manifest_reqs.values())

        if test_file:
            # Endpoint-mirror scoping: filter annotations to include closure
            # of test file.  Requirements are manifest-level (RfcRequirement
            # has no .file attribute) so they must NOT be filtered by path.
            try:
                abs_test = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
            graph = await ctx.get_req_graph()
            if graph is not None:
                scope = graph.get_test_scope(abs_test)
                if scope is not None:
                    scope_files = scope.include_closure
                    annotations = [a for a in annotations if a.file in scope_files]
                else:
                    annotations = [a for a in annotations if a.file == abs_test]
        elif relative_path:
            try:
                abs_path = ctx.validate_path(relative_path)
            except ValueError as exc:
                return error_response(str(exc))
            if os.path.isdir(abs_path):
                prefix = abs_path.rstrip(os.sep) + os.sep
                annotations = [a for a in annotations if a.file.startswith(prefix)]
            else:
                annotations = [a for a in annotations if a.file == abs_path]

        # FX2 fix: when relative_path scoping yields no annotations,
        # return zero coverage instead of counting global requirements.
        if relative_path and not annotations:
            return {
                "total": 0,
                "covered": 0,
                "uncovered": 0,
                "coverage_percent": 0,
                "by_level": {},
                "by_layer": {},
                "uncovered_ids": [],
                "_uncovered_ids_full": [],
                "_covered_ids": [],
                "counting_method": "manifest_annotations",
                "scope": relative_path,
            }

        req_ids = {r.id for r in requirements}
        covered_tags: set[str] = set()
        coverage_warnings: list[str] = []
        for ann in annotations:
            for tag in ann.tags:
                resolution = normalize_tag_with_diagnostics(tag, req_ids)
                covered_tags.update(resolution.matched_ids)
                coverage_warnings.extend(resolution.warnings)

        # Warn if annotations exist but no requirements loaded
        if annotations and not requirements:
            coverage_warnings.append(
                "RFC annotations found but no requirement manifests loaded. "
                "Coverage cannot be computed."
            )

        by_level: dict[str, dict] = {}
        by_layer: dict[str, dict] = {}
        for req in requirements:
            level = req.level or "UNKNOWN"
            layer = getattr(req, "layer", None) or "unspecified"

            if level not in by_level:
                by_level[level] = {"total": 0, "covered": 0}
            by_level[level]["total"] += 1
            if req.id in covered_tags:
                by_level[level]["covered"] += 1

            if layer not in by_layer:
                by_layer[layer] = {"total": 0, "covered": 0}
            by_layer[layer]["total"] += 1
            if req.id in covered_tags:
                by_layer[layer]["covered"] += 1

        total = len(requirements)
        covered = sum(1 for r in requirements if r.id in covered_tags)

        # P1: Include top uncovered requirement IDs for AI consumption
        uncovered_ids = [r.id for r in requirements if r.id not in covered_tags]

        # P2: Add coverage_percent and uncovered count per by_level/by_layer
        for group in (by_level, by_layer):
            for entry in group.values():
                entry["uncovered"] = entry["total"] - entry["covered"]
                entry["coverage_percent"] = (
                    round(100 * entry["covered"] / entry["total"], 1)
                    if entry["total"]
                    else 0
                )

        covered_ids = sorted(r.id for r in requirements if r.id in covered_tags)

        result: dict[str, Any] = {
            "total": total,
            "covered": covered,
            "uncovered": total - covered,
            "coverage_percent": round(100 * covered / total, 1) if total else 0,
            "by_level": by_level,
            "by_layer": by_layer,
            "uncovered_ids": uncovered_ids[:50],
            "_uncovered_ids_full": uncovered_ids,
            "_covered_ids": covered_ids,
            "counting_method": "manifest_annotations",
        }
        if coverage_warnings:
            result["warnings"] = coverage_warnings

        # Add manifests summary
        from ivy_lsp.core.semantic.rfc_annotations import find_manifests

        workspace_root = ctx.root
        manifests = find_manifests(workspace_root)
        if manifests:
            result["manifests"] = [
                os.path.relpath(m, workspace_root) if workspace_root else m
                for m in manifests
            ]

        # Save as baseline for diff mode
        scope_key = relative_path or "__global__"
        _coverage_baselines[scope_key] = result

        return result

    async def _ivy_coverage_gaps(
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> dict:
        """Identify coverage gaps: unguarded state vars, uncovered RFC requirements."""
        from ivy_lsp.lsp.viz_coverage import handle_coverage_gaps

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        result = handle_coverage_gaps(server_proxy, params)

        # C4 fix: override RFC uncovered requirements using the same
        # logic as _ivy_requirement_coverage() so stats and gaps agree.
        stats = await _ivy_requirement_coverage(relative_path=None, test_file=test_file)
        try:
            uncovered_ids = set(stats.get("_uncovered_ids_full", []))
            # Check model status first to avoid blocking
            _status = ctx.get_model_status()
            if _status.get("state") not in ("ready", "not_built"):
                model = None
            else:
                model = await ctx.get_model()

            # Build req_map from the semantic model first
            req_map: dict[str, Any] = {}
            if model is not None:
                from ivy_lsp.core.semantic.nodes import RfcRequirement

                requirements = model.get_nodes_by_type(RfcRequirement)
                req_map = {r.id: r for r in requirements}

            # Fallback: when the semantic model has no RfcRequirement nodes
            # (e.g. test mode with no .ivy files), use the requirement graph
            if not req_map:
                graph = await ctx.get_req_graph()
                if graph is not None:
                    for rid, rfc_req in getattr(graph, "rfc_requirements", {}).items():
                        req_map[rid] = rfc_req
                    # If _ivy_requirement_coverage returned empty uncovered_ids
                    # because the model was empty, compute from graph directly
                    if not uncovered_ids:
                        uncovered_ids = set(
                            r.id for r in graph.get_uncovered_requirements()
                        )

            result["uncoveredRfcRequirements"] = [
                {
                    "id": rid,
                    "rfc": getattr(req_map.get(rid), "rfc", ""),
                    "section": getattr(req_map.get(rid), "section", ""),
                    "level": getattr(req_map.get(rid), "level", ""),
                    "text": getattr(req_map.get(rid), "text", ""),
                }
                for rid in sorted(uncovered_ids)
                if rid in req_map
            ]
            # M9 fix: align summary counts with stats overlay.
            # Use stats total when available; fall back to req_map size
            # (covers graph-only mode where semantic model is empty).
            stats_total = stats.get("total", 0)
            result["summary"]["totalRfcReqs"] = stats_total or len(req_map)
            result["summary"]["uncoveredRfcCount"] = len(
                result.get("uncoveredRfcRequirements", [])
            )
        except KeyError:
            pass  # Fall back to visualization handler result

        # Apply protocol filter to uncovered requirements
        protocol_filter = params.get("protocolFilter", "")
        if protocol_filter:
            result["uncoveredRfcRequirements"] = [
                r
                for r in result.get("uncoveredRfcRequirements", [])
                if protocol_filter in r.get("id", "")
                or protocol_filter in r.get("rfc", "").lower()
            ]
            result["unguardedStateVars"] = [
                v
                for v in result.get("unguardedStateVars", [])
                if not v.get("file") or protocol_filter in v.get("file", "")
            ]
            if "summary" in result:
                result["summary"]["uncoveredRfcCount"] = len(
                    result.get("uncoveredRfcRequirements", [])
                )
                result["summary"]["unguardedCount"] = len(
                    result.get("unguardedStateVars", [])
                )

        result["counting_method"] = "requirement_graph_with_stats_overlay"

        # FX5: When the semantic model has no wired edges, impact analysis
        # cannot trace cross-references.  Surface this as a note so callers
        # know why symbol-level impact data is absent.
        if model is None or not getattr(model, "has_edges", lambda: False)():
            result.setdefault("notes", []).append(
                "No cross-reference edges found for this symbol."
            )

        return result

    async def _ivy_coverage_diff(relative_path: str | None = None) -> dict:
        """Compare current coverage against the cached baseline."""
        scope_key = relative_path or "__global__"
        baseline = _coverage_baselines.get(scope_key)
        if baseline is None:
            return error_response(
                "No coverage baseline cached"
                + (f" for scope '{relative_path}'" if relative_path else "")
                + ". Run ivy_coverage(mode='stats') first."
            )

        # Get current stats (this also updates the baseline)
        current = await _ivy_requirement_coverage(relative_path)

        if not current.get("total"):
            return error_response("No requirements found")

        baseline_covered = set(baseline.get("_covered_ids", []))
        current_covered = set(current.get("_covered_ids", []))

        all_ids = (
            baseline_covered
            | current_covered
            | set(
                baseline.get("_uncovered_ids_full", baseline.get("uncovered_ids", []))
            )
            | set(current.get("_uncovered_ids_full", current.get("uncovered_ids", [])))
        )

        new_gaps = sorted(baseline_covered - current_covered)
        recovered = sorted(current_covered - baseline_covered)
        unchanged_covered = len(baseline_covered & current_covered)
        unchanged_uncovered = len(all_ids) - len(current_covered) - len(new_gaps)
        # Clamp to zero in case of data inconsistency
        if unchanged_uncovered < 0:
            unchanged_uncovered = 0

        baseline_pct = baseline.get("coverage_percent", 0)
        current_pct = current.get("coverage_percent", 0)
        delta = round(current_pct - baseline_pct, 1)

        if delta > 0:
            direction = "improved"
        elif delta < 0:
            direction = "regressed"
        else:
            direction = "unchanged"

        parts = []
        if recovered:
            parts.append(f"{len(recovered)} recovered")
        if new_gaps:
            parts.append(f"{len(new_gaps)} new gaps")
        if not parts:
            parts.append("no changes")
        summary = f"Coverage {direction} by {abs(delta)}% ({', '.join(parts)})"

        return {
            "baseline_coverage_percent": baseline_pct,
            "current_coverage_percent": current_pct,
            "delta_percent": delta,
            "delta_direction": direction,
            "new_gaps": new_gaps,
            "recovered": recovered,
            "unchanged_covered": unchanged_covered,
            "unchanged_uncovered": unchanged_uncovered,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_coverage(
        mode: Literal["matrix", "stats", "gaps", "diff"] = "stats",
        relative_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
        compact: bool = True,
        max_items: int = 50,
        scope: str = "",
    ) -> dict:
        """Unified RFC coverage analysis tool.

        Combines traceability matrix, coverage statistics, gap detection,
        and regression diff into a single tool with mode-based dispatch.

        Args:
            mode: Analysis mode.
                - "matrix": Requirement-to-annotation traceability mapping.
                  Shows which RFC requirements are covered by bracket-tag
                  annotations in the codebase.
                - "stats": Coverage statistics by requirement level
                  (MUST/SHOULD/MAY) and layer (default). Also saves a
                  baseline snapshot for later diff comparison.
                - "gaps": Identify unguarded state variables, uncovered RFC
                  requirements, and orphan requirements.
                - "diff": Compare current coverage against the last baseline
                  saved by "stats" mode. Reports new gaps, recovered
                  coverage, and overall delta.
            relative_path: Optional file to scope the analysis to
                (used by "matrix", "stats", and "diff" modes).
            test_file: Optional test entry point whose transitive include
                closure defines the scope. Provides endpoint-mirror scoping
                for NCT-aligned results (used by "matrix", "stats", and
                "gaps" modes). Takes precedence over relative_path.
            protocol: Protocol name to scope results (used by "gaps" mode).
            compact: When True (default), strip internal fields
                (_uncovered_ids_full, _covered_ids) from stats results
                to reduce context window usage.
            max_items: Maximum number of list items to return (default 50).
                Truncates matrix rows and uncovered_ids lists.  Set to 0
                to disable truncation.
            scope: Optional test scope name from the workspace context.
                When set and test_file is not provided, the scope's test
                file is used as the test_file for scoping.  Empty string
                (default) = no scope-based override.
        """
        # Resolve scope -> test_file when scope is provided
        _resolved_scope = None
        if (
            scope
            and not test_file
            and getattr(ctx, "workspace_context", None) is not None
        ):
            _resolved_scope = ctx.workspace_context.get_test_scope(scope)
            if _resolved_scope is not None:
                # Convert absolute test_file path to relative for the tool
                test_file = os.path.relpath(_resolved_scope.test_file, ctx.root)
                logger.debug(
                    "[ivy_coverage] Scope '%s' resolved to test_file='%s'",
                    scope,
                    test_file,
                )
            else:
                logger.warning(
                    "[ivy_coverage] Unknown scope '%s'; proceeding without scoping",
                    scope,
                )

        logger.debug(
            "[ivy_coverage] workspace=%s, args=%r",
            ctx.root,
            {
                "mode": mode,
                "relative_path": relative_path,
                "test_file": test_file,
                "protocol": protocol,
                "scope": scope,
            },
        )
        _tc = ToolTraceContext(
            "ivy_coverage",
            {
                "mode": mode,
                "relative_path": relative_path,
                "test_file": test_file,
                "protocol": protocol,
                "scope": scope,
            },
        )
        _valid_modes = {"matrix", "stats", "gaps", "diff"}
        if mode not in _valid_modes:
            return _tc.finish(
                error_response(
                    f"Unknown mode '{mode}'. Valid modes: {sorted(_valid_modes)}"
                )
            )
        if mode == "matrix":
            result_dict = await _ivy_traceability_matrix(relative_path, test_file)
            if max_items > 0:
                matrix = result_dict.get("matrix", [])
                if len(matrix) > max_items:
                    result_dict["matrix"] = matrix[:max_items]
                    result_dict["matrix_truncated"] = True
                    result_dict["matrix_total"] = len(matrix)
            inject_scope_metadata(result_dict, scope, _resolved_scope)
            return _tc.finish(result_dict)
        elif mode == "gaps":
            result_dict = await _ivy_coverage_gaps(test_file, protocol)
            inject_scope_metadata(result_dict, scope, _resolved_scope)
            return _tc.finish(result_dict)
        elif mode == "diff":
            result_dict = await _ivy_coverage_diff(relative_path)
            inject_scope_metadata(result_dict, scope, _resolved_scope)
            return _tc.finish(result_dict)
        else:  # default: stats
            result_dict = await _ivy_requirement_coverage(relative_path, test_file)
            if compact:
                result_dict.pop("_uncovered_ids_full", None)
                result_dict.pop("_covered_ids", None)
                if max_items > 0:
                    uncovered = result_dict.get("uncovered_ids", [])
                    if len(uncovered) > max_items:
                        result_dict["uncovered_ids"] = uncovered[:max_items]
                        result_dict["uncovered_ids_truncated"] = True
            inject_scope_metadata(result_dict, scope, _resolved_scope)
            return _tc.finish(result_dict)

    # ------------------------------------------------------------------
    # Delegate extraction / manifest tools to traceability_extraction.py
    # ------------------------------------------------------------------
    from ivy_lsp.mcp.tools.traceability_extraction import register_extraction_tools

    register_extraction_tools(mcp, ctx)
