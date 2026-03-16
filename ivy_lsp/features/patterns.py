"""Pattern analysis and scaffolding handlers for the Ivy LSP/MCP server.

Provides pure-function handlers for pattern detection, validation,
comparison, and template scaffolding.  Called by MCP tools registered
in ``mcp_server.py``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Maximum response size (2 MB)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _cap_list(response: dict, list_key: str) -> dict:
    """Truncate the main list if serialized size exceeds MAX_RESPONSE_BYTES."""
    encoded = json.dumps(response)
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return response
    items = response.get(list_key, [])
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        response[list_key] = items[:mid]
        if len(json.dumps(response)) <= MAX_RESPONSE_BYTES:
            lo = mid
        else:
            hi = mid - 1
    response[list_key] = items[:lo]
    if lo < len(items):
        response["truncated"] = True
        response["totalCount"] = len(items)
    return response


def handle_pattern_analysis(
    root: str,
    params: dict,
) -> dict:
    """Analyze patterns in a protocol model.

    Args:
        root: Workspace root directory.
        params: Dict with keys:
            - protocol (str): Protocol name (e.g., "quic", "bgp")
            - mode (str): "detect" | "validate" | "compare"
            - pattern (str, optional): Specific pattern to analyze
            - reference_protocol (str, optional): For compare mode

    Returns:
        Dict with detected patterns, validation issues, or comparison.
    """
    try:
        from ivy_lsp.analysis.pattern_library import (
            PatternCrossReferencer,
            PatternKind,
            analyze_protocol,
            compare_protocols,
        )
    except ImportError:
        return {"success": False, "message": "pattern_library not available"}

    protocol = params.get("protocol", "")
    mode = params.get("mode", "detect")
    pattern_filter = params.get("pattern")
    ref_protocol = params.get("reference_protocol")

    if not protocol:
        return {"success": False, "message": "protocol parameter required"}

    # Resolve protocol directory
    protocol_dir = _find_protocol_dir(root, protocol)
    if not protocol_dir:
        return {
            "success": False,
            "message": f"Protocol directory not found for '{protocol}'",
        }

    result = analyze_protocol(protocol_dir)

    if mode == "detect":
        detected = result.detected
        if pattern_filter:
            try:
                kind = PatternKind(pattern_filter)
                detected = [p for p in detected if p.kind == kind]
            except ValueError:
                return {
                    "success": False,
                    "message": f"Unknown pattern: {pattern_filter}",
                }

        return _cap_list(
            {
                "success": True,
                "protocol": protocol,
                "mode": "detect",
                "summary": result.summary,
                "patterns": [
                    {
                        "kind": p.kind.value,
                        "file": os.path.relpath(p.file, root),
                        "line": p.line,
                        "name": p.name,
                        "details": p.details,
                    }
                    for p in detected
                ],
            },
            "patterns",
        )

    elif mode == "validate":
        xref = PatternCrossReferencer(result)
        issues = xref.validate_all()
        result.issues = issues

        return {
            "success": True,
            "protocol": protocol,
            "mode": "validate",
            "summary": result.summary,
            "issues": [
                {
                    "severity": i.severity,
                    "pattern": i.pattern.value,
                    "message": i.message,
                    "file": os.path.relpath(i.file, root) if i.file else None,
                    "line": i.line,
                    "related": i.related,
                }
                for i in issues
            ],
            "issue_count": len(issues),
        }

    elif mode == "compare":
        if not ref_protocol:
            return {
                "success": False,
                "message": "reference_protocol required for compare mode",
            }

        ref_dir = _find_protocol_dir(root, ref_protocol)
        if not ref_dir:
            return {
                "success": False,
                "message": f"Reference protocol directory not found: '{ref_protocol}'",
            }

        ref_result = analyze_protocol(ref_dir)
        comparison = compare_protocols(result, ref_result)

        return {
            "success": True,
            "mode": "compare",
            **comparison,
        }

    else:
        return {"success": False, "message": f"Unknown mode: {mode}"}


def handle_pattern_scaffold(
    root: str,
    params: dict,
) -> dict:
    """Load and customize a pattern template.

    Args:
        root: Workspace root directory.
        params: Dict with keys:
            - protocol (str): Protocol name
            - pattern (str): Pattern kind (serdes, variants, etc.)
            - wire_format (str, optional): "binary" or "json" (for serdes)
            - role_type (str, optional): "asymmetric" or "symmetric" (for entity)
            - variant_names (list, optional): Variant names for variants pattern
            - roles (list, optional): Role names for entity pattern

    Returns:
        Dict with generated source code and target file paths.
    """
    protocol = params.get("protocol", "")
    pattern = params.get("pattern", "")
    wire_format = params.get("wire_format", "binary")
    role_type = params.get("role_type", "asymmetric")
    variant_names = params.get("variant_names", ["type_a", "type_b"])
    roles = params.get("roles", ["client", "server"])

    if not protocol or not pattern:
        return {"success": False, "message": "protocol and pattern required"}

    # Find patterns directory
    patterns_dir = _find_patterns_dir(root)
    if not patterns_dir:
        return {"success": False, "message": "patterns directory not found"}

    # Load template catalog
    catalog = _load_catalog(patterns_dir)
    if not catalog:
        return {"success": False, "message": "pattern_catalog.yaml not found"}

    pattern_info = catalog.get("patterns", {}).get(pattern)
    if not pattern_info:
        return {
            "success": False,
            "message": f"Unknown pattern: {pattern}",
            "available": list(catalog.get("patterns", {}).keys()),
        }

    # Determine template files to load
    templates = pattern_info.get("templates", {})
    files_to_generate: List[Dict[str, Any]] = []

    if pattern == "serdes":
        ser_key = f"{wire_format}_ser"
        deser_key = f"{wire_format}_deser"
        for key in [ser_key, deser_key]:
            tpl_path = templates.get(key)
            if tpl_path:
                content = _load_and_substitute(
                    os.path.join(patterns_dir, tpl_path),
                    protocol,
                    variant_names,
                    roles,
                )
                if content:
                    target = f"{protocol}/{protocol}_stack/{protocol}_{key.split('_', 1)[1]}.ivy"
                    files_to_generate.append(
                        {
                            "template": tpl_path,
                            "target": target,
                            "content": content,
                        }
                    )

    elif pattern == "variants":
        tpl_path = templates.get("frame")
        if tpl_path:
            content = _load_and_substitute(
                os.path.join(patterns_dir, tpl_path),
                protocol,
                variant_names,
                roles,
            )
            if content:
                files_to_generate.append(
                    {
                        "template": tpl_path,
                        "target": f"{protocol}/{protocol}_stack/{protocol}_message.ivy",
                        "content": content,
                    }
                )

    elif pattern == "monitors":
        for key in ["before_after", "finalize", "export_weight"]:
            tpl_path = templates.get(key)
            if tpl_path:
                content = _load_and_substitute(
                    os.path.join(patterns_dir, tpl_path),
                    protocol,
                    variant_names,
                    roles,
                )
                if content:
                    suffix = key.replace("_", "_")
                    files_to_generate.append(
                        {
                            "template": tpl_path,
                            "target": f"{protocol}/{protocol}_entities/{protocol}_{suffix}.ivy",
                            "content": content,
                        }
                    )

    elif pattern == "shim":
        transport = wire_format if wire_format in ("udp", "tcp") else "udp"
        tpl_path = templates.get(transport)
        if tpl_path:
            content = _load_and_substitute(
                os.path.join(patterns_dir, tpl_path),
                protocol,
                variant_names,
                roles,
            )
            if content:
                files_to_generate.append(
                    {
                        "template": tpl_path,
                        "target": f"{protocol}/{protocol}_shims/{protocol}_shim.ivy",
                        "content": content,
                    }
                )

    elif pattern == "module":
        tpl_path = templates.get("parameterized")
        if tpl_path:
            content = _load_and_substitute(
                os.path.join(patterns_dir, tpl_path),
                protocol,
                variant_names,
                roles,
            )
            if content:
                files_to_generate.append(
                    {
                        "template": tpl_path,
                        "target": f"{protocol}/{protocol}_stack/{protocol}_module.ivy",
                        "content": content,
                    }
                )

    elif pattern == "entity":
        key = role_type
        tpl_path = templates.get(key)
        if tpl_path:
            content = _load_and_substitute(
                os.path.join(patterns_dir, tpl_path),
                protocol,
                variant_names,
                roles,
            )
            if content:
                files_to_generate.append(
                    {
                        "template": tpl_path,
                        "target": f"{protocol}/{protocol}_stack/{protocol}_endpoint.ivy",
                        "content": content,
                    }
                )

    if not files_to_generate:
        return {
            "success": False,
            "message": f"No templates found for pattern '{pattern}' with options",
        }

    return {
        "success": True,
        "protocol": protocol,
        "pattern": pattern,
        "files": files_to_generate,
        "dependencies": pattern_info.get("depends_on", []),
        "layer": pattern_info.get("layer"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_protocol_dir(root: str, protocol: str) -> Optional[str]:
    """Find protocol directory under protocol-testing/."""
    candidates = [
        os.path.join(root, "protocol-testing", protocol),
        os.path.join(root, "protocol-testing", "apt", "apt_protocols", protocol),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _find_patterns_dir(root: str) -> Optional[str]:
    """Find the patterns template directory."""
    d = os.path.join(root, "protocol-testing", "patterns")
    return d if os.path.isdir(d) else None


def _load_catalog(patterns_dir: str) -> Optional[dict]:
    """Load pattern_catalog.yaml."""
    catalog_path = os.path.join(patterns_dir, "pattern_catalog.yaml")
    if not os.path.isfile(catalog_path):
        return None
    try:
        # Use a simple YAML-like parser to avoid requiring pyyaml
        # For production, this should use yaml.safe_load
        import yaml

        with open(catalog_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        # Fallback: try to parse enough for our needs
        logger.warning("PyYAML not available; catalog loading limited")
        return None
    except Exception:
        logger.warning("Failed to load pattern catalog", exc_info=True)
        return None


def _load_and_substitute(
    template_path: str,
    protocol: str,
    variant_names: List[str],
    roles: List[str],
) -> Optional[str]:
    """Load a template file and perform placeholder substitution."""
    if not os.path.isfile(template_path):
        return None
    try:
        with open(template_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    # Perform substitutions
    content = content.replace("{prot}", protocol)
    content = content.replace("{PROT}", protocol.upper())

    if roles:
        content = content.replace("{role}", roles[0])
        content = content.replace("{role_a}", roles[0])
        if len(roles) > 1:
            content = content.replace("{role_b}", roles[1])

    if variant_names:
        content = content.replace("{variant_name}", variant_names[0])
        # Replace variant list placeholders
        for i, vn in enumerate(variant_names):
            content = content.replace(f"{{variant_{i}}}", vn)

    return content
