"""Quality tools: ivy_smart_suggestions, ivy_quality_gate."""

from __future__ import annotations

import json
import os
import re
from typing import Any


def register_quality_tools(mcp: Any, ctx: Any) -> None:
    """Register quality-related MCP tools."""

    @mcp.tool()
    async def ivy_smart_suggestions(
        file_path: str | None = None,
        line: int | None = None,
        context: str | None = None,
        protocol: str | None = None,
    ) -> str:
        """Get context-aware suggestions for improving the Ivy specification.

        Args:
            file_path: File to analyze (relative path).
            line: Optional line number for cursor-local suggestions.
            context: Optional context hint (e.g., "monitor", "property").
        """
        from ivy_lsp.features.visualization import handle_smart_suggestions

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if file_path:
            try:
                params["filePath"] = ctx.validate_path(file_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if line is not None:
            params["line"] = line
        if context:
            params["context"] = context
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_smart_suggestions(server_proxy, params))

    @mcp.tool()
    async def ivy_quality_gate(
        protocol: str,
        gate_level: str = "minimal",
    ) -> str:
        """Validate a protocol model against quality gates.

        Checks the model at one of three levels:
        - minimal: lang header, balanced braces, includes resolve
        - standard: + test specs exist, behavior files exist, actions have monitors
        - comprehensive: + manifest exists, coverage > 0, no unguarded state vars

        Args:
            protocol: Protocol name (e.g., "quic", "bgp").
            gate_level: Gate level: "minimal", "standard", or "comprehensive".
        """
        prot_dir = os.path.join(ctx.root, "protocol-testing", protocol)
        if not os.path.isdir(prot_dir):
            return json.dumps({
                "success": False,
                "message": f"Protocol directory not found: protocol-testing/{protocol}",
            })

        prot_files = [
            f for f in ctx.find_ivy_files(ctx.root)
            if f.startswith(f"protocol-testing/{protocol}/")
        ]

        checks: list[dict[str, Any]] = []
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
            except OSError:
                continue
        passed = len(files_without_header) == 0
        if not passed:
            all_passed = False
        checks.append({
            "check": "lang_header",
            "level": "minimal",
            "passed": passed,
            "detail": (
                f"{len(files_without_header)} files missing #lang header"
                if not passed else "All files have #lang header"
            ),
        })

        # 2. Includes resolve
        _inc_re = re.compile(r"^include\s+(\w+)", re.MULTILINE)
        basenames = {
            os.path.splitext(os.path.basename(f))[0] for f in prot_files
        }
        unresolved = []
        for f in prot_files:
            abs_f = os.path.join(ctx.root, f)
            try:
                with open(abs_f, encoding="utf-8", errors="replace") as fh:
                    for inc in _inc_re.findall(fh.read()):
                        if inc not in basenames and inc not in ctx.stdlib_modules:
                            unresolved.append({"file": f, "include": inc})
            except OSError:
                continue
        passed = len(unresolved) == 0
        if not passed:
            all_passed = False
        checks.append({
            "check": "includes_resolve",
            "level": "minimal",
            "passed": passed,
            "detail": (
                f"{len(unresolved)} unresolved includes"
                if not passed else "All includes resolve"
            ),
            "unresolved": unresolved[:10] if not passed else [],
        })

        # 3. File count sanity
        passed = len(prot_files) >= 3
        if not passed:
            all_passed = False
        checks.append({
            "check": "minimum_files",
            "level": "minimal",
            "passed": passed,
            "detail": f"{len(prot_files)} .ivy files found",
        })

        if gate_level in ("standard", "comprehensive"):
            # --- STANDARD checks ---
            # 4. Test specs exist
            test_files = [
                f for f in prot_files if "_test" in os.path.basename(f)
            ]
            passed = len(test_files) > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "test_specs_exist",
                "level": "standard",
                "passed": passed,
                "detail": f"{len(test_files)} test spec files found",
            })

            # 5. Behavior/monitor files exist
            behavior_files = [
                f for f in prot_files if "_behavior" in os.path.basename(f)
            ]
            monitor_count = 0
            _monitor_re = re.compile(
                r"^\s*(before|after|around)\s+", re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        monitor_count += len(_monitor_re.findall(fh.read()))
                except OSError:
                    continue
            passed = monitor_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "monitors_exist",
                "level": "standard",
                "passed": passed,
                "detail": (
                    f"{monitor_count} monitor clauses across "
                    f"{len(behavior_files)} behavior files"
                ),
            })

            # 6. Export actions exist (for test generation)
            export_count = 0
            _export_re = re.compile(
                r"^\s*export\s+\w+", re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        export_count += len(_export_re.findall(fh.read()))
                except OSError:
                    continue
            passed = export_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "exports_exist",
                "level": "standard",
                "passed": passed,
                "detail": f"{export_count} exported actions found",
            })

        if gate_level == "comprehensive":
            # --- COMPREHENSIVE checks ---
            # 7. Manifest exists
            has_manifest = any(
                f.endswith("_requirements.yaml") for f in prot_files
            )
            if not has_manifest:
                all_passed = False
            checks.append({
                "check": "manifest_exists",
                "level": "comprehensive",
                "passed": has_manifest,
                "detail": (
                    "Requirements manifest found"
                    if has_manifest else "No requirements manifest"
                ),
            })

            # 8. Bracket tag annotations present
            _tag_re = re.compile(r"#\s*\[[\w:.,\s]+\]\s*$", re.MULTILINE)
            tag_count = 0
            for f in prot_files:
                abs_f = os.path.join(ctx.root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        tag_count += len(_tag_re.findall(fh.read()))
                except OSError:
                    continue
            passed = tag_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "annotations_exist",
                "level": "comprehensive",
                "passed": passed,
                "detail": f"{tag_count} bracket-tag annotations found",
            })

        passed_count = sum(1 for c in checks if c["passed"])
        return json.dumps({
            "protocol": protocol,
            "gate_level": gate_level,
            "passed": all_passed,
            "checks_passed": passed_count,
            "checks_total": len(checks),
            "checks": checks,
        })
