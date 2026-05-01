"""Diagnostic and verification dashboard MCP tools."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Literal

from ivy_lsp.core.patterns import ASSERTION_RE as _ASSERTION_RE
from ivy_lsp.core.patterns import BRACKET_TAG_RE as _BRACKET_TAG_RE
from ivy_lsp.core.patterns import EXPORT_ACTION_RE, MONITOR_RE
from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import (
    error_response,
    inject_dispatch_key,
    inject_scope_metadata,
    safe_tool,
)
from ivy_lsp.mcp.tools._helpers import resolve_scope, validated_path_or_error

logger = logging.getLogger(__name__)


def register_diagnostic_tools(mcp, ctx, get_cache_summary_fn) -> None:
    """Register diagnostic and verification dashboard MCP tools.

    Args:
        mcp: The MCP server instance.
        ctx: The workspace context.
        get_cache_summary_fn: Callable returning the verification cache summary dict.
    """

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_diagnostics(
        relative_path: str = "",
        mode: Literal["structural", "full", "collisions", "dashboard"] = "full",
        layers: list[str] | None = None,
        min_severity: str | None = None,
        scope: str = "",
    ) -> dict:
        """Diagnostic analysis of Ivy files, workspace collisions, and verification cache status.

        Modes:
        - structural: fast structural lint (ms, no subprocess) → {diagnostics: [{file, line, severity, message}], counts{}}
        - full: all 5 diagnostic layers → {diagnostics[], counts{}, layers_run[]}
        - collisions: workspace-level include-name collision report → {collisions: [{name, files[]}]}
        - dashboard: verification cache status → {total_files, verified, failed, pending, verified_files[], failed_files[]}

        For fast feedback during editing, use structural. For pre-commit checks, use full. Use dashboard to see overall verification progress.

        IMPORTANT: full mode requires the semantic model (ivy_index first). structural mode works without it.
        """
        logger.debug(
            "[ivy_diagnostics] workspace=%s, args=%r",
            ctx.root,
            {
                "relative_path": relative_path,
                "mode": mode,
                "layers": layers,
                "min_severity": min_severity,
                "scope": scope,
            },
        )

        _resolved_scope = resolve_scope(ctx, scope, "ivy_diagnostics")
        _scope_files: frozenset[str] | None = (
            _resolved_scope.include_closure if _resolved_scope is not None else None
        )

        _tc = ToolTraceContext(
            "ivy_diagnostics",
            {
                "relative_path": relative_path,
                "mode": mode,
                "layers": layers,
                "min_severity": min_severity,
                "scope": scope,
            },
        )
        if mode not in ("structural", "full", "collisions", "dashboard"):
            return _tc.finish(
                error_response(
                    f"Unknown mode '{mode}'. Valid modes: ['structural', 'full', 'collisions', 'dashboard']"
                )
            )

        if mode == "dashboard":
            result = await _verification_dashboard_impl(ctx, get_cache_summary_fn)
            return _tc.finish(inject_dispatch_key(result, "dashboard"))

        if mode == "collisions":
            from ivy_lsp.mcp.tools.analysis import _handle_collisions_mode

            result = await _handle_collisions_mode(ctx)
            return _tc.finish(inject_dispatch_key(result, "collisions"))

        abs_path, err = validated_path_or_error(ctx, relative_path)
        if err:
            return _tc.finish(err)
        assert abs_path is not None
        if not os.path.isfile(abs_path):
            return _tc.finish(error_response(f"File not found: {relative_path}"))

        # Skip file if it falls outside the requested scope
        if _scope_files is not None and abs_path not in _scope_files:
            return _tc.finish(
                {
                    "success": True,
                    "file": relative_path,
                    "mode": mode,
                    "diagnostics": [],
                    "diagnostic_count": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "scope": scope,
                    "scope_filtered": True,
                    "note": f"File not in scope '{scope}' include closure; skipped.",
                }
            )

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        if mode == "structural":
            resolve_cb = ctx.make_resolve_callback()
            diagnostics = ctx.check_structural_issues(source, abs_path, resolve_cb)
            _struct_result: dict[str, Any] = {
                "success": True,
                "file": relative_path,
                "mode": "structural",
                "diagnostics": diagnostics,
                "diagnostic_count": len(diagnostics),
                "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
                "warning_count": sum(
                    1 for d in diagnostics if d["severity"] == "warning"
                ),
            }
            inject_scope_metadata(_struct_result, scope, _resolved_scope)
            return _tc.finish(_struct_result)

        # Full mode: all 5 diagnostic layers
        all_diags: list[dict[str, Any]] = []
        layer_errors: list[dict[str, str]] = []

        # 1. Structural checks
        if layers is None or "structural" in layers:
            resolve_cb = ctx.make_resolve_callback()
            all_diags.extend(ctx.check_structural_issues(source, abs_path, resolve_cb))

        # 2. Lexer errors via fallback scanner (no Z3 needed)
        if layers is None or "lexer" in layers:
            try:
                from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

                loop = asyncio.get_running_loop()
                _symbols, error_info = await loop.run_in_executor(
                    ctx.tool_executor,
                    lambda: fallback_scan(source, abs_path),
                )
                if error_info is not None:
                    all_diags.append(
                        {
                            "line": error_info.get("line", 1),
                            "severity": "error",
                            "message": f"Lexer error: {error_info.get('message', 'unknown')}",
                            "source": "ivy-lsp-lexer",
                        }
                    )
            except Exception as exc:
                logger.warning("Fallback scan failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "lexer", "error": str(exc)})

        # 3. Semantic diagnostics (orphaned RFC tags, untagged assertions)
        if layers is None or "semantic" in layers:
            try:
                # Check model status — avoid blocking on first-time build
                _model_status = ctx.get_model_status()
                if _model_status.get("state") == "ready":
                    model = await ctx.get_model()  # instant — already built
                elif hasattr(ctx, "get_model_or_none") and callable(
                    ctx.get_model_or_none
                ):
                    _result = ctx.get_model_or_none(timeout=5.0)
                    if asyncio.iscoroutine(_result):
                        model = await _result
                    else:
                        model = _result
                else:
                    model = None
                if model is None and _model_status.get("state") != "ready":
                    layer_errors.append(
                        {
                            "layer": "semantic",
                            "error": (
                                f"Model {_model_status.get('state', 'unavailable')} "
                                "(building in background; retry in 30s for full results)"
                            ),
                        }
                    )
                if model is not None:
                    from ivy_lsp.core.semantic.nodes import (
                        RfcAnnotation,
                        RfcRequirement,
                    )
                    from ivy_lsp.core.semantic.rfc_annotations import is_tag_covered

                    rfc_reqs = model.get_nodes_by_type(RfcRequirement)  # type: ignore[union-attr]
                    annotations = [
                        n
                        for n in model.get_nodes_by_type(RfcAnnotation)  # type: ignore[union-attr]
                        if n.file == abs_path
                    ]
                    if rfc_reqs:
                        req_ids = {r.id for r in rfc_reqs}
                        for ann in annotations:
                            for tag in ann.tags:
                                if not is_tag_covered(tag, req_ids):
                                    all_diags.append(
                                        {
                                            "line": ann.line + 1,
                                            "severity": "warning",
                                            "message": (
                                                f"Orphaned RFC tag: [{tag}] does not "
                                                "match any loaded requirement manifest"
                                            ),
                                            "source": "ivy-lsp-semantic",
                                        }
                                    )

                    # Missing tags on assertions
                    lines = source.split("\n")
                    for m in _ASSERTION_RE.finditer(source):
                        line_no = source[: m.start()].count("\n")
                        line_text = lines[line_no] if line_no < len(lines) else ""
                        if not _BRACKET_TAG_RE.search(line_text):
                            all_diags.append(
                                {
                                    "line": line_no + 1,
                                    "severity": "hint",
                                    "message": "Assertion without RFC bracket tag annotation",
                                    "source": "ivy-lsp-semantic",
                                }
                            )
            except Exception as exc:
                logger.warning(
                    "Semantic diagnostics failed for %s: %s", relative_path, exc
                )
                layer_errors.append({"layer": "semantic", "error": str(exc)})

        # 4. Coverage hints
        if layers is None or "coverage" in layers:
            try:
                graph = await ctx.get_req_graph()
                if graph is not None:
                    from ivy_lsp.core.coverage_hints import compute_coverage_hints

                    for hint in compute_coverage_hints(graph, abs_path):
                        all_diags.append(hint.to_mcp_dict())
            except Exception as exc:
                logger.warning("Coverage hints failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "coverage", "error": str(exc)})

        # 5. Pattern diagnostics (regex-based)
        if layers is None or "pattern" in layers:
            try:
                basename = os.path.basename(abs_path)

                # Missing _finalize in test files
                if "test" in basename.lower() and "_finalize" not in source:
                    has_export = bool(
                        re.search(r"^\s*export\s+action", source, re.MULTILINE)
                    )
                    if has_export:
                        all_diags.append(
                            {
                                "line": 1,
                                "severity": "warning",
                                "message": (
                                    "Test file has exports but no _finalize action. "
                                    "Consider adding 'export action _finalize' for "
                                    "end-of-test assertions."
                                ),
                                "source": "ivy-pattern",
                            }
                        )

                # Exported actions without monitors
                exports = set(EXPORT_ACTION_RE.findall(source))
                monitored = set(MONITOR_RE.findall(source))
                for exp_action in exports:
                    if exp_action not in monitored and exp_action != "_finalize":
                        action_defined = bool(
                            re.search(
                                rf"^\s*action\s+{re.escape(exp_action)}\s*",
                                source,
                                re.MULTILINE,
                            )
                        )
                        if action_defined:
                            match = re.search(
                                rf"^\s*export\s+action\s+{re.escape(exp_action)}",
                                source,
                                re.MULTILINE,
                            )
                            line_num = (
                                source[: match.start()].count("\n") + 1 if match else 1
                            )
                            all_diags.append(
                                {
                                    "line": line_num,
                                    "severity": "hint",
                                    "message": (
                                        f"Exported action '{exp_action}' has no "
                                        "before/after monitor in this file."
                                    ),
                                    "source": "ivy-pattern",
                                }
                            )
            except Exception as exc:
                logger.warning(
                    "Pattern diagnostics failed for %s: %s", relative_path, exc
                )
                layer_errors.append({"layer": "pattern", "error": str(exc)})

        # P1: Ensure each diagnostic has the file field for multi-file processing
        for d in all_diags:
            if "file" not in d:
                d["file"] = relative_path

        # Apply severity filter
        if min_severity:
            _sev_order = {"error": 4, "warning": 3, "info": 2, "hint": 1}
            min_rank = _sev_order.get(min_severity, 0)
            all_diags = [
                d
                for d in all_diags
                if _sev_order.get(d.get("severity", "hint"), 0) >= min_rank
            ]

        # Build source-breakdown summary
        by_source: dict[str, int] = {}
        for d in all_diags:
            src = d.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        _diag_result: dict[str, Any] = {
            "success": True,
            "file": relative_path,
            "mode": mode,
            "diagnostics": all_diags,
            "diagnostic_count": len(all_diags),
            "by_source": by_source,
            "error_count": sum(1 for d in all_diags if d.get("severity") == "error"),
            "warning_count": sum(
                1 for d in all_diags if d.get("severity") == "warning"
            ),
            "hint_count": sum(1 for d in all_diags if d.get("severity") == "hint"),
            "info_count": sum(1 for d in all_diags if d.get("severity") == "info"),
            "layer_errors": layer_errors,
            "partial": bool(layer_errors),
        }
        inject_scope_metadata(_diag_result, scope, _resolved_scope)

        return _tc.finish(_diag_result)

    async def _verification_dashboard_impl(ctx, get_cache_summary_fn) -> dict:
        """Build verification dashboard result dict."""
        ivy_files = ctx.find_ivy_files(ctx.root)
        cache = get_cache_summary_fn()
        verified_set = set(cache["verified_files"])
        failed_set = set(cache["failed_files"])
        pending = [
            f for f in ivy_files if f not in verified_set and f not in failed_set
        ]
        return {
            "success": True,
            "total_files": len(ivy_files),
            "verified": len(verified_set),
            "failed": len(failed_set),
            "pending": len(pending),
            "cache_size": cache["cache_size"],
            "cache_max": cache["cache_max"],
            "verified_files": sorted(verified_set),
            "failed_files": sorted(failed_set),
        }
