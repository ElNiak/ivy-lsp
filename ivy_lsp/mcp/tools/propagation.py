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

    variant_result = _find_variant_type(type_name, result, protocol_dir)
    if variant_result is not None:
        return variant_result

    return {
        "type_name": type_name,
        "error": f"type '{type_name}' not found in {protocol_dir}",
    }


# ---------------------------------------------------------------------------
# Variant-type helpers
# ---------------------------------------------------------------------------

# Variant struct body: ``variant this of <parent> = struct { field : type, ... }``
_VARIANT_STRUCT_RE = re.compile(
    r"variant\s+this\s+of\s+(\w+)\s*=\s*struct\s*\{([^}]+)\}",
    re.DOTALL,
)

# Nearest enclosing ``object <name> = {`` before a given position.
_ENCLOSING_OBJECT_RE = re.compile(r"object\s+(\w+)\s*=\s*\{", re.MULTILINE)

# Tag dispatch inside C++ open_tag(): ``if (tag == N) { ... frame_type = 0xNN``
_OPEN_TAG_ENTRY_RE = re.compile(
    r"tag\s*==\s*(\d+)\s*\).*?frame_type\s*=\s*(0x[0-9a-fA-F]+)",
    re.DOTALL,
)


def _parse_variant_members(
    source: str,
    parent: str,
) -> List[Dict[str, Any]]:
    """Extract variant member name + fields from Ivy source.

    For each ``variant this of <parent> = struct { ... }`` match, walks
    backwards to find the nearest enclosing ``object <name> = {`` to
    determine the member name.
    """
    members: List[Dict[str, Any]] = []
    for m in _VARIANT_STRUCT_RE.finditer(source):
        if m.group(1) != parent:
            continue

        fields_raw = m.group(2)
        fields: List[Dict[str, str]] = []
        for f in fields_raw.split(","):
            f = f.strip()
            if ":" in f:
                fname, ftype = f.split(":", 1)
                fields.append({"name": fname.strip(), "type": ftype.strip()})

        prefix = source[: m.start()]
        obj_name: Optional[str] = None
        for obj_m in _ENCLOSING_OBJECT_RE.finditer(prefix):
            obj_name = obj_m.group(1)
        if obj_name is None or obj_name == parent:
            continue

        members.append({"name": obj_name, "fields": fields})
    return members


def _extract_tag_map(ser_source: str) -> Dict[int, str]:
    """Parse ``open_tag()`` in a serializer's C++ impl block.

    Returns a mapping from integer tag to hex wire-type string
    (e.g. ``{0: "0x01", 1: "0x02", 2: "0x03"}``).
    """
    from ivy_lsp.core.analysis.impl_block_parser import analyze_impl_blocks

    impl = analyze_impl_blocks(ser_source)
    cpp = " ".join(b.content for b in impl.impl_blocks)

    tag_map: Dict[int, str] = {}
    for m in _OPEN_TAG_ENTRY_RE.finditer(cpp):
        tag = int(m.group(1))
        wire = m.group(2)
        tag_map[tag] = wire
    return tag_map


def _find_serializer_source(
    type_name: str,
    detected: List[Any],
    protocol_dir: str,
) -> Optional[str]:
    """Locate the serializer file for the packet type that contains *type_name*.

    Strategy:
    1. Find struct types whose fields reference ``<type_name>.arr``
       (e.g. ``ping_packet`` has ``payload : frame.arr``).
    2. Find a SERDES *instance* whose ``message_type`` matches that struct.
    3. Read the file containing the serializer class (matched by ``ser_name``).
    """
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    containing_structs: List[str] = []
    for pat in detected:
        if (
            pat.kind == PatternKind.VARIANTS
            and pat.details.get("type") == "struct_object"
        ):
            for field in pat.details.get("fields", []):
                if field.get("type", "").startswith(f"{type_name}."):
                    containing_structs.append(pat.name)
                    break

    ser_name: Optional[str] = None
    for pat in detected:
        if pat.kind == PatternKind.SERDES and pat.details.get("type") == "instance":
            if pat.details.get("message_type") in containing_structs:
                ser_name = pat.details.get("ser_name")
                break

    if ser_name is None:
        return None

    for pat in detected:
        if (
            pat.kind == PatternKind.SERDES
            and pat.details.get("type") == "serializer"
            and pat.name == ser_name
        ):
            try:
                with open(pat.file, encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return None

    return None


def _find_variant_type(
    type_name: str,
    analysis_result: Any,
    protocol_dir: str,
) -> Optional[Dict[str, Any]]:
    """Build a variant-type result dict if *type_name* is a variant parent."""
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    variant_pats = [
        p
        for p in analysis_result.detected
        if p.kind == PatternKind.VARIANTS
        and p.details.get("type") == "variant"
        and p.details.get("parent") == type_name
    ]
    if not variant_pats:
        return None

    first = variant_pats[0]
    try:
        with open(first.file, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return None

    members = _parse_variant_members(source, type_name)
    if not members:
        return None

    ser_source = _find_serializer_source(
        type_name, analysis_result.detected, protocol_dir
    )
    tag_map: Dict[int, str] = {}
    if ser_source is not None:
        tag_map = _extract_tag_map(ser_source)

    enriched: List[Dict[str, Any]] = []
    for idx, mem in enumerate(members):
        enriched.append(
            {
                "name": mem["name"],
                "tag": idx,
                "wire_type": tag_map.get(idx, ""),
                "fields": mem["fields"],
            }
        )

    rel_file = os.path.relpath(first.file, protocol_dir)
    line = first.line

    return {
        "type_name": type_name,
        "kind": "variant",
        "file": rel_file,
        "line": line,
        "members": enriched,
    }


def serdes_correlation_impl(
    type_name: str,
    protocol_dir: str,
) -> Dict[str, Any]:
    """Return the serializer/deserializer files correlated with *type_name*.

    Walks all SERDES patterns in *protocol_dir* to find every instance whose
    ``message_type`` matches *type_name*, then resolves the corresponding
    serializer and deserializer class patterns by name.

    Args:
        type_name: Unqualified Ivy message type name (e.g. ``"ping_packet"``).
        protocol_dir: Absolute path to the protocol directory to scan.

    Returns:
        Dict with keys ``type_name`` and ``correlations``.  Each entry in
        ``correlations`` has ``serializer``, ``deserializer``, and ``instance``
        sub-dicts.  Returns an empty ``correlations`` list when *type_name* is
        not found.
    """
    from ivy_lsp.core.analysis.pattern_library import PatternKind, analyze_protocol

    result = analyze_protocol(protocol_dir)

    class_index: Dict[str, Any] = {}
    for pat in result.detected:
        if pat.kind == PatternKind.SERDES and pat.details.get("type") in (
            "serializer",
            "deserializer",
        ):
            class_index[pat.name] = pat

    correlations: List[Dict[str, Any]] = []
    for pat in result.detected:
        if pat.kind != PatternKind.SERDES:
            continue
        if pat.details.get("type") != "instance":
            continue
        if pat.details.get("message_type") != type_name:
            continue

        ser_name = pat.details.get("ser_name")
        deser_name = pat.details.get("deser_name")

        ser_pat = class_index.get(ser_name)
        deser_pat = class_index.get(deser_name)

        def _class_info(p: Any) -> Dict[str, Any]:
            rel = os.path.relpath(p.file, protocol_dir)
            states = p.details.get("states", [])
            return {
                "file": rel,
                "class": p.name,
                "base": p.details.get("base_class", ""),
                "states": len(states),
            }

        correlations.append(
            {
                "serializer": _class_info(ser_pat) if ser_pat is not None else None,
                "deserializer": (
                    _class_info(deser_pat) if deser_pat is not None else None
                ),
                "instance": {
                    "name": pat.name,
                    "file": os.path.relpath(pat.file, protocol_dir),
                    "line": pat.line,
                },
            }
        )

    return {"type_name": type_name, "correlations": correlations}


def register_propagation_tools(mcp: Any, ctx: Any) -> None:
    """Register propagation analysis MCP tools."""
    pass  # Tools added in subsequent tasks
