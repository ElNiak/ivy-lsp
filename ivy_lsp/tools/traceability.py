"""Traceability tools: ivy_coverage, ivy_query, ivy_extract_requirements.

Consolidated from the original seven tools:
- ivy_traceability_matrix, ivy_requirement_coverage, ivy_coverage_gaps -> ivy_coverage
- ivy_impact_analysis, ivy_cross_references, ivy_query_symbol -> ivy_query
- ivy_extract_requirements + ivy_generate_manifest -> ivy_extract_requirements
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

from ivy_lsp.tools import error_response

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

    def _model_unavailable_response() -> str:
        """Error response with indexing status note."""
        status = ctx.get_model_status()
        if status.get("state") == "not_built":
            return json.dumps(
                {
                    "success": False,
                    "message": "Semantic model unavailable",
                    "note": "LSP is still indexing. Results may be incomplete. Try again shortly.",
                }
            )
        if status.get("state") == "failed":
            return json.dumps(
                {
                    "success": False,
                    "message": "Semantic model unavailable",
                    "note": f"Model build failed: {status.get('error', 'unknown')}. "
                    f"Retry in {status.get('retry_in_seconds', '?')}s.",
                }
            )
        return error_response("Semantic model unavailable")

    async def _ivy_traceability_matrix(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> str:
        """RFC requirement-to-annotation traceability matrix."""
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
        return json.dumps(result)

    async def _ivy_requirement_coverage(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> str:
        """RFC requirement coverage statistics by level and layer."""
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
            annotations = [a for a in annotations if a.file == abs_path]

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

        return json.dumps(result)

    async def _ivy_coverage_gaps(
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> str:
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
        stats_raw = await _ivy_requirement_coverage(
            relative_path=None, test_file=test_file
        )
        try:
            stats = json.loads(stats_raw)
            uncovered_ids = set(stats.get("_uncovered_ids_full", []))
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
        except (json.JSONDecodeError, KeyError):
            pass  # Fall back to visualization handler result

        result["counting_method"] = "requirement_graph_with_stats_overlay"
        return json.dumps(result)

    async def _ivy_coverage_diff(relative_path: str | None = None) -> str:
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
        current_raw = await _ivy_requirement_coverage(relative_path)
        current = json.loads(current_raw)

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

        return json.dumps(
            {
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
        )

    async def _ivy_impact_analysis(symbol_name: str) -> str:
        """Analyze incoming and outgoing edges for a symbol."""
        model = await ctx.get_model()
        if model is None:
            return _model_unavailable_response()

        from ivy_lsp.semantic.nodes import SymbolNode

        # H10: Find matching symbol nodes with dotted name resolution
        all_symbols = model.get_nodes_by_type(SymbolNode)
        matches = [
            sn
            for sn in all_symbols
            if sn.name == symbol_name or sn.qualified_name == symbol_name
        ]

        # Fallback: dotted suffix match
        if not matches and "." in symbol_name:
            last = symbol_name.rsplit(".", 1)[-1]
            by_last = [sn for sn in all_symbols if sn.name == last]
            suffix = [sn for sn in by_last if sn.qualified_name.endswith(symbol_name)]
            matches = suffix if suffix else by_last

        if not matches:
            return json.dumps(
                {
                    "symbol": symbol_name,
                    "found": False,
                    "message": f"Symbol '{symbol_name}' not found in semantic model",
                }
            )

        sn = matches[0]
        incoming = model.get_incoming(sn.id)
        outgoing = model.get_outgoing(sn.id)

        result: dict[str, Any] = {
            "symbol": symbol_name,
            "found": True,
            "qualified_name": sn.qualified_name,
            "kind": sn.kind,
            "file": sn.file,
            "line": sn.line,
            "incoming_edges": [
                {"type": etype.value, "source": src} for etype, src in incoming
            ],
            "outgoing_edges": [
                {"type": etype.value, "target": tgt} for etype, tgt in outgoing
            ],
            "total_references": len(incoming) + len(outgoing),
        }

        # Note when symbol found but no edges and deep indexing still running
        if not incoming and not outgoing:
            status = ctx.get_model_status()
            if status.get("state") != "ready":
                result["note"] = (
                    "Semantic model is still being built. "
                    "Edge data may be incomplete."
                )

        return json.dumps(result)

    async def _ivy_cross_references(node_id: str) -> str:
        """Query cross-reference graph neighborhood of a node."""
        model = await ctx.get_model()
        if model is None:
            return _model_unavailable_response()

        node = model.get_node(node_id)
        # H7: Fuzzy node_id matching — handle path format mismatches
        # (absolute vs relative paths, different separators, etc.)
        if node is None:
            from ivy_lsp.semantic.nodes import SymbolNode

            # Extract symbol name: last segment after ":" separator
            parts = node_id.split(":")
            symbol_name = parts[-1] if parts else node_id
            all_symbols = model.get_nodes_by_type(SymbolNode)

            # 1. Try exact name or qualified_name match
            sym_matches = [
                sn
                for sn in all_symbols
                if sn.name == symbol_name or sn.qualified_name == symbol_name
            ]

            # 2. Try dotted suffix match
            if not sym_matches and "." in symbol_name:
                last = symbol_name.rsplit(".", 1)[-1]
                by_last = [sn for sn in all_symbols if sn.name == last]
                suffix = [
                    sn for sn in by_last if sn.qualified_name.endswith(symbol_name)
                ]
                sym_matches = suffix if suffix else by_last

            # 3. If file hint present in node_id, use it to narrow results
            if len(sym_matches) > 1 and len(parts) >= 2:
                file_hint = parts[0]
                # Try suffix match on file path (handles absolute vs relative)
                narrowed = [
                    sn
                    for sn in sym_matches
                    if (sn.file or "").endswith(file_hint)
                    or file_hint in (sn.file or "")
                ]
                if narrowed:
                    sym_matches = narrowed

            # 4. Rank by reference count and pick best match
            if sym_matches:
                if len(sym_matches) > 1:

                    def _ref_count(sn):
                        return len(model.get_incoming(sn.id)) + len(
                            model.get_outgoing(sn.id)
                        )

                    sym_matches.sort(key=_ref_count, reverse=True)
                node = sym_matches[0]
                node_id = node.id

        if node is None:
            # Provide sample node IDs for debugging
            from ivy_lsp.semantic.nodes import SymbolNode

            sample_symbols = model.get_nodes_by_type(SymbolNode)[:5]
            sample_ids = [sn.id for sn in sample_symbols]
            return json.dumps(
                {
                    "node_id": node_id,
                    "found": False,
                    "message": f"Node '{node_id}' not found",
                    "hint": "Try using symbol name directly or qualified_name",
                    "sample_node_ids": sample_ids,
                }
            )

        incoming = model.get_incoming(node_id)
        outgoing = model.get_outgoing(node_id)

        return json.dumps(
            {
                "node_id": node_id,
                "found": True,
                "node_type": type(node).__name__,
                "incoming": [
                    {"type": etype.value, "source": src} for etype, src in incoming
                ],
                "outgoing": [
                    {"type": etype.value, "target": tgt} for etype, tgt in outgoing
                ],
            }
        )

    async def _ivy_query_symbol(symbol_name: str, protocol: str = "") -> str:
        """Query rich semantic info about a symbol: type, references, requirements."""
        model = await ctx.get_model()
        if model is None:
            return _model_unavailable_response()

        from ivy_lsp.semantic.nodes import SymbolNode, TypeNode

        # H10: Search SymbolNode with dotted name resolution
        all_sym_nodes = model.get_nodes_by_type(SymbolNode)
        symbol_matches = [
            sn
            for sn in all_sym_nodes
            if sn.name == symbol_name or sn.qualified_name == symbol_name
        ]
        if not symbol_matches and "." in symbol_name:
            last = symbol_name.rsplit(".", 1)[-1]
            by_last = [sn for sn in all_sym_nodes if sn.name == last]
            suffix = [sn for sn in by_last if sn.qualified_name.endswith(symbol_name)]
            symbol_matches = suffix if suffix else by_last

        # Protocol-scoped filtering: when protocol is provided,
        # filter to symbols with file paths containing protocol-testing/{protocol}/
        if protocol and symbol_matches:
            prot_path = f"protocol-testing/{protocol}/"
            prot_filtered = [
                sn for sn in symbol_matches if prot_path in (sn.file or "")
            ]
            if prot_filtered:
                symbol_matches = prot_filtered

        # Disambiguation: when multiple symbols match and no protocol filter
        # narrowed them down, prefer:
        #   a. Files in main protocol directory (not apt/ variants)
        #   b. Files closer to the workspace root (shorter path)
        #   c. Files with more references
        if len(symbol_matches) > 1:

            def _rank_symbol(sn):
                fpath = sn.file or ""
                # Penalize apt/ variant paths
                is_apt = 1 if "/apt/" in fpath else 0
                # Prefer shorter paths (closer to workspace root)
                path_depth = fpath.count("/")
                # Prefer symbols with more references
                ref_count = len(model.get_incoming(sn.id)) + len(
                    model.get_outgoing(sn.id)
                )
                # Lower tuple = better rank
                return (is_apt, path_depth, -ref_count)

            symbol_matches.sort(key=_rank_symbol)

        # Search TypeNode
        type_matches = [
            tn
            for tn in model.get_nodes_by_type(TypeNode)
            if tn.name == symbol_name or tn.qualified_name == symbol_name
        ]

        # Apply protocol filter to type matches too
        if protocol and type_matches:
            prot_path = f"protocol-testing/{protocol}/"
            prot_filtered = [tn for tn in type_matches if prot_path in (tn.file or "")]
            if prot_filtered:
                type_matches = prot_filtered

        if not symbol_matches and not type_matches:
            return json.dumps(
                {
                    "symbol": symbol_name,
                    "found": False,
                    "message": f"Symbol '{symbol_name}' not found",
                }
            )

        result: dict[str, Any] = {
            "symbol": symbol_name,
            "found": True,
        }

        if symbol_matches:
            sn = symbol_matches[0]
            incoming = model.get_incoming(sn.id)
            outgoing = model.get_outgoing(sn.id)
            result["symbol_info"] = {
                "qualified_name": sn.qualified_name,
                "kind": sn.kind,
                "file": sn.file,
                "line": sn.line,
                "params": sn.params,
                "return_sort": sn.return_sort,
                "sort_name": sn.sort_name,
            }
            result["references"] = {
                "incoming": len(incoming),
                "outgoing": len(outgoing),
            }
            # Include all match candidates count for transparency
            if len(symbol_matches) > 1:
                result["disambiguation"] = {
                    "total_candidates": len(symbol_matches),
                    "other_locations": [
                        {"file": s.file, "line": s.line}
                        for s in symbol_matches[1:5]  # up to 4 alternatives
                    ],
                }

        if type_matches:
            tn = type_matches[0]
            result["type_info"] = {
                "qualified_name": tn.qualified_name,
                "file": tn.file,
                "line": tn.line,
                "sort_name": tn.sort_name,
                "is_enum": tn.is_enum,
                "variants": tn.variants,
            }

        return json.dumps(result)

    async def _ivy_extract_requirements_logic(rfc_text: str) -> str:
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

        return json.dumps(
            {
                "requirements": results,
                "total": len(results),
                "by_level": {
                    level: sum(1 for r in results if r["level"] == level)
                    for level in sorted({r["level"] for r in results})
                },
            }
        )

    async def _ivy_generate_manifest(
        rfc_name: str,
        rfc_text: str,
        protocol: str = "",
        base_section: str = "",
    ) -> str:
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

        return json.dumps(
            {
                "yaml": yaml_content,
                "total_requirements": len(results),
                "suggested_path": suggested_path,
                "by_level": {
                    level: sum(1 for r in results if r["level"] == level)
                    for level in sorted({r["level"] for r in results})
                },
            }
        )

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def ivy_coverage(
        mode: Literal["matrix", "stats", "gaps", "diff"] = "stats",
        relative_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> str:
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
        _valid_modes = {"matrix", "stats", "gaps", "diff"}
        if mode not in _valid_modes:
            return error_response(
                f"Unknown mode '{mode}'. Valid modes: {sorted(_valid_modes)}"
            )
        if mode == "matrix":
            return await _ivy_traceability_matrix(relative_path, test_file)
        elif mode == "gaps":
            return await _ivy_coverage_gaps(test_file, protocol)
        elif mode == "diff":
            return await _ivy_coverage_diff(relative_path)
        else:  # default: stats
            return await _ivy_requirement_coverage(relative_path, test_file)

    @mcp.tool()
    async def ivy_query(
        mode: Literal["impact", "xrefs", "info"] = "info",
        symbol_name: str | None = None,
        node_id: str | None = None,
        protocol: str = "",
    ) -> str:
        """Unified semantic query tool for symbols and cross-references.

        Combines impact analysis, cross-reference queries, and rich symbol
        info into a single tool with mode-based dispatch.

        Args:
            mode: Query mode.
                - "impact": Analyze incoming and outgoing edges for a symbol
                  in the semantic model. Requires symbol_name.
                - "xrefs": Query cross-reference graph neighborhood of a node.
                  Requires node_id (or symbol_name as fallback).
                - "info": Query rich semantic info about a symbol: type,
                  references, requirements (default). Requires symbol_name.
            symbol_name: The symbol name to query (required for "impact"
                and "info" modes; optional fallback for "xrefs" mode).
            node_id: The node ID to query for "xrefs" mode
                (e.g., "test.ivy:5:send").
            protocol: Optional protocol name (e.g., "quic") to disambiguate
                symbols. When provided, prefers symbols from the
                ``protocol-testing/{protocol}/`` directory over
                ``apt/`` variants.
        """
        logger.debug(
            "[ivy_query] workspace=%s, args=%r",
            ctx.root,
            {
                "mode": mode,
                "symbol_name": symbol_name,
                "node_id": node_id,
                "protocol": protocol,
            },
        )
        _valid_modes = {"impact", "xrefs", "info"}
        if mode not in _valid_modes:
            return error_response(
                f"Unknown mode '{mode}'. Valid modes: {sorted(_valid_modes)}"
            )
        if mode == "impact":
            if not symbol_name:
                return error_response("symbol_name is required for mode='impact'")
            return await _ivy_impact_analysis(symbol_name)
        elif mode == "xrefs":
            effective_id = node_id or symbol_name
            if not effective_id:
                return error_response(
                    "node_id or symbol_name is required for mode='xrefs'"
                )
            return await _ivy_cross_references(effective_id)
        else:  # default: info
            if not symbol_name:
                return error_response("symbol_name is required for mode='info'")
            return await _ivy_query_symbol(symbol_name, protocol)

    @mcp.tool()
    async def ivy_extract_requirements(
        rfc_text: str = "",
        output: str = "structured",
        rfc_name: str = "",
        protocol: str = "",
        base_section: str = "",
        rfc_source: str = "",
        sections: str = "",
    ) -> str:
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
                return error_response(f"Failed to fetch RFC: {exc}")

        if not rfc_text:
            return error_response("Either rfc_text or rfc_source must be provided")

        if output == "manifest":
            if not rfc_name:
                return error_response("rfc_name is required for output='manifest'")
            result_str = await _ivy_generate_manifest(
                rfc_name, rfc_text, protocol, base_section
            )
            # Add metadata if we fetched the source
            if fetch_result:
                try:
                    import datetime

                    result_data = json.loads(result_str)
                    result_data["metadata"] = {
                        "generated_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        "generator_version": "ivy-lsp",
                        "source": fetch_result.source,
                        "content_hash": fetch_result.content_hash,
                    }
                    return json.dumps(result_data)
                except json.JSONDecodeError:
                    pass
            return result_str
        else:  # default: structured
            return await _ivy_extract_requirements_logic(rfc_text)

    # --- ivy_manifest tool ---

    @mcp.tool()
    async def ivy_manifest(
        mode: Literal["info", "validate", "staleness", "refresh"] = "info",
        protocol: str = "",
        rfc_source: str = "",
        check_online: bool = False,
    ) -> str:
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

            return json.dumps(
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

            return json.dumps(
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

            return json.dumps({"reports": reports})

        elif mode == "refresh":
            if not rfc_source:
                return error_response("rfc_source is required for mode='refresh'")

            # Fetch and extract new requirements
            try:
                from ivy_lsp.rfc.fetcher import fetch_rfc

                fetch_result = await fetch_rfc(rfc_source)

                # Extract requirements from fetched text
                new_reqs_raw = await _ivy_extract_requirements_logic(fetch_result.text)
                new_reqs = json.loads(new_reqs_raw)
            except Exception as exc:
                return error_response(f"Failed to fetch/parse: {exc}")

            # Load current manifest requirements
            current_ids: set[str] = set()
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                current_ids.update(result.requirements.keys())

            return json.dumps(
                {
                    "rfc_source": rfc_source,
                    "new_requirements_found": new_reqs.get("total", 0),
                    "current_manifest_ids": len(current_ids),
                    "by_level": new_reqs.get("by_level", {}),
                    "source_hash": fetch_result.content_hash if fetch_result else "",
                }
            )

        return error_response(f"Unknown mode '{mode}'")

    # --- Individual tool aliases (backward compatibility) ---

    @mcp.tool()
    async def ivy_requirement_coverage(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> str:
        """RFC requirement coverage statistics by level and layer."""
        logger.debug(
            "[ivy_requirement_coverage] workspace=%s, args=%r",
            ctx.root,
            {"relative_path": relative_path, "test_file": test_file},
        )
        return await _ivy_requirement_coverage(relative_path, test_file)

    @mcp.tool()
    async def ivy_coverage_gaps(
        test_file: str | None = None, protocol: str | None = None
    ) -> str:
        """Identify unguarded state variables, uncovered RFC requirements, and orphan requirements."""
        logger.debug(
            "[ivy_coverage_gaps] workspace=%s, args=%r",
            ctx.root,
            {"test_file": test_file, "protocol": protocol},
        )
        return await _ivy_coverage_gaps(test_file, protocol)

    @mcp.tool()
    async def ivy_traceability_matrix(
        relative_path: str | None = None,
        test_file: str | None = None,
    ) -> str:
        """RFC requirement-to-annotation traceability matrix."""
        logger.debug(
            "[ivy_traceability_matrix] workspace=%s, args=%r",
            ctx.root,
            {"relative_path": relative_path, "test_file": test_file},
        )
        return await _ivy_traceability_matrix(relative_path, test_file)

    @mcp.tool()
    async def ivy_query_symbol(symbol_name: str, protocol: str = "") -> str:
        """Query rich semantic info about a symbol."""
        return await _ivy_query_symbol(symbol_name, protocol)

    @mcp.tool()
    async def ivy_impact_analysis(symbol_name: str) -> str:
        """Analyze incoming and outgoing edges for a symbol."""
        return await _ivy_impact_analysis(symbol_name)

    @mcp.tool()
    async def ivy_cross_references(node_id: str) -> str:
        """Query cross-reference graph neighborhood of a node."""
        return await _ivy_cross_references(node_id)

    @mcp.tool()
    async def ivy_generate_manifest(
        rfc_name: str,
        rfc_text: str,
        protocol: str = "",
        base_section: str = "",
    ) -> str:
        """Generate YAML requirements manifest from RFC text."""
        return await _ivy_generate_manifest(rfc_name, rfc_text, protocol, base_section)
