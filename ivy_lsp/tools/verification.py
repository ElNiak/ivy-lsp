"""Verification tools: ivy_verify, ivy_compile, ivy_model_info, ivy_diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from ivy_lsp.utils.ivy_output import extract_error_summary, parse_ivy_output
from ivy_lsp.utils.validation import validate_ivy_param as _validate_ivy_param
from ivy_lsp.verification import (
    run_ivy_check as shared_ivy_check,
    run_ivy_compile as shared_ivy_compile,
    run_ivy_show as shared_ivy_show,
)

logger = logging.getLogger(__name__)

# Per-isolate verification cache: (abs_path, isolate|None) -> result_dict
_verify_cache: dict[tuple[str, str | None], dict] = {}
_verify_cache_lock = asyncio.Lock()

# Assertion/tag detection for ivy_diagnostics semantic layer
_ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)
_BRACKET_TAG_RE = re.compile(r"#\s*\[")


# Pattern for per-isolate status in ivy_check output, e.g.:
# "  isolate quic_server_test_stream: PASS" or "FAIL"
_ISOLATE_STATUS_RE = re.compile(
    r"^\s*isolate\s+([\w.]+)\s*:\s*(PASS|FAIL|OK)\s*$", re.MULTILINE
)


def _cache_per_isolate_results(
    abs_path: str,
    raw_output: str,
    full_result: dict[str, Any],
) -> None:
    """Extract per-isolate status from full verification output and cache each."""
    for m in _ISOLATE_STATUS_RE.finditer(raw_output):
        iso_name = m.group(1)
        status = m.group(2)
        iso_key = (abs_path, iso_name)
        if iso_key not in _verify_cache:
            iso_success = status in ("PASS", "OK")
            # Filter diagnostics to those mentioning this isolate (best effort)
            iso_diags = [
                d for d in full_result.get("diagnostics", [])
                if iso_name in d.get("message", "")
                or iso_name in d.get("file", "")
            ]
            _verify_cache[iso_key] = {
                "success": iso_success,
                "diagnostics": iso_diags,
                "diagnostic_count": len(iso_diags),
                "error_summary": full_result.get("error_summary", "") if not iso_success else "",
                "raw_output": raw_output,
                "duration_seconds": full_result.get("duration_seconds", 0),
                "cached": False,
                "isolate": iso_name,
            }


def register_verification_tools(mcp: Any, ctx: Any) -> None:
    """Register verification-related MCP tools."""

    @mcp.tool()
    async def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
        use_cache: bool = False,
    ) -> str:
        """Run ivy_check on an Ivy file to verify formal properties.

        Returns structured diagnostics with file, line, severity, and message.

        Args:
            relative_path: Relative path to the .ivy file to check.
            isolate: Optional isolate name to check in isolation.
            use_cache: When True, return cached result if available.
        """
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        if isolate:
            try:
                _validate_ivy_param(isolate)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})

        # Serialize cache check-and-write to prevent TOCTOU race
        async with _verify_cache_lock:
            # Check cache if requested
            cache_key = (abs_path, isolate)
            if use_cache and cache_key in _verify_cache:
                cached_result = dict(_verify_cache[cache_key])
                cached_result["cached"] = True
                return json.dumps(cached_result)

            result = await shared_ivy_check(
                filepath=abs_path,
                workspace_root=ctx.root,
                isolate=isolate,
                staging_dir=ctx.staging_dir,
            )

            # Parse counterexample if verification failed
            if not result.get("success", True):
                from ivy_lsp.utils.counterexample_parser import parse_counterexample

                raw = result.get("raw_output", "")
                cex = parse_counterexample(raw)
                if cex is not None:
                    result["counterexample"] = cex

            result["cached"] = False

            # Cache the result for this (file, isolate) pair
            _verify_cache[cache_key] = dict(result)

            # When full verification (no isolate), also cache individual isolate
            # results if the output contains per-isolate status lines
            if isolate is None:
                raw_output = result.get("raw_output", "")
                _cache_per_isolate_results(abs_path, raw_output, result)

        return json.dumps(result)

    @mcp.tool()
    async def ivy_compile(
        relative_path: str,
        target: str = "test",
        isolate: str | None = None,
    ) -> str:
        """Compile an Ivy file to a test executable using ivyc.

        When a Docker image is configured (--docker-image), compilation runs
        inside a Docker container with all required C++ dependencies.
        Otherwise falls back to native subprocess execution.

        Args:
            relative_path: Relative path to the .ivy file to compile.
            target: Compilation target (default: "test").
            isolate: Optional isolate name to compile in isolation.
        """
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        try:
            _validate_ivy_param(target)
            if isolate:
                _validate_ivy_param(isolate)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})

        t0 = time.monotonic()
        _docker_fallback_reason: str | None = None

        # Try API executor path (Docker-aware compilation)
        if ctx.executor is not None and ctx.base_path is not None:
            try:
                from pathlib import Path as P

                from api.compiler import generate_compile_commands

                compile_result = generate_compile_commands(
                    ivy_file=P(abs_path),
                    base_path=ctx.base_path,
                )

                start = time.monotonic()
                # Run setup + compilation via thread pool (executor.execute is blocking)
                setup_result = await asyncio.to_thread(
                    ctx.executor.execute,
                    compile_result.setup_commands,
                    workspace_root=ctx.root,
                    timeout=30,
                )
                if hasattr(setup_result, 'exit_code') and setup_result.exit_code != 0:
                    return json.dumps({
                        "success": False,
                        "message": f"Docker setup failed (exit {setup_result.exit_code})",
                        "raw_output": getattr(setup_result, 'stderr', ''),
                        "duration_seconds": round(time.monotonic() - t0, 2),
                    })
                exec_result = await asyncio.to_thread(
                    ctx.executor.execute,
                    compile_result.compile_commands,
                    workspace_root=ctx.root,
                    timeout=300,
                )
                duration = time.monotonic() - start

                raw_output = (
                    exec_result.stderr + "\n" + exec_result.stdout
                ).strip()
                diagnostics = parse_ivy_output(raw_output)
                return json.dumps({
                    "success": exec_result.exit_code == 0
                    and not any(
                        d["severity"] == "error" for d in diagnostics
                    ),
                    "diagnostics": diagnostics,
                    "diagnostic_count": len(diagnostics),
                    "error_summary": extract_error_summary(
                        raw_output, diagnostics
                    ),
                    "raw_output": raw_output,
                    "target": exec_result.target,
                    "duration_seconds": round(duration, 2),
                })
            except ImportError:
                logger.debug(
                    "panther_ivy.api not available; falling back to direct subprocess"
                )
            except Exception as exc:
                logger.warning(
                    "API executor failed: %s; falling back to direct subprocess",
                    exc,
                )
                _docker_fallback_reason = str(exc)

        # Direct subprocess fallback
        result = await shared_ivy_compile(
            filepath=abs_path,
            workspace_root=ctx.root,
            target=target,
            isolate=isolate,
            staging_dir=ctx.staging_dir,
        )

        if _docker_fallback_reason:
            result["fallback"] = "subprocess"
            result["fallback_reason"] = _docker_fallback_reason

        return json.dumps(result)

    @mcp.tool()
    async def ivy_model_info(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Display the structure of an Ivy model using ivy_show.

        Args:
            relative_path: Relative path to the .ivy file to inspect.
            isolate: Optional isolate name for a specific isolate.
        """
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        if isolate:
            try:
                _validate_ivy_param(isolate)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})

        result = await shared_ivy_show(
            filepath=abs_path,
            workspace_root=ctx.root,
            isolate=isolate,
            staging_dir=ctx.staging_dir,
        )
        return json.dumps(result)

    @mcp.tool()
    async def ivy_diagnostics(
        relative_path: str,
        layers: list[str] | None = None,
        min_severity: str | None = None,
    ) -> str:
        """Full diagnostic analysis of an Ivy file.

        Runs 5 diagnostic layers (structural, lexer, semantic, coverage,
        pattern) — comparable to what an IDE shows via
        textDocument/publishDiagnostics. More thorough than ivy_lint but
        may take longer on first call (lazy model/graph building).

        Use ivy_lint for quick structural checks (milliseconds).
        Use ivy_diagnostics for thorough analysis after editing.

        Args:
            relative_path: Relative path to the .ivy file to diagnose.
            layers: Optional list of layers to run. Valid values: structural,
                lexer, semantic, coverage, pattern. Defaults to all.
            min_severity: Minimum severity to include: error, warning, info, hint.
        """
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        all_diags: list[dict[str, Any]] = []
        layer_errors: list[dict[str, str]] = []

        # 1. Structural checks (same as ivy_lint)
        if layers is None or "structural" in layers:
            resolve_cb = ctx.make_resolve_callback()
            all_diags.extend(ctx.check_structural_issues(source, abs_path, resolve_cb))

        # 2. Lexer errors via fallback scanner (no Z3 needed)
        if layers is None or "lexer" in layers:
            try:
                from ivy_lsp.parsing.fallback_scanner import fallback_scan

                _symbols, error_info = await asyncio.to_thread(
                    fallback_scan, source, abs_path,
                )
                if error_info is not None:
                    all_diags.append({
                        "line": error_info.get("line", 1),
                        "severity": "error",
                        "message": f"Lexer error: {error_info.get('message', 'unknown')}",
                        "source": "ivy-lsp-lexer",
                    })
            except Exception as exc:
                logger.warning("Fallback scan failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "lexer", "error": str(exc)})

        # 3. Semantic diagnostics (orphaned RFC tags, untagged assertions)
        if layers is None or "semantic" in layers:
            try:
                model = await ctx.get_model()
                if model is not None:
                    from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

                    rfc_reqs = model.get_nodes_by_type(RfcRequirement)
                    annotations = [
                        n for n in model.get_nodes_by_type(RfcAnnotation)
                        if n.file == abs_path
                    ]
                    if rfc_reqs:
                        req_ids = {r.id for r in rfc_reqs}
                        for ann in annotations:
                            for tag in ann.tags:
                                if tag not in req_ids:
                                    all_diags.append({
                                        "line": ann.line + 1,
                                        "severity": "warning",
                                        "message": (
                                            f"Orphaned RFC tag: [{tag}] does not "
                                            "match any loaded requirement manifest"
                                        ),
                                        "source": "ivy-lsp-semantic",
                                    })

                    # Missing tags on assertions
                    lines = source.split("\n")
                    for m in _ASSERTION_RE.finditer(source):
                        line_no = source[:m.start()].count("\n")
                        line_text = lines[line_no] if line_no < len(lines) else ""
                        if not _BRACKET_TAG_RE.search(line_text):
                            all_diags.append({
                                "line": line_no + 1,
                                "severity": "hint",
                                "message": "Assertion without RFC bracket tag annotation",
                                "source": "ivy-lsp-semantic",
                            })
            except Exception as exc:
                logger.warning("Semantic diagnostics failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "semantic", "error": str(exc)})

        # 4. Coverage hints
        if layers is None or "coverage" in layers:
            try:
                graph = await ctx.get_req_graph()
                if graph is not None:
                    from ivy_lsp.features.coverage_hints import compute_coverage_hints

                    for hint in compute_coverage_hints(graph, abs_path):
                        all_diags.append({
                            "line": hint.get("line", 0),
                            "severity": hint.get("severity", "hint"),
                            "message": hint["message"],
                            "source": "ivy-lsp-coverage",
                            "code": hint.get("code"),
                        })
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
                        all_diags.append({
                            "line": 1,
                            "severity": "warning",
                            "message": (
                                "Test file has exports but no _finalize action. "
                                "Consider adding 'export action _finalize' for "
                                "end-of-test assertions."
                            ),
                            "source": "ivy-pattern",
                        })

                # Exported actions without monitors
                exports = set(re.findall(
                    r"^\s*export\s+action\s+([\w.]+)", source, re.MULTILINE,
                ))
                monitored = set(re.findall(
                    r"^\s*(?:before|after|around)\s+([\w.]+)", source, re.MULTILINE,
                ))
                for exp_action in exports:
                    if exp_action not in monitored and exp_action != "_finalize":
                        action_defined = bool(re.search(
                            rf"^\s*action\s+{re.escape(exp_action)}\s*",
                            source, re.MULTILINE,
                        ))
                        if action_defined:
                            match = re.search(
                                rf"^\s*export\s+action\s+{re.escape(exp_action)}",
                                source, re.MULTILINE,
                            )
                            line_num = source[:match.start()].count("\n") + 1 if match else 1
                            all_diags.append({
                                "line": line_num,
                                "severity": "hint",
                                "message": (
                                    f"Exported action '{exp_action}' has no "
                                    "before/after monitor in this file."
                                ),
                                "source": "ivy-pattern",
                            })
            except Exception as exc:
                logger.warning("Pattern diagnostics failed for %s: %s", relative_path, exc)
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
                d for d in all_diags
                if _sev_order.get(d.get("severity", "hint"), 0) >= min_rank
            ]

        # Build source-breakdown summary
        by_source: dict[str, int] = {}
        for d in all_diags:
            src = d.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        return json.dumps({
            "success": True,
            "file": relative_path,
            "diagnostics": all_diags,
            "diagnostic_count": len(all_diags),
            "by_source": by_source,
            "error_count": sum(1 for d in all_diags if d.get("severity") == "error"),
            "warning_count": sum(1 for d in all_diags if d.get("severity") == "warning"),
            "hint_count": sum(1 for d in all_diags if d.get("severity") == "hint"),
            "info_count": sum(1 for d in all_diags if d.get("severity") == "info"),
            "layer_errors": layer_errors,
            "partial": bool(layer_errors),
        })
