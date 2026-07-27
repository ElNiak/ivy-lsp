"""Pattern tools: ivy_patterns (unified pattern analysis, checking, and scaffolding)."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import error_response, inject_dispatch_key, safe_tool

logger = logging.getLogger(__name__)

_VALID_PATTERN_MODES: frozenset[str] = frozenset(
    {"analyze", "validate", "compare", "check", "scaffold"}
)


def register_pattern_tools(mcp: Any, ctx: Any) -> None:
    """Register pattern-related MCP tools."""

    async def _ivy_pattern_analysis(
        protocol: str,
        mode: str = "detect",
        pattern: str | None = None,
        reference_protocol: str | None = None,
    ) -> dict:
        """Analyze formal model patterns in a protocol specification."""
        from ivy_lsp.core.analysis.patterns import handle_pattern_analysis

        params: dict[str, Any] = {"protocol": protocol, "mode": mode}
        if pattern:
            params["pattern"] = pattern
        if reference_protocol:
            params["reference_protocol"] = reference_protocol
        return handle_pattern_analysis(ctx.root, params)

    async def _ivy_scaffold_check(protocol: str) -> dict:
        """Check which layers/patterns are present or missing in a protocol model."""
        # Canonical layers with detection heuristics
        _LAYERS = [
            ("types", "{p}_types.ivy", "type "),
            ("codec", "{p}_codec.ivy", "interpret "),
            ("frame", "{p}_frame.ivy", "variant "),
            ("packet", "{p}_packet.ivy", "type.*quic_packet"),
            ("connection", "{p}_connection.ivy", "relation conn"),
            ("transport", "{p}_transport.ivy", "action "),
            ("security", "{p}_security.ivy", "action "),
            ("application", "{p}_application.ivy", "action app_"),
            ("shim", "{p}_shim*.ivy", "<<< impl"),
            ("test_specs", "{p}_*_test_*.ivy", "export "),
            ("entities", None, "instance "),
            ("behavior", "{p}_*_behavior.ivy", "before "),
            ("recovery", "*recovery*.ivy", None),
            ("extensions", "*ext*.ivy", None),
        ]

        prot_dir = os.path.join(ctx.root, "protocol-testing", protocol)
        if not os.path.isdir(prot_dir):
            return error_response(
                f"Protocol directory not found: protocol-testing/{protocol}"
            )

        # Collect all .ivy files under this protocol
        prot_files = ctx.find_ivy_files(ctx.root)
        prot_files = [
            f for f in prot_files if f.startswith(f"protocol-testing/{protocol}/")
        ]

        layers_present = []
        layers_missing = []
        suggestions = []

        for layer_name, file_pattern, content_marker in _LAYERS:
            found = False
            matched_files = []

            if file_pattern:
                import fnmatch

                pat = file_pattern.replace("{p}", protocol.split("/")[-1])
                for f in prot_files:
                    basename = os.path.basename(f)
                    if fnmatch.fnmatch(basename, pat):
                        found = True
                        matched_files.append(f)

            if not found and content_marker:
                for f in prot_files:
                    abs_f = os.path.join(ctx.root, f)
                    try:
                        with open(abs_f, encoding="utf-8", errors="replace") as fh:
                            if content_marker in fh.read(4096):
                                found = True
                                matched_files.append(f)
                                break
                    except OSError as exc:
                        logger.warning("Skipping unreadable file %s: %s", f, exc)
                        continue

            if found:
                layers_present.append(
                    {
                        "layer": layer_name,
                        "files": matched_files[:3],
                    }
                )
            else:
                layers_missing.append(layer_name)
                suggestions.append(
                    {
                        "layer": layer_name,
                        "priority": (
                            "high"
                            if layer_name
                            in (
                                "types",
                                "frame",
                                "packet",
                                "connection",
                            )
                            else "medium"
                        ),
                        "suggestion": (
                            f"Add {layer_name} layer: create "
                            f"{protocol}_{layer_name}.ivy in "
                            f"protocol-testing/{protocol}/{protocol}_stack/"
                        ),
                    }
                )

        total = len(_LAYERS)
        present = len(layers_present)
        score = round(present / total * 100) if total else 0

        # Check for manifest (prot_files only has .ivy files, scan directory)
        has_manifest = any(
            f.endswith("_requirements.yaml") or f.endswith("_requirements.yml")
            for f in os.listdir(prot_dir)
            if os.path.isfile(os.path.join(prot_dir, f))
        )
        if not has_manifest:
            suggestions.append(
                {
                    "layer": "traceability",
                    "priority": "medium",
                    "suggestion": (
                        "No requirements manifest found. Use "
                        "ivy_extract_requirements(output='manifest') to create one from RFC text."
                    ),
                }
            )

        return {
            "protocol": protocol,
            "completeness_score": score,
            "total_layers": total,
            "present": present,
            "missing": len(layers_missing),
            "total_ivy_files": len(prot_files),
            "has_manifest": has_manifest,
            "layers_present": layers_present,
            "layers_missing": layers_missing,
            "suggestions": suggestions,
        }

    async def _pattern_scaffold_impl(
        protocol: str,
        pattern: str,
        wire_format: str,
        role_type: str,
        variant_names: list[str] | None,
        roles: list[str] | None,
        _ctx: Any = ctx,
    ) -> dict:
        from ivy_lsp.core.analysis.patterns import handle_pattern_scaffold

        params: dict[str, Any] = {
            "protocol": protocol,
            "pattern": pattern,
            "wire_format": wire_format,
            "role_type": role_type,
        }
        if variant_names:
            params["variant_names"] = variant_names
        if roles:
            params["roles"] = roles
        return handle_pattern_scaffold(_ctx.root, params)

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_patterns(
        protocol: str,
        mode: Literal[
            "analyze", "validate", "compare", "check", "scaffold"
        ] = "analyze",
        pattern: str | None = None,
        reference_protocol: str | None = None,
        wire_format: str = "binary",
        role_type: str = "asymmetric",
        variant_names: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> dict:
        """Detects, validates, compares, and scaffolds Ivy protocol patterns.

        Modes:
        - analyze: detect recurring patterns (serdes, variants, monitors, shims) -> {patterns: [{name, files[], instances}]}
        - validate: check cross-references between patterns -> {valid: bool, issues[]}
        - compare: diff two protocols (requires reference_protocol) -> {shared[], only_source[], only_reference[]}
        - check: completeness against 14-layer decomposition -> {present[], missing[], score: float 0-100}
        - scaffold: generate Ivy source from a pattern template -> {source: str, pattern, protocol}

        Use check first to identify missing layers, then scaffold to generate stubs.

        Args:
            protocol: Protocol name (e.g., "quic", "bgp", "minip").
            mode: Analysis mode (default: "analyze").
            pattern: Specific pattern to analyze or scaffold (e.g., "serdes",
                "variants"). Required for mode="scaffold".
            reference_protocol: Protocol to compare against. Required for
                mode="compare".
            wire_format: Wire format for scaffold serdes: "binary" (default)
                or "json". For shim pattern, use "udp" or "tcp".
            role_type: Role type for scaffold entity: "asymmetric" (default)
                or "symmetric".
            variant_names: Optional list of variant/message type names
                (scaffold mode).
            roles: Optional list of role names, e.g., ["client", "server"]
                (scaffold mode).
        """
        logger.debug(
            "[ivy_patterns] workspace=%s, args=%r",
            ctx.root,
            {
                "protocol": protocol,
                "mode": mode,
                "pattern": pattern,
                "reference_protocol": reference_protocol,
            },
        )
        _tc = ToolTraceContext(
            "ivy_patterns", {"protocol": protocol, "mode": mode, "pattern": pattern}
        )
        if mode not in _VALID_PATTERN_MODES:
            return _tc.finish(
                error_response(
                    f"Unknown mode '{mode}'. Valid: {sorted(_VALID_PATTERN_MODES)}"
                )
            )
        if mode == "scaffold":
            if not pattern:
                return _tc.finish(
                    error_response(
                        "scaffold mode requires 'pattern' parameter "
                        "(e.g., 'serdes', 'variants', 'shim')"
                    )
                )
            result = await _pattern_scaffold_impl(
                protocol, pattern, wire_format, role_type, variant_names, roles
            )
        elif mode == "check":
            result = await _ivy_scaffold_check(protocol)
        else:
            # _ivy_pattern_analysis expects "detect" as the internal label
            # for pattern detection; surface it as "analyze" in the result.
            effective_mode = "detect" if mode == "analyze" else mode
            result = await _ivy_pattern_analysis(
                protocol, effective_mode, pattern, reference_protocol
            )
        return _tc.finish(inject_dispatch_key(result, mode))
