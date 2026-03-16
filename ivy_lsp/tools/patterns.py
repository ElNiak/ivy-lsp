"""Pattern tools: ivy_patterns, ivy_pattern_scaffold.

Consolidated from the original three tools:
- ivy_pattern_analysis + ivy_scaffold_check -> ivy_patterns
- ivy_pattern_scaffold (kept as-is, it's a generator not an analyzer)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ivy_lsp.tools._helpers import error_response

logger = logging.getLogger(__name__)


def register_pattern_tools(mcp: Any, ctx: Any) -> None:
    """Register pattern-related MCP tools."""
    # ------------------------------------------------------------------
    # Private helpers (former standalone tool bodies)
    # ------------------------------------------------------------------

    async def _ivy_pattern_analysis(
        protocol: str,
        mode: str = "detect",
        pattern: str | None = None,
        reference_protocol: str | None = None,
    ) -> str:
        """Analyze formal model patterns in a protocol specification."""
        from ivy_lsp.features.patterns import handle_pattern_analysis

        params: dict[str, Any] = {"protocol": protocol, "mode": mode}
        if pattern:
            params["pattern"] = pattern
        if reference_protocol:
            params["reference_protocol"] = reference_protocol
        return json.dumps(handle_pattern_analysis(ctx.root, params))

    async def _ivy_scaffold_check(protocol: str) -> str:
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
            ("recovery", "{p}_recovery*.ivy", None),
            ("extensions", "{p}_extension*.ivy", None),
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

        # Check for manifest
        has_manifest = any(f.endswith("_requirements.yaml") for f in prot_files)
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

        return json.dumps(
            {
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
        )

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def ivy_patterns(
        protocol: str,
        mode: str = "analyze",
        pattern: str | None = None,
        reference_protocol: str | None = None,
    ) -> str:
        """Unified pattern analysis and scaffold completeness checking tool.

        Combines pattern detection/validation with layer completeness
        checking into a single tool with mode-based dispatch.

        Args:
            protocol: Protocol name (e.g., "quic", "bgp", "minip").
            mode: Analysis mode.
                - "analyze": Detect recurring patterns (serdes, variants,
                  monitors, shims, modules, entities) and validate
                  cross-references between them (default). Supports
                  sub-modes via the pattern parameter.
                - "validate": Check cross-references between patterns.
                  Same as "analyze" with mode="validate" passed through.
                - "compare": Diff two protocols. Requires
                  reference_protocol.
                - "check": Check which layers/patterns are present or
                  missing against the canonical 14-layer decomposition.
                  Returns a completeness score with suggestions.
            pattern: Optional specific pattern to analyze (e.g., "serdes",
                "variants"). Used by mode="analyze" and mode="validate".
            reference_protocol: Protocol to compare against. Required for
                mode="compare".
        """
        if mode == "check":
            return await _ivy_scaffold_check(protocol)
        else:
            # Validate mode and alias "analyze" -> "detect"
            _VALID_MODES = {"analyze", "detect", "validate", "compare", "check"}
            if mode not in _VALID_MODES:
                return error_response(
                    f"Unknown mode '{mode}'. Valid: {sorted(_VALID_MODES)}"
                )
            effective_mode = "detect" if mode == "analyze" else mode
            return await _ivy_pattern_analysis(
                protocol, effective_mode, pattern, reference_protocol
            )

    @mcp.tool()
    async def ivy_pattern_scaffold(
        protocol: str,
        pattern: str,
        wire_format: str = "binary",
        role_type: str = "asymmetric",
        variant_names: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> str:
        """Generate Ivy source from a pattern template.

        Loads a pattern template, performs placeholder substitution with the
        given protocol name and options, and returns the generated source code.

        Args:
            protocol: Protocol name for placeholder substitution.
            pattern: Pattern to scaffold: "serdes", "variants", "monitors",
                "shim", "module", or "entity".
            wire_format: Wire format for serdes: "binary" (default) or "json".
                For shim pattern, use "udp" or "tcp".
            role_type: Role type for entity: "asymmetric" (default) or "symmetric".
            variant_names: Optional list of variant/message type names.
            roles: Optional list of role names (e.g., ["client", "server"]).
        """
        from ivy_lsp.features.patterns import handle_pattern_scaffold

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
        return json.dumps(handle_pattern_scaffold(ctx.root, params))

    # --- Individual tool aliases (backward compatibility) ---

    @mcp.tool()
    async def ivy_pattern_analysis(
        protocol: str,
        mode: str = "detect",
        pattern: str | None = None,
        reference_protocol: str | None = None,
    ) -> str:
        """Analyze formal model patterns in a protocol specification."""
        return await _ivy_pattern_analysis(protocol, mode, pattern, reference_protocol)

    @mcp.tool()
    async def ivy_scaffold_check(protocol: str) -> str:
        """Check which layers/patterns are present or missing."""
        return await _ivy_scaffold_check(protocol)
