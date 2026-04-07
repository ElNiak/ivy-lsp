"""Propagation analysis tools for Ivy type change impact."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _build_serdes_class_index(detected: List[Any]) -> Dict[str, Any]:
    """Build a name→pattern index of serializer/deserializer class patterns."""
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    return {
        pat.name: pat
        for pat in detected
        if pat.kind == PatternKind.SERDES
        and pat.details.get("type") in ("serializer", "deserializer")
    }


def _get_analysis(protocol_dir: str, _analysis: Any = None) -> Any:
    """Return *_analysis* if provided, otherwise run ``analyze_protocol``."""
    if _analysis is not None:
        return _analysis
    from ivy_lsp.core.analysis.pattern_library import analyze_protocol

    return analyze_protocol(protocol_dir)


def _find_containing_structs(type_name: str, detected: List[Any]) -> List[str]:
    """Return names of struct objects that have a field referencing *type_name*."""
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    structs: List[str] = []
    for pat in detected:
        if (
            pat.kind == PatternKind.VARIANTS
            and pat.details.get("type") == "struct_object"
        ):
            for field in pat.details.get("fields", []):
                if field.get("type", "").startswith(f"{type_name}."):
                    structs.append(pat.name)
                    break
    return structs


def find_variants_impl(
    type_name: str,
    protocol_dir: str,
    _analysis: Any = None,
) -> Dict[str, Any]:
    """Return the structure of an Ivy type (struct fields or variant members).

    Args:
        type_name: Unqualified Ivy object name (e.g. ``"ping_packet"``).
        protocol_dir: Absolute path to the protocol directory to scan.
        _analysis: Pre-computed ``analyze_protocol`` result (avoids re-scan).
    """
    from ivy_lsp.core.analysis.pattern_library import INSTANCE_RE, PatternKind

    result = _get_analysis(protocol_dir, _analysis)

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

    containing_structs = _find_containing_structs(type_name, detected)

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


def change_impact_impl(
    type_name: str,
    change_type: str,
    protocol_dir: str,
) -> Dict[str, Any]:
    """Categorize protocol files by impact of a change to *type_name*.

    Splits every ``.ivy`` file in *protocol_dir* into three buckets:

    - **auto_propagate**: the type definition file and its serializer /
      deserializer files (mechanical edits).
    - **manual_review**: files that transitively depend on any
      auto_propagate file via Ivy ``include`` chains.
    - **unaffected**: all remaining protocol files.

    Args:
        type_name: Unqualified Ivy type name (e.g. ``"ping_packet"``).
        change_type: Kind of change (``"add_field"``, ``"add_variant"``, etc.).
        protocol_dir: Absolute path to the protocol directory.

    Returns:
        Dict with keys ``type_name``, ``change_type``, ``auto_propagate``,
        ``manual_review``, and ``unaffected``.
    """
    from ivy_lsp.core.analysis.pattern_library import PatternKind, detect_all_patterns

    analysis = _get_analysis(protocol_dir)
    variant_info = find_variants_impl(type_name, protocol_dir, _analysis=analysis)

    type_def_file: Optional[str] = variant_info.get("file")
    if type_def_file is None or "error" in variant_info:
        return {
            "type_name": type_name,
            "change_type": change_type,
            "error": f"type '{type_name}' not found in {protocol_dir}",
        }

    auto_entries: List[Dict[str, str]] = [
        {
            "file": type_def_file,
            "category": "type_definition",
            "edit": (
                "add_field_to_struct"
                if change_type == "add_field"
                else "add_variant_case"
            ),
        }
    ]

    serdes = serdes_correlation_impl(type_name, protocol_dir, _analysis=analysis)
    if serdes["correlations"]:
        for corr in serdes["correlations"]:
            if corr.get("serializer"):
                auto_entries.append(
                    {
                        "file": corr["serializer"]["file"],
                        "category": "serializer",
                        "edit": "add_state_and_set_case",
                    }
                )
            if corr.get("deserializer"):
                auto_entries.append(
                    {
                        "file": corr["deserializer"]["file"],
                        "category": "deserializer",
                        "edit": "add_state_and_get_case",
                    }
                )
    elif variant_info.get("kind") == "variant":
        ser_source = _find_serializer_source(type_name, analysis.detected, protocol_dir)
        if ser_source is not None:
            _add_ser_deser_from_containing_struct(
                type_name, analysis.detected, protocol_dir, auto_entries
            )

    auto_stems = set()
    for entry in auto_entries:
        stem = os.path.splitext(os.path.basename(entry["file"]))[0]
        auto_stems.add(stem)

    # Build include graph from already-computed INCLUDE_CHAIN patterns
    # (avoids re-reading every .ivy file from disk)
    file_includes: Dict[str, List[str]] = {}
    all_ivy_files: List[str] = []
    for pat in analysis.detected:
        if pat.kind == PatternKind.INCLUDE_CHAIN:
            all_ivy_files.append(pat.file)
            stem = os.path.splitext(os.path.basename(pat.file))[0]
            raw_includes = pat.details.get("includes", [])
            file_includes[stem] = [
                inc["name"] if isinstance(inc, dict) else inc for inc in raw_includes
            ]

    auto_rel_set = {e["file"] for e in auto_entries}

    def _depends_on_auto(stem: str, visited: Optional[set] = None) -> bool:
        if visited is None:
            visited = set()
        if stem in visited:
            return False
        visited.add(stem)
        for inc in file_includes.get(stem, []):
            if inc in auto_stems:
                return True
            if _depends_on_auto(inc, visited):
                return True
        return False

    manual_review: List[Dict[str, str]] = []
    unaffected: List[Dict[str, str]] = []

    for full_path in all_ivy_files:
        rel = os.path.relpath(full_path, protocol_dir)
        if rel in auto_rel_set:
            continue

        stem = os.path.splitext(os.path.basename(full_path))[0]
        if _depends_on_auto(stem):
            try:
                with open(full_path, encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                source = ""
            patterns = detect_all_patterns(source, full_path)
            category = _categorize_file(patterns, rel)
            reason = f"transitively includes {type_name} via include chain"
            manual_review.append({"file": rel, "category": category, "reason": reason})
        else:
            unaffected.append({"file": rel})

    return {
        "type_name": type_name,
        "change_type": change_type,
        "auto_propagate": auto_entries,
        "manual_review": manual_review,
        "unaffected": unaffected,
    }


def _add_ser_deser_from_containing_struct(
    type_name: str,
    detected: List[Any],
    protocol_dir: str,
    auto_entries: List[Dict[str, str]],
) -> None:
    """For variant types, find ser/deser via the parent struct's SERDES instance."""
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    containing_structs = _find_containing_structs(type_name, detected)

    class_index = _build_serdes_class_index(detected)

    for pat in detected:
        if pat.kind != PatternKind.SERDES or pat.details.get("type") != "instance":
            continue
        if pat.details.get("message_type") not in containing_structs:
            continue

        ser_name = pat.details.get("ser_name")
        deser_name = pat.details.get("deser_name")

        ser_pat = class_index.get(ser_name)
        if ser_pat is not None:
            auto_entries.append(
                {
                    "file": os.path.relpath(ser_pat.file, protocol_dir),
                    "category": "serializer",
                    "edit": "add_state_and_set_case",
                }
            )
        deser_pat = class_index.get(deser_name)
        if deser_pat is not None:
            auto_entries.append(
                {
                    "file": os.path.relpath(deser_pat.file, protocol_dir),
                    "category": "deserializer",
                    "edit": "add_state_and_get_case",
                }
            )
        break


def _categorize_file(patterns: List[Any], rel_path: str) -> str:
    """Determine a human-readable category from detected patterns."""
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    kinds = {p.kind for p in patterns}
    if PatternKind.SHIM in kinds:
        return "shim"
    if PatternKind.ENTITY in kinds:
        return "entity"
    if PatternKind.MONITORS in kinds:
        return "behavior"
    if "test" in rel_path:
        return "test"
    return "other"


def serdes_correlation_impl(
    type_name: str,
    protocol_dir: str,
    _analysis: Any = None,
) -> Dict[str, Any]:
    """Return the serializer/deserializer files correlated with *type_name*.

    Args:
        type_name: Unqualified Ivy message type name (e.g. ``"ping_packet"``).
        protocol_dir: Absolute path to the protocol directory to scan.
        _analysis: Pre-computed ``analyze_protocol`` result (avoids re-scan).
    """
    from ivy_lsp.core.analysis.pattern_library import PatternKind

    result = _get_analysis(protocol_dir, _analysis)

    class_index = _build_serdes_class_index(result.detected)

    def _class_info(p: Any) -> Dict[str, Any]:
        rel = os.path.relpath(p.file, protocol_dir)
        states = p.details.get("states", [])
        return {
            "file": rel,
            "class": p.name,
            "base": p.details.get("base_class", ""),
            "states": len(states),
        }

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


def _resolve_protocol_dir(ctx: Any, protocol: Optional[str]) -> str:
    """Resolve the protocol directory from workspace root and optional protocol name."""
    if protocol:
        candidate = os.path.join(ctx.root, protocol)
        if os.path.isdir(candidate):
            return candidate
        candidate = os.path.join(ctx.root, "protocol-testing", protocol)
        if os.path.isdir(candidate):
            return candidate
    return ctx.root


def register_propagation_tools(mcp: Any, ctx: Any) -> None:
    """Register propagation analysis MCP tools."""
    from ivy_lsp.mcp.tools import safe_tool

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_find_variants(
        type_name: str,
        protocol: str | None = None,
    ) -> dict:
        """Enumerate the structure of an Ivy type -- struct fields or variant members with tags."""
        protocol_dir = _resolve_protocol_dir(ctx, protocol)
        return find_variants_impl(type_name, protocol_dir)

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_serdes_correlation(
        type_name: str,
        protocol: str | None = None,
    ) -> dict:
        """Return the serializer/deserializer files correlated with an Ivy message type."""
        protocol_dir = _resolve_protocol_dir(ctx, protocol)
        return serdes_correlation_impl(type_name, protocol_dir)

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_change_impact(
        type_name: str,
        change_type: str,
        protocol: str | None = None,
    ) -> dict:
        """Categorize protocol files by impact of a type change (auto-propagate vs manual review)."""
        protocol_dir = _resolve_protocol_dir(ctx, protocol)
        return change_impact_impl(type_name, change_type, protocol_dir)
