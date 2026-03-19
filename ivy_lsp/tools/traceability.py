"""Traceability tools: ivy_coverage, ivy_extract_requirements, ivy_manifest.

Consolidated from the original seven tools:
- ivy_traceability_matrix, ivy_requirement_coverage, ivy_coverage_gaps -> ivy_coverage
- ivy_extract_requirements + ivy_generate_manifest -> ivy_extract_requirements

Note: ivy_query (impact, xrefs, info) was removed — those capabilities are
provided by the LSP server via hover, findReferences, and call hierarchy.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

from ivy_lsp.debug_trace import ToolTraceContext
from ivy_lsp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)

_RFC_REQ_PATTERN = re.compile(
    r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
    r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
    re.MULTILINE,
)


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

        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement
        from ivy_lsp.semantic.rfc_annotations import normalize_tag_with_diagnostics

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

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

        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement
        from ivy_lsp.semantic.rfc_annotations import normalize_tag_with_diagnostics

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

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
        from ivy_lsp.semantic.rfc_annotations import find_manifests

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
        from ivy_lsp.features.visualization import handle_coverage_gaps

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
                from ivy_lsp.semantic.nodes import RfcRequirement

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

    async def _ivy_extract_requirements_logic(rfc_text: str) -> dict:
        """Parse RFC text to extract MUST/SHOULD/MAY structured requirements."""
        results = []
        for m in _RFC_REQ_PATTERN.finditer(rfc_text):
            text = m.group(1).strip()
            level = m.group(2)
            # Normalize level
            if level in ("SHALL", "REQUIRED"):
                level = "MUST"
            elif level in ("SHALL NOT",):
                level = "MUST NOT"
            elif level in ("RECOMMENDED",):
                level = "SHOULD"
            elif level in ("OPTIONAL",):
                level = "MAY"

            results.append(
                {
                    "text": text,
                    "level": level,
                    "offset": m.start(),
                }
            )

        return {
            "requirements": results,
            "total": len(results),
            "by_level": {
                level: sum(1 for r in results if r["level"] == level)
                for level in sorted({r["level"] for r in results})
            },
        }

    async def _ivy_generate_manifest(
        rfc_name: str,
        rfc_text: str,
        protocol: str = "",
        base_section: str = "",
    ) -> dict:
        """Generate a YAML requirements manifest from RFC text."""
        results = []
        for m in _RFC_REQ_PATTERN.finditer(rfc_text):
            text = m.group(1).strip()
            level = m.group(2)
            if level in ("SHALL", "REQUIRED"):
                level = "MUST"
            elif level in ("SHALL NOT",):
                level = "MUST NOT"
            elif level in ("RECOMMENDED",):
                level = "SHOULD"
            elif level in ("OPTIONAL",):
                level = "MAY"
            results.append({"text": text, "level": level, "offset": m.start()})

        rfc_lower = rfc_name.lower().replace(" ", "")
        manifest_lines = [
            f"rfc: {rfc_name}",
            f"title: '{protocol.upper()} protocol requirements'",
            "requirements:",
        ]
        for i, req in enumerate(results, start=1):
            section = f"{base_section}.{i}" if base_section else str(i)
            tag = f"{rfc_lower}:{section}"
            escaped_text = req["text"].replace("'", "''")
            manifest_lines.append(f"  {tag}:")
            manifest_lines.append(f"    text: '{escaped_text}'")
            manifest_lines.append(f"    section: '{section}'")
            manifest_lines.append(f"    level: {req['level']}")
            manifest_lines.append(f"    layer: ''")
            manifest_lines.append(f"    testable: true")

        yaml_content = "\n".join(manifest_lines) + "\n"
        suggested_path = ""
        if protocol:
            suggested_path = (
                f"protocol-testing/{protocol}/" f"{rfc_lower}_requirements.yaml"
            )

        return {
            "yaml": yaml_content,
            "total_requirements": len(results),
            "suggested_path": suggested_path,
            "by_level": {
                level: sum(1 for r in results if r["level"] == level)
                for level in sorted({r["level"] for r in results})
            },
        }

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @safe_tool
    async def ivy_coverage(
        mode: Literal["matrix", "stats", "gaps", "diff"] = "stats",
        relative_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
        compact: bool = True,
        max_items: int = 50,
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
        """
        logger.debug(
            "[ivy_coverage] workspace=%s, args=%r",
            ctx.root,
            {
                "mode": mode,
                "relative_path": relative_path,
                "test_file": test_file,
                "protocol": protocol,
            },
        )
        _tc = ToolTraceContext(
            "ivy_coverage",
            {
                "mode": mode,
                "relative_path": relative_path,
                "test_file": test_file,
                "protocol": protocol,
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
            return _tc.finish(result_dict)
        elif mode == "gaps":
            return _tc.finish(await _ivy_coverage_gaps(test_file, protocol))
        elif mode == "diff":
            return _tc.finish(await _ivy_coverage_diff(relative_path))
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
            return _tc.finish(result_dict)

    @mcp.tool()
    @safe_tool
    async def ivy_extract_requirements(
        rfc_text: str = "",
        output: str = "structured",
        rfc_name: str = "",
        protocol: str = "",
        base_section: str = "",
        rfc_source: str = "",
        sections: str = "",
    ) -> dict:
        """Parse RFC text to extract MUST/SHOULD/MAY requirements.

        Can output either structured requirement data or a YAML manifest
        ready for traceability tools.

        Args:
            rfc_text: Raw RFC text to parse for normative requirements.
                Can be empty when rfc_source is provided.
            output: Output format.
                - "structured": Extracted requirements as JSON with text,
                  level, offset, and by_level counts (default).
                - "manifest": YAML requirements manifest ready for
                  traceability tools. Requires rfc_name.
            rfc_name: RFC identifier (e.g., "RFC9000"). Required for
                output="manifest".
            protocol: Protocol name for layer inference (e.g., "quic").
                Used by output="manifest" for suggested path.
            base_section: Default section prefix (e.g., "4"). Used by
                output="manifest" for requirement IDs.
            rfc_source: RFC source to fetch text from. Accepts:
                - RFC number: "RFC9000" or "9000"
                - Internet draft: "draft-ietf-quic-transport-34"
                - Local file path: "/path/to/rfc.txt"
                - Direct URL: "https://example.com/rfc.txt"
                When provided, fetches text automatically (rfc_text ignored).
            sections: Comma-separated section numbers to extract from
                (e.g., "4,5,7.1"). Only used with rfc_source.
        """
        logger.debug(
            "[ivy_extract_requirements] workspace=%s, args=%r",
            ctx.root,
            {
                "output": output,
                "rfc_name": rfc_name,
                "protocol": protocol,
                "rfc_source": rfc_source,
                "sections": sections,
                "rfc_text_len": len(rfc_text),
            },
        )
        _tc = ToolTraceContext(
            "ivy_extract_requirements",
            {
                "output": output,
                "rfc_name": rfc_name,
                "rfc_source": rfc_source,
                "rfc_text_len": len(rfc_text),
            },
        )

        fetch_result = None
        # If rfc_source is provided, fetch and optionally filter by section
        if rfc_source:
            try:
                from ivy_lsp.rfc.fetcher import fetch_rfc
                from ivy_lsp.rfc.parser import get_section_text, parse_rfc_text

                fetch_result = await fetch_rfc(rfc_source)
                parsed = parse_rfc_text(fetch_result.text)

                # Auto-detect rfc_name if not provided
                if not rfc_name and parsed.rfc_number:
                    rfc_name = f"RFC{parsed.rfc_number}"

                # Filter by sections if specified
                if sections:
                    section_list = [s.strip() for s in sections.split(",") if s.strip()]
                    rfc_text = get_section_text(parsed, section_list)
                else:
                    rfc_text = fetch_result.text

            except Exception as exc:
                return _tc.finish(error_response(f"Failed to fetch RFC: {exc}"))

        if not rfc_text:
            return _tc.finish(
                error_response("Either rfc_text or rfc_source must be provided")
            )

        if output == "manifest":
            if not rfc_name:
                return _tc.finish(
                    error_response("rfc_name is required for output='manifest'")
                )
            result_data = await _ivy_generate_manifest(
                rfc_name, rfc_text, protocol, base_section
            )
            # Add metadata if we fetched the source
            if fetch_result:
                import datetime

                result_data["metadata"] = {
                    "generated_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "generator_version": "ivy-lsp",
                    "source": fetch_result.source,
                    "content_hash": fetch_result.content_hash,
                }
            return _tc.finish(result_data)
        else:  # default: structured
            return _tc.finish(await _ivy_extract_requirements_logic(rfc_text))

    # --- ivy_manifest tool ---

    @mcp.tool()
    @safe_tool
    async def ivy_manifest(
        mode: Literal["info", "validate", "staleness", "refresh"] = "info",
        protocol: str = "",
        rfc_source: str = "",
        check_online: bool = False,
    ) -> dict:
        """Manage and inspect requirement manifests.

        Args:
            mode: Operation mode.
                - "info": Discover all manifests, summarize each, detect
                  protocols without manifests.
                - "validate": Run validation on discovered manifests.
                  Reports missing fields, invalid levels, etc.
                - "staleness": Check if manifests are up-to-date
                  relative to their source RFCs.
                - "refresh": Fetch RFC source, extract requirements,
                  and diff against current manifest by ID.
            protocol: Protocol name to filter (e.g. "quic").
            rfc_source: RFC source for refresh mode.
            check_online: Whether to check RFC editor API for
                staleness (obsolescence, updates, errata).
        """
        logger.debug(
            "[ivy_manifest] workspace=%s, args=%r",
            ctx.root,
            {"mode": mode, "protocol": protocol, "rfc_source": rfc_source},
        )
        _tc = ToolTraceContext(
            "ivy_manifest",
            {"mode": mode, "protocol": protocol, "rfc_source": rfc_source},
        )

        from ivy_lsp.semantic.rfc_annotations import (
            find_manifests,
            load_manifest_with_metadata,
            validate_manifest,
        )

        workspace_root = ctx.root
        all_manifests = find_manifests(workspace_root)

        # Filter by protocol if specified
        if protocol:
            prot_filter = f"protocol-testing/{protocol}/"
            all_manifests = [m for m in all_manifests if prot_filter in m]

        if mode == "info":
            summaries = []
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                # Infer protocol from path
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )
                prot = ""
                if "protocol-testing/" in rel:
                    parts = rel.split("protocol-testing/")[1].split("/")
                    if parts:
                        prot = parts[0]
                summaries.append(
                    {
                        "path": rel,
                        "protocol": prot,
                        "requirements": len(result.requirements),
                        "has_metadata": result.metadata is not None,
                        "warnings": len(result.warnings),
                    }
                )

            # Detect protocols without manifests
            pt_dir = os.path.join(workspace_root, "protocol-testing")
            protocols_with_manifests = {
                s["protocol"] for s in summaries if s["protocol"]
            }
            protocols_without = []
            if os.path.isdir(pt_dir):
                for entry in os.listdir(pt_dir):
                    if os.path.isdir(os.path.join(pt_dir, entry)):
                        if entry not in protocols_with_manifests:
                            protocols_without.append(entry)

            return _tc.finish(
                {
                    "manifests": summaries,
                    "total_manifests": len(summaries),
                    "protocols_without_manifests": protocols_without,
                }
            )

        elif mode == "validate":
            import yaml

            results = []
            for mpath in all_manifests:
                try:
                    with open(mpath, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                except Exception as exc:
                    results.append(
                        {
                            "path": mpath,
                            "warnings": [f"Failed to load: {exc}"],
                        }
                    )
                    continue

                warnings = (
                    validate_manifest(data)
                    if isinstance(data, dict)
                    else ["Not a mapping"]
                )
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )
                results.append(
                    {
                        "path": rel,
                        "warnings": warnings,
                        "valid": len(warnings) == 0,
                    }
                )

            return _tc.finish(
                {
                    "results": results,
                    "total_manifests": len(results),
                    "all_valid": all(r.get("valid", False) for r in results),
                }
            )

        elif mode == "staleness":
            from ivy_lsp.rfc.staleness import check_staleness

            reports = []
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )

                if result.metadata is None:
                    reports.append(
                        {
                            "path": rel,
                            "status": "no_metadata",
                            "info": ["No metadata section; cannot check staleness."],
                        }
                    )
                    continue

                meta = result.metadata
                rfc_num = ""
                rfc_field = ""
                # Try to extract RFC number from the rfc field
                for rpath in all_manifests:
                    if rpath == mpath:
                        try:
                            with open(rpath, encoding="utf-8") as f:
                                import yaml

                                rdata = yaml.safe_load(f)
                            rfc_field = (
                                str(rdata.get("rfc", ""))
                                if isinstance(rdata, dict)
                                else ""
                            )
                        except Exception:
                            pass
                        break

                import re as _re

                rfc_match = _re.search(r"(\d+)", rfc_field)
                if rfc_match:
                    rfc_num = rfc_match.group(1)

                report = await check_staleness(
                    manifest_source=meta.source,
                    manifest_hash=meta.content_hash,
                    rfc_number=rfc_num,
                    check_online=check_online,
                )
                reports.append(
                    {
                        "path": rel,
                        "is_stale": report.is_stale,
                        "reasons": report.reasons,
                        "info": report.info,
                        "content_hash_match": report.content_hash_match,
                        "obsoleted_by": report.obsoleted_by,
                        "updated_by": report.updated_by,
                    }
                )

            return _tc.finish({"reports": reports})

        elif mode == "refresh":
            if not rfc_source:
                return _tc.finish(
                    error_response("rfc_source is required for mode='refresh'")
                )

            # Fetch and extract new requirements
            try:
                from ivy_lsp.rfc.fetcher import fetch_rfc

                fetch_result = await fetch_rfc(rfc_source)

                # Extract requirements from fetched text
                new_reqs = await _ivy_extract_requirements_logic(fetch_result.text)
            except Exception as exc:
                return _tc.finish(error_response(f"Failed to fetch/parse: {exc}"))

            # Load current manifest requirements
            current_ids: set[str] = set()
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                current_ids.update(result.requirements.keys())

            return _tc.finish(
                {
                    "rfc_source": rfc_source,
                    "new_requirements_found": new_reqs.get("total", 0),
                    "current_manifest_ids": len(current_ids),
                    "by_level": new_reqs.get("by_level", {}),
                    "source_hash": (fetch_result.content_hash if fetch_result else ""),
                }
            )

        return _tc.finish(error_response(f"Unknown mode '{mode}'"))
