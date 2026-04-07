"""Propagation analysis tools for Ivy type change impact."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def find_variants_impl(
    type_name: str,
    protocol_dir: str,
) -> Dict[str, Any]:
    """Return the structure of an Ivy type (struct fields or variant members).

    Uses ``analyze_protocol`` from the pattern library to scan all ``.ivy``
    files under *protocol_dir*, then filters for VARIANTS patterns whose
    name matches *type_name*.

    Args:
        type_name: Unqualified Ivy object name (e.g. ``"ping_packet"``).
        protocol_dir: Absolute path to the protocol directory to scan.

    Returns:
        Dict with keys ``type_name``, ``kind``, ``file``, ``line``, and
        ``fields`` (for structs).  Returns an error dict when the type is
        not found.
    """
    from ivy_lsp.core.analysis.pattern_library import (
        INSTANCE_RE,
        PatternKind,
        analyze_protocol,
    )

    result = analyze_protocol(protocol_dir)

    for pat in result.detected:
        if pat.kind != PatternKind.VARIANTS:
            continue
        if pat.details.get("type") != "struct_object":
            continue
        if pat.name != type_name:
            continue

        raw_fields: List[Dict[str, str]] = pat.details.get("fields", [])

        source: Optional[str] = None
        try:
            with open(pat.file, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            pass

        array_types: set[str] = set()
        if source is not None:
            for m in INSTANCE_RE.finditer(source):
                inst_name = m.group(1)
                module_name = m.group(2)
                if module_name == "array":
                    array_types.add(f"{type_name}.{inst_name}")

        enriched: List[Dict[str, Any]] = []
        for f in raw_fields:
            fname = f.get("name", "")
            ftype = f.get("type", "")
            is_array = ftype.endswith(".arr") or f"{type_name}.{fname}" in array_types
            enriched.append(
                {
                    "name": fname,
                    "type": ftype,
                    "is_array": is_array,
                }
            )

        rel_file = os.path.relpath(pat.file, protocol_dir)

        return {
            "type_name": type_name,
            "kind": "struct",
            "file": rel_file,
            "line": pat.line,
            "fields": enriched,
        }

    return {
        "type_name": type_name,
        "error": f"type '{type_name}' not found in {protocol_dir}",
    }


def register_propagation_tools(mcp: Any, ctx: Any) -> None:
    """Register propagation analysis MCP tools."""
    pass  # Tools added in subsequent tasks
