"""Formatters for traceability-related MCP tools.

Covers: ivy_coverage (stats/matrix/gaps/diff), ivy_extract_requirements,
ivy_manifest (info/validate/staleness/refresh).
"""

from __future__ import annotations

from ivy_lsp.mcp.tools.formatters._primitives import (
    _badge,
    _bullet_list,
    _code_block,
    _kv,
    _pct_bar,
    _section,
    _table,
)

# ---------------------------------------------------------------------------
# ivy_coverage
# ---------------------------------------------------------------------------


def _format_ivy_coverage(data: dict) -> str:
    """Dispatcher: detects mode from response shape."""
    # diff mode
    if "delta_direction" in data:
        return _format_coverage_diff(data)
    # matrix mode
    if "matrix" in data:
        return _format_coverage_matrix(data)
    # gaps mode
    if "unguardedStateVars" in data or "uncoveredRfcRequirements" in data:
        return _format_coverage_gaps(data)
    # stats mode (default)
    return _format_coverage_stats(data)


def _format_coverage_stats(data: dict) -> str:
    total = data.get("total", 0)
    covered = data.get("covered", 0)
    pct = data.get("coverage_percent", 0)
    parts = ["## RFC Coverage Statistics"]
    parts.append(_pct_bar(pct))
    parts.append(f"{covered}/{total} requirements covered")

    by_level = data.get("by_level", {})
    if by_level:
        parts.append("")
        rows = []
        for level, info in sorted(by_level.items()):
            rows.append(
                [
                    _badge(level),
                    str(info.get("covered", 0)),
                    str(info.get("total", 0)),
                    f"{info.get('coverage_percent', 0):.1f}%",
                ]
            )
        parts.append(_table(["Level", "Covered", "Total", "%"], rows))

    by_layer = data.get("by_layer", {})
    if by_layer:
        parts.append("")
        parts.append(_section("By Layer"))
        rows = []
        for layer, info in sorted(by_layer.items()):
            rows.append(
                [
                    layer,
                    str(info.get("covered", 0)),
                    str(info.get("total", 0)),
                    f"{info.get('coverage_percent', 0):.1f}%",
                ]
            )
        parts.append(_table(["Layer", "Covered", "Total", "%"], rows))

    uncovered = data.get("uncovered_ids", [])
    if uncovered:
        parts.append("")
        parts.append(_section(f"Uncovered ({len(uncovered)})"))
        parts.append(_bullet_list(uncovered))
        if data.get("uncovered_ids_truncated"):
            parts.append("*(list truncated)*")

    manifests = data.get("manifests", [])
    if manifests:
        parts.append("")
        parts.append(_section("Manifests"))
        parts.append(_bullet_list(manifests))

    warnings = data.get("warnings", [])
    if warnings:
        parts.append("")
        for w in warnings:
            parts.append(f"> **Warning**: {w}")

    return "\n".join(parts)


def _format_coverage_matrix(data: dict) -> str:
    total_reqs = data.get("total_requirements", 0)
    covered = data.get("covered", 0)
    uncovered = data.get("uncovered", 0)
    parts = [f"## Traceability Matrix"]
    parts.append(f"{covered}/{total_reqs} covered, {uncovered} uncovered")

    matrix = data.get("matrix", [])
    if matrix:
        parts.append("")
        rows = []
        for m in matrix:
            cov = "Yes" if m.get("covered") else "No"
            assertions = m.get("assertions", [])
            loc = ""
            if assertions:
                first = assertions[0]
                loc = f"`{first.get('file', '')}:{first.get('line', '')}`"
                if len(assertions) > 1:
                    loc += f" (+{len(assertions) - 1})"
            rows.append(
                [
                    f"`{m.get('id', '')}`",
                    _badge(m.get("level", "")),
                    cov,
                    loc,
                ]
            )
        parts.append(_table(["Requirement", "Level", "Covered", "Location"], rows))

        if data.get("matrix_truncated"):
            parts.append(
                f"*(showing {len(matrix)} of {data.get('matrix_total', '?')})*"
            )

    warnings = data.get("warnings", [])
    if warnings:
        parts.append("")
        for w in warnings:
            parts.append(f"> **Warning**: {w}")

    return "\n".join(parts)


def _format_coverage_gaps(data: dict) -> str:
    parts = ["## Coverage Gaps"]

    summary = data.get("summary", {})
    if summary:
        parts.append(_kv("Total RFC reqs", summary.get("totalRfcReqs", "?")))
        parts.append(_kv("Uncovered RFC", summary.get("uncoveredRfcCount", "?")))
        parts.append(_kv("Unguarded state vars", summary.get("unguardedCount", "?")))

    unguarded = data.get("unguardedStateVars", [])
    if unguarded:
        parts.append("")
        parts.append(_section(f"Unguarded State Variables ({len(unguarded)})"))
        for v in unguarded[:30]:
            name = v.get("name", v.get("var", "?"))
            file_ = v.get("file", "")
            parts.append(f"- `{name}`" + (f" in `{file_}`" if file_ else ""))
        if len(unguarded) > 30:
            parts.append(f"- ... and {len(unguarded) - 30} more")

    uncovered_rfc = data.get("uncoveredRfcRequirements", [])
    if uncovered_rfc:
        parts.append("")
        parts.append(_section(f"Uncovered RFC Requirements ({len(uncovered_rfc)})"))
        for r in uncovered_rfc[:30]:
            rid = r.get("id", "?")
            level = r.get("level", "")
            text = r.get("text", "")[:80]
            parts.append(f"- `{rid}` {_badge(level)} {text}")
        if len(uncovered_rfc) > 30:
            parts.append(f"- ... and {len(uncovered_rfc) - 30} more")

    return "\n".join(parts)


def _format_coverage_diff(data: dict) -> str:
    direction = data.get("delta_direction", "unchanged")
    delta = data.get("delta_percent", 0)
    parts = [f"## Coverage Diff: {direction}"]
    parts.append(data.get("summary", ""))
    parts.append("")
    parts.append(_kv("Baseline", f"{data.get('baseline_coverage_percent', 0):.1f}%"))
    parts.append(_kv("Current", f"{data.get('current_coverage_percent', 0):.1f}%"))
    parts.append(_kv("Delta", f"{'+' if delta > 0 else ''}{delta:.1f}%"))

    recovered = data.get("recovered", [])
    if recovered:
        parts.append("")
        parts.append(_section(f"Recovered ({len(recovered)})"))
        parts.append(_bullet_list(recovered))

    new_gaps = data.get("new_gaps", [])
    if new_gaps:
        parts.append("")
        parts.append(_section(f"New Gaps ({len(new_gaps)})"))
        parts.append(_bullet_list(new_gaps))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ivy_extract_requirements
# ---------------------------------------------------------------------------


def _format_ivy_extract_requirements(data: dict) -> str:
    """Dispatcher by output format."""
    if "yaml" in data:
        return _format_extract_manifest(data)
    return _format_extract_structured(data)


def _format_extract_structured(data: dict) -> str:
    reqs = data.get("requirements", [])
    total = data.get("total", len(reqs))
    parts = [f"## Extracted Requirements ({total})"]

    by_level = data.get("by_level", {})
    if by_level:
        level_parts = [f"{_badge(k)}: {v}" for k, v in sorted(by_level.items())]
        parts.append(" | ".join(level_parts))

    if reqs:
        parts.append("")
        for r in reqs[:50]:
            level = r.get("level", "")
            text = r.get("text", "")[:120]
            parts.append(f"- {_badge(level)} {text}")
        if len(reqs) > 50:
            parts.append(f"- ... and {len(reqs) - 50} more")

    return "\n".join(parts)


def _format_extract_manifest(data: dict) -> str:
    total = data.get("total_requirements", 0)
    parts = [f"## Generated Manifest ({total} requirements)"]

    by_level = data.get("by_level", {})
    if by_level:
        level_parts = [f"{_badge(k)}: {v}" for k, v in sorted(by_level.items())]
        parts.append(" | ".join(level_parts))

    suggested = data.get("suggested_path")
    if suggested:
        parts.append(_kv("Suggested path", f"`{suggested}`"))

    yaml_content = data.get("yaml", "")
    if yaml_content:
        parts.append("")
        parts.append(_code_block(yaml_content, "yaml"))

    metadata = data.get("metadata")
    if metadata:
        parts.append("")
        parts.append(_section("Metadata"))
        for k, v in metadata.items():
            parts.append(f"- {_kv(k, v)}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ivy_manifest
# ---------------------------------------------------------------------------


def _format_ivy_manifest(data: dict) -> str:
    """Dispatcher by mode (detected from response shape)."""
    from ivy_lsp.mcp.tools.formatters._primitives import _code_block

    if "reports" in data:
        # staleness or refresh -- differentiate by report fields
        reports = data["reports"]
        if reports and "is_stale" in reports[0]:
            return _format_manifest_staleness(data)
        return _format_manifest_staleness(data)  # both use same format
    if "results" in data:
        return _format_manifest_validate(data)
    if "manifests" in data:
        return _format_manifest_info(data)
    # refresh mode
    if "new_requirements_found" in data:
        return _format_manifest_refresh(data)
    # generic fallback -- import here to avoid circular import at module level
    import json

    cleaned = {k: v for k, v in data.items() if not k.startswith("_")}
    return _code_block(json.dumps(cleaned, indent=2), "json")


def _format_manifest_info(data: dict) -> str:
    manifests = data.get("manifests", [])
    parts = [f"## Manifest Info ({data.get('total_manifests', len(manifests))})"]

    if manifests:
        rows = []
        for m in manifests:
            rows.append(
                [
                    f"`{m.get('path', '')}`",
                    m.get("protocol", ""),
                    str(m.get("requirements", 0)),
                    "Yes" if m.get("has_metadata") else "No",
                ]
            )
        parts.append(_table(["Path", "Protocol", "Requirements", "Metadata"], rows))

    without = data.get("protocols_without_manifests", [])
    if without:
        parts.append("")
        parts.append(_section("Protocols Without Manifests"))
        parts.append(_bullet_list(without))

    return "\n".join(parts)


def _format_manifest_validate(data: dict) -> str:
    results = data.get("results", [])
    all_valid = data.get("all_valid", False)
    status = "All valid" if all_valid else "Issues found"
    parts = [f"## Manifest Validation: {status}"]

    for r in results:
        path = r.get("path", "?")
        valid = r.get("valid", False)
        icon = "+" if valid else "X"
        parts.append(f"- [{icon}] `{path}`")
        for w in r.get("warnings", []):
            parts.append(f"  - {w}")

    return "\n".join(parts)


def _format_manifest_staleness(data: dict) -> str:
    reports = data.get("reports", [])
    parts = ["## Manifest Staleness"]

    for r in reports:
        path = r.get("path", "?")
        stale = r.get("is_stale", False)
        status_label = r.get("status", "stale" if stale else "fresh")
        icon = "!" if stale or status_label == "no_metadata" else "+"
        parts.append(f"- [{icon}] `{path}` ({status_label})")
        for reason in r.get("reasons", []):
            parts.append(f"  - {reason}")
        for info in r.get("info", []):
            parts.append(f"  - {info}")
        if r.get("obsoleted_by"):
            parts.append(f"  - Obsoleted by: {r['obsoleted_by']}")
        if r.get("updated_by"):
            parts.append(f"  - Updated by: {', '.join(r['updated_by'])}")

    return "\n".join(parts)


def _format_manifest_refresh(data: dict) -> str:
    parts = ["## Manifest Refresh"]
    parts.append(_kv("RFC source", data.get("rfc_source", "?")))
    parts.append(_kv("New requirements found", data.get("new_requirements_found", 0)))
    parts.append(_kv("Current manifest IDs", data.get("current_manifest_ids", 0)))

    by_level = data.get("by_level", {})
    if by_level:
        level_parts = [f"{_badge(k)}: {v}" for k, v in sorted(by_level.items())]
        parts.append(" | ".join(level_parts))

    if data.get("source_hash"):
        parts.append(_kv("Source hash", f"`{data['source_hash']}`"))

    return "\n".join(parts)
