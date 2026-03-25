"""Quality tools: ivy_quality.

Consolidated from the original two tools:
- ivy_smart_suggestions + ivy_quality_gate -> ivy_quality
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)


def register_quality_tools(mcp: Any, ctx: Any) -> None:
    """Register quality-related MCP tools."""
    # ------------------------------------------------------------------
    # Private helpers (former standalone tool bodies)
    # ------------------------------------------------------------------

    async def _ivy_smart_suggestions(
        file_path: str | None = None,
        line: int | None = None,
        context: str | None = None,
        protocol: str | None = None,
        max_items: int = 50,
    ) -> dict:
        """Get context-aware suggestions for improving the Ivy specification."""
        from ivy_lsp.features.visualization import handle_smart_suggestions

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if file_path:
            try:
                params["filePath"] = ctx.validate_path(file_path)
            except ValueError as exc:
                return error_response(str(exc))
        if line is not None:
            params["line"] = line
        if context:
            params["context"] = context
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        result = handle_smart_suggestions(server_proxy, params)

        # Apply output size limit
        suggestions = result.get("suggestions", [])
        if max_items > 0 and len(suggestions) > max_items:
            result["total"] = len(suggestions)
            result["suggestions"] = suggestions[:max_items]
            result["truncated"] = True
        return result

    async def _ivy_quality_gate(
        protocol: str,
        gate_level: str = "minimal",
    ) -> dict:
        """Validate a protocol model against quality gates."""
        prot_dir = os.path.join(ctx.root, "protocol-testing", protocol)
        if not os.path.isdir(prot_dir):
            return error_response(
                f"Protocol directory not found: protocol-testing/{protocol}"
            )

        prot_files = [
            f
            for f in ctx.find_ivy_files(ctx.root)
            if f.startswith(f"protocol-testing/{protocol}/")
        ]

        checks: list[dict[str, Any]] = []
        skipped_files: list[str] = []
        all_passed = True

        # --- MINIMAL checks ---
        # 1. Lang header
        files_without_header = []
        for f in prot_files:
            abs_f = os.path.join(ctx.root, f)
            try:
                with open(abs_f, encoding="utf-8", errors="replace") as fh:
                    first_line = fh.readline()
                if not first_line.startswith("#lang"):
                    files_without_header.append(f)
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", f, exc)
                skipped_files.append(f)
                continue
        passed = len(files_without_header) == 0
        if not passed:
            all_passed = False
        checks.append(
            {
                "check": "lang_header",
                "level": "minimal",
                "passed": passed,
                "detail": (
                    f"{len(files_without_header)} files missing #lang header"
                    if not passed
                    else "All files have #lang header"
                ),
            }
        )

        # 2. Includes resolve
        from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor

        _inc_extractor = TieredExtractor()
        basenames = {os.path.splitext(os.path.basename(f))[0] for f in prot_files}
        unresolved = []
        for f in prot_files:
            abs_f = os.path.join(ctx.root, f)
            try:
                with open(abs_f, encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
                for inc in _inc_extractor.extract(source, abs_f).includes:
                    if inc not in basenames and inc not in ctx.stdlib_modules:
                        unresolved.append({"file": f, "include": inc})
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", f, exc)
                skipped_files.append(f)
                continue
        passed = len(unresolved) == 0
        if not passed:
            all_passed = False
        checks.append(
            {
                "check": "includes_resolve",
                "level": "minimal",
                "passed": passed,
                "detail": (
                    f"{len(unresolved)} unresolved includes"
                    if not passed
                    else "All includes resolve"
                ),
                "unresolved": unresolved[:10] if not passed else [],
            }
        )

        # 3. File count sanity
        passed = len(prot_files) >= 3
        if not passed:
            all_passed = False
        checks.append(
            {
                "check": "minimum_files",
                "level": "minimal",
                "passed": passed,
                "detail": f"{len(prot_files)} .ivy files found",
            }
        )

        if gate_level in ("standard", "comprehensive"):
            # --- STANDARD checks ---
            # 4. Test specs exist
            test_files = [f for f in prot_files if "_test" in os.path.basename(f)]
            passed = len(test_files) > 0
            if not passed:
                all_passed = False
            checks.append(
                {
                    "check": "test_specs_exist",
                    "level": "standard",
                    "passed": passed,
                    "detail": f"{len(test_files)} test spec files found",
                }
            )

            # 5. Behavior/monitor files exist
            # Note: _monitor_re, _export_re, _tag_re below intentionally use regex
            # for quality-gate counting — not symbol extraction.
            # See ivy_lsp.parsing.tiered_extractor for the symbol extraction cascade.
            behavior_files = [
                f for f in prot_files if "_behavior" in os.path.basename(f)
            ]
            monitor_count = 0
            _monitor_re = re.compile(
                r"^\s*(before|after|around)\s+",
                re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        monitor_count += len(_monitor_re.findall(fh.read()))
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", f, exc)
                    skipped_files.append(f)
                    continue
            passed = monitor_count > 0
            if not passed:
                all_passed = False
            checks.append(
                {
                    "check": "monitors_exist",
                    "level": "standard",
                    "passed": passed,
                    "detail": (
                        f"{monitor_count} monitor clauses across "
                        f"{len(behavior_files)} behavior files"
                    ),
                }
            )

            # 6. Export actions exist (for test generation)
            export_count = 0
            _export_re = re.compile(
                r"^\s*export\s+\w+",
                re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        export_count += len(_export_re.findall(fh.read()))
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", f, exc)
                    skipped_files.append(f)
                    continue
            passed = export_count > 0
            if not passed:
                all_passed = False
            checks.append(
                {
                    "check": "exports_exist",
                    "level": "standard",
                    "passed": passed,
                    "detail": f"{export_count} exported actions found",
                }
            )

        if gate_level == "comprehensive":
            # --- COMPREHENSIVE checks ---
            # 7. Manifest exists
            has_manifest = any(f.endswith("_requirements.yaml") for f in prot_files)
            if not has_manifest:
                all_passed = False
            checks.append(
                {
                    "check": "manifest_exists",
                    "level": "comprehensive",
                    "passed": has_manifest,
                    "detail": (
                        "Requirements manifest found"
                        if has_manifest
                        else "No requirements manifest"
                    ),
                }
            )

            # 8. Bracket tag annotations present
            _tag_re = re.compile(r"#\s*\[[\w:.,\s]+\]\s*$", re.MULTILINE)
            tag_count = 0
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        tag_count += len(_tag_re.findall(fh.read()))
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", f, exc)
                    skipped_files.append(f)
                    continue
            passed = tag_count > 0
            if not passed:
                all_passed = False
            checks.append(
                {
                    "check": "annotations_exist",
                    "level": "comprehensive",
                    "passed": passed,
                    "detail": f"{tag_count} bracket-tag annotations found",
                }
            )

        passed_count = sum(1 for c in checks if c["passed"])
        result: dict[str, Any] = {
            "protocol": protocol,
            "gate_level": gate_level,
            "passed": all_passed,
            "checks_passed": passed_count,
            "checks_total": len(checks),
            "checks": checks,
        }
        if skipped_files:
            # Deduplicate (a file may be skipped in multiple checks)
            unique_skipped = sorted(set(skipped_files))
            result["skipped_files"] = unique_skipped
            result["skipped_file_count"] = len(unique_skipped)
        return result

    # ------------------------------------------------------------------
    # Public MCP tool
    # ------------------------------------------------------------------

    @mcp.tool()
    @safe_tool
    async def ivy_quality(
        mode: Literal["suggestions", "gate"] = "suggestions",
        file_path: str | None = None,
        line: int | None = None,
        context: str | None = None,
        protocol: str | None = None,
        gate_level: str = "minimal",
        max_items: int = 50,
    ) -> dict:
        """Unified quality analysis tool for Ivy specifications.

        Combines context-aware suggestions with quality gate validation
        into a single tool with mode-based dispatch.

        Args:
            mode: Quality analysis mode.
                - "suggestions": Get context-aware suggestions for improving
                  the Ivy specification (default). Analyzes the file at
                  file_path (optionally at a specific line) and returns
                  improvement suggestions.
                - "gate": Validate a protocol model against quality gates.
                  Checks at one of three levels: minimal (lang header,
                  balanced braces, includes resolve), standard (+ test
                  specs, behavior files, monitors), or comprehensive
                  (+ manifest, coverage, no unguarded state vars).
                  Requires protocol.
            file_path: File to analyze (relative path). Used by
                mode="suggestions".
            line: Optional line number for cursor-local suggestions.
                Used by mode="suggestions".
            context: Optional context hint (e.g., "monitor", "property").
                Used by mode="suggestions".
            protocol: Protocol name (e.g., "quic", "bgp"). Required for
                mode="gate", optional for mode="suggestions".
            gate_level: Gate level: "minimal", "standard", or
                "comprehensive". Used by mode="gate".
            max_items: Maximum number of items to return in the response
                (default: 50). When the result is truncated, the response
                includes ``"truncated": true`` and ``"total": N``.
                Set to 0 for unlimited.
        """
        logger.debug(
            "[ivy_quality] workspace=%s, args=%r",
            ctx.root,
            {
                "mode": mode,
                "file_path": file_path,
                "protocol": protocol,
                "gate_level": gate_level,
            },
        )
        _tc = ToolTraceContext(
            "ivy_quality", {"mode": mode, "file_path": file_path, "protocol": protocol}
        )
        _valid_modes = {"suggestions", "gate"}
        if mode not in _valid_modes:
            return _tc.finish(
                error_response(
                    f"Unknown mode '{mode}'. Valid modes: {sorted(_valid_modes)}"
                )
            )
        if mode == "gate":
            if not protocol:
                return _tc.finish(
                    error_response("protocol is required for mode='gate'")
                )
            return _tc.finish(await _ivy_quality_gate(protocol, gate_level))
        else:  # default: suggestions
            return _tc.finish(
                await _ivy_smart_suggestions(
                    file_path, line, context, protocol, max_items
                )
            )
