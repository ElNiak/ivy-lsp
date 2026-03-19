"""Markdown formatters for MCP tool results.

Each MCP tool has a dedicated formatter that converts its JSON result dict
into human-readable markdown.  ``format_tool_result()`` dispatches by tool
name; ``format_error()`` handles error/timeout responses.

The ``safe_tool`` decorator in ``tools/__init__`` calls these after the
tool handler returns, so tool files themselves remain unchanged (Phase 1).
"""

from __future__ import annotations

import json
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def _section(title: str, level: int = 3) -> str:
    """Return a markdown section header."""
    return f"{'#' * level} {title}"


def _kv(key: str, value: Any) -> str:
    """Return a bold key-value line."""
    return f"**{key}**: {value}"


def _badge(label: str) -> str:
    """Return an inline badge like `[MUST]`."""
    return f"`{label}`"


def _pct_bar(pct: float, width: int = 20) -> str:
    """Return a text progress bar like [############........] 60.0%."""
    filled = int(round(pct / 100 * width))
    bar = "#" * filled + "." * (width - filled)
    return f"[{bar}] {pct:.1f}%"


def _diag_line(d: dict) -> str:
    """Format a single diagnostic as a bullet line."""
    sev = d.get("severity", "info")
    icon = {"error": "X", "warning": "!", "hint": "?", "info": "i"}.get(sev, "-")
    loc = d.get("file", "")
    line = d.get("line")
    if loc and line:
        loc = f"`{loc}:{line}`"
    elif loc:
        loc = f"`{loc}`"
    elif line:
        loc = f"line {line}"
    msg = d.get("message", "")
    parts = [f"[{icon}]"]
    if loc:
        parts.append(loc)
    parts.append(msg)
    return "- " + " ".join(parts)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """Build a markdown table from headers and rows."""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _code_block(content: str, lang: str = "") -> str:
    """Wrap content in a fenced code block."""
    return f"```{lang}\n{content}\n```"


def _bullet_list(items: list[str], max_items: int = 30) -> str:
    """Return a bullet list, truncating if needed."""
    lines = [f"- `{item}`" for item in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"- ... and {len(items) - max_items} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Error formatter
# ---------------------------------------------------------------------------


def format_error(data: dict) -> str:
    """Format an error/timeout response as markdown."""
    msg = data.get("message", "Unknown error")
    parts = [f"**Error** -- {msg}"]

    note = data.get("note")
    if note:
        parts.append(f"\n> {note}")

    if data.get("timeout"):
        tool = data.get("tool", "unknown")
        parts.append(f"\nTool `{tool}` exceeded its timeout limit.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------


def _format_generic(data: dict) -> str:
    """Render any dict as indented JSON in a code fence (fallback)."""
    # Remove internal fields
    cleaned = {k: v for k, v in data.items() if not k.startswith("_")}
    return _code_block(json.dumps(cleaned, indent=2), "json")


# ---------------------------------------------------------------------------
# Verification formatters
# ---------------------------------------------------------------------------


def _format_ivy_verify(data: dict) -> str:
    success = data.get("success", False)
    status = "PASS" if success else "FAIL"
    duration = data.get("duration_seconds", 0)
    parts = [f"## Verification: {status}"]
    parts.append(_kv("Duration", f"{duration:.2f}s"))

    if data.get("cached"):
        parts.append("*(cached result)*")

    isolate = data.get("isolate")
    if isolate:
        parts.append(_kv("Isolate", f"`{isolate}`"))

    diags = data.get("diagnostics", [])
    if diags:
        parts.append("")
        parts.append(_section(f"Diagnostics ({len(diags)})"))
        for d in diags[:30]:
            parts.append(_diag_line(d))
        if len(diags) > 30:
            parts.append(f"- ... and {len(diags) - 30} more")

    err_summary = data.get("error_summary")
    if err_summary:
        parts.append("")
        parts.append(_section("Error Summary"))
        parts.append(err_summary)

    cex_trace = data.get("counterexample_trace")
    if cex_trace:
        parts.append("")
        parts.append(_section("Counterexample"))
        parts.append(_code_block(cex_trace))

    return "\n".join(parts)


def _format_ivy_compile(data: dict) -> str:
    success = data.get("success", False)
    status = "SUCCESS" if success else "FAIL"
    duration = data.get("duration_seconds", 0)
    parts = [f"## Compilation: {status}"]
    parts.append(_kv("Duration", f"{duration:.2f}s"))

    target = data.get("target")
    if target:
        parts.append(_kv("Target", f"`{target}`"))

    fallback = data.get("fallback")
    if fallback:
        reason = data.get("fallback_reason", "")
        parts.append(
            f"\n> **Fallback**: Using {fallback}" + (f" ({reason})" if reason else "")
        )

    diags = data.get("diagnostics", [])
    if diags:
        parts.append("")
        parts.append(_section(f"Diagnostics ({len(diags)})"))
        for d in diags[:30]:
            parts.append(_diag_line(d))
        if len(diags) > 30:
            parts.append(f"- ... and {len(diags) - 30} more")

    err_summary = data.get("error_summary")
    if err_summary:
        parts.append("")
        parts.append(_section("Error Summary"))
        parts.append(err_summary)

    return "\n".join(parts)


def _format_ivy_model_info(data: dict) -> str:
    parts = [f"## Model Info"]
    if data.get("file"):
        parts.append(_kv("File", f"`{data['file']}`"))
    if data.get("type"):
        parts.append(_kv("Type", data["type"]))

    content = data.get("content") or data.get("raw_output") or data.get("output")
    if content:
        parts.append("")
        parts.append(_code_block(str(content), "ivy"))

    return "\n".join(parts)


def _format_ivy_diagnostics(data: dict) -> str:
    mode = data.get("mode", "full")
    file_ = data.get("file", "")
    diags = data.get("diagnostics", [])
    parts = [f"## Diagnostics ({mode} mode)"]
    if file_:
        parts.append(_kv("File", f"`{file_}`"))

    # Counts
    counts = []
    for sev in ("error", "warning", "info", "hint"):
        c = data.get(f"{sev}_count", 0)
        if c:
            counts.append(f"{c} {sev}{'s' if c != 1 else ''}")
    if counts:
        parts.append(_kv("Found", ", ".join(counts)))
    elif not diags:
        parts.append("No issues found.")

    # By-source breakdown
    by_source = data.get("by_source", {})
    if by_source:
        parts.append("")
        parts.append(_section("By Source"))
        rows = [[src, str(cnt)] for src, cnt in sorted(by_source.items())]
        parts.append(_table(["Source", "Count"], rows))

    if diags:
        parts.append("")
        parts.append(_section(f"Issues ({len(diags)})"))
        for d in diags[:40]:
            parts.append(_diag_line(d))
        if len(diags) > 40:
            parts.append(f"- ... and {len(diags) - 40} more")

    layer_errors = data.get("layer_errors", [])
    if layer_errors:
        parts.append("")
        parts.append(_section("Layer Errors"))
        for le in layer_errors:
            parts.append(f"- **{le.get('layer', '?')}**: {le.get('error', '')}")

    if data.get("partial"):
        parts.append("\n> *Partial results* -- some layers failed")

    return "\n".join(parts)


def _format_ivy_verification_dashboard(data: dict) -> str:
    total = data.get("total_files", 0)
    verified = data.get("verified", 0)
    failed = data.get("failed", 0)
    pending = data.get("pending", 0)
    parts = [f"## Verification Dashboard"]
    parts.append(_kv("Total files", total))
    parts.append(_kv("Verified", verified))
    parts.append(_kv("Failed", failed))
    parts.append(_kv("Pending", pending))
    parts.append(
        _kv("Cache", f"{data.get('cache_size', 0)}/{data.get('cache_max', 0)}")
    )

    verified_files = data.get("verified_files", [])
    if verified_files:
        parts.append("")
        parts.append(_section("Verified Files"))
        parts.append(_bullet_list(verified_files))

    failed_files = data.get("failed_files", [])
    if failed_files:
        parts.append("")
        parts.append(_section("Failed Files"))
        parts.append(_bullet_list(failed_files))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Analysis formatters
# ---------------------------------------------------------------------------


def _format_ivy_include_graph(data: dict) -> str:
    # Single-file mode
    if "file" in data:
        file_ = data["file"]
        parts = [f"## Include Graph: `{file_}`"]

        includes = data.get("includes", [])
        if includes:
            parts.append("")
            parts.append(_section("Includes"))
            for inc in includes:
                mod = inc.get("module", "")
                resolved = inc.get("resolved_path")
                line = f"- `{mod}`"
                if resolved:
                    line += f" -> `{resolved}`"
                candidates = inc.get("candidates")
                if candidates and len(candidates) > 1:
                    line += f" *(ambiguous: {len(candidates)} candidates)*"
                parts.append(line)
        else:
            parts.append("\nNo includes.")

        included_by = data.get("included_by", [])
        if included_by:
            parts.append("")
            parts.append(_section("Included By"))
            parts.append(_bullet_list(included_by))

        transitive = data.get("transitive_includes", [])
        if transitive:
            parts.append("")
            parts.append(_section(f"Transitive Closure ({len(transitive)})"))
            parts.append(_bullet_list(transitive))

        return "\n".join(parts)

    # Full graph mode
    files = data.get("files", {})
    total = data.get("total_files", len(files))
    parts = [f"## Include Graph (full workspace)"]
    parts.append(_kv("Total files", total))
    if files:
        parts.append("")
        for fp, info in list(files.items())[:30]:
            incs = info.get("includes", [])
            parts.append(f"- `{fp}` ({len(incs)} includes)")
        if len(files) > 30:
            parts.append(f"- ... and {len(files) - 30} more files")
    return "\n".join(parts)


def _format_ivy_capabilities(data: dict) -> str:
    parts = ["## Ivy Capabilities"]

    # CLI tools
    cli_tools = data.get("cli_tools", {})
    if cli_tools:
        parts.append(_section("CLI Tools"))
        for tool, available in cli_tools.items():
            icon = "+" if available else "-"
            parts.append(f"- [{icon}] `{tool}`")
    else:
        # Fallback to legacy flat keys
        for tool in ("ivy_check", "ivyc", "ivy_show"):
            available = data.get(tool, False)
            icon = "+" if available else "-"
            parts.append(f"- [{icon}] `{tool}`")

    # MCP tools
    mcp_tools = data.get("mcp_tools", {})
    mcp_count = data.get("mcp_tool_count", len(mcp_tools))
    if mcp_tools:
        parts.append("")
        parts.append(_section(f"MCP Tools ({mcp_count})"))
        by_category: dict[str, list[str]] = {}
        for name, meta in mcp_tools.items():
            cat = meta.get("category", "other") if isinstance(meta, dict) else "other"
            by_category.setdefault(cat, []).append(name)
        for cat in sorted(by_category):
            tools = sorted(by_category[cat])
            parts.append(f"- **{cat}**: {', '.join(f'`{t}`' for t in tools)}")

    staging = data.get("staging_health")
    if staging:
        parts.append("")
        parts.append(_section("Staging Health"))
        if isinstance(staging, dict):
            for k, v in staging.items():
                parts.append(f"- {_kv(k, v)}")
        else:
            parts.append(str(staging))

    return "\n".join(parts)


def _format_ivy_scope(data: dict) -> str:
    file_ = data.get("file", "")
    parts = [f"## Scope: `{file_}`"]

    mirrors = data.get("endpoint_mirrors", [])
    if mirrors:
        parts.append(_kv("Endpoint mirrors", len(mirrors)))
        for m in mirrors:
            parts.append(f"  - `{m}`")
    else:
        parts.append("No endpoint mirrors found.")

    role = data.get("tester_role")
    if role:
        parts.append(_kv("Tester role", role))

    closure_size = data.get("include_closure_size")
    if closure_size is not None:
        parts.append(_kv("Include closure", f"{closure_size} files"))

    closure = data.get("include_closure", [])
    if closure:
        parts.append("")
        parts.append(_section("Include Closure"))
        parts.append(_bullet_list(closure))

    exported = data.get("exported_actions", [])
    if exported:
        parts.append("")
        parts.append(_section("Exported Actions"))
        parts.append(_bullet_list(exported))

    imported = data.get("imported_actions", [])
    if imported:
        parts.append("")
        parts.append(_section("Imported Actions"))
        parts.append(_bullet_list(imported))

    mirror_roles = data.get("mirror_roles")
    if mirror_roles:
        parts.append("")
        parts.append(_section("Mirror Roles"))
        for m, r in mirror_roles.items():
            parts.append(f"- `{m}` -> {r}")

    partition = data.get("partition")
    if partition:
        parts.append(_kv("Partition", partition))

    collision = data.get("collision_report")
    if collision:
        parts.append("")
        parts.append(_section("Collision Report"))
        parts.append(_kv("Basename", f"`{collision.get('basename', '')}`"))
        for v in collision.get("variants", []):
            parts.append(f"  - `{v}`")

    return "\n".join(parts)


def _format_ivy_health_check(data: dict) -> str:
    parts = ["## Health Check"]

    server = data.get("server", {})
    if server:
        parts.append(_kv("Workspace", f"`{server.get('workspace', '')}`"))
        staging = server.get("staging_dir")
        if staging:
            parts.append(_kv("Staging", f"`{staging}`"))

    model = data.get("model_status", {})
    if model:
        state = model.get("state", "unknown")
        parts.append(_kv("Model", state))
        if model.get("error"):
            parts.append(f"  > Error: {model['error']}")

    # Capabilities
    caps = data.get("capabilities", {})
    if caps:
        parts.append("")
        parts.append(_section("Capabilities"))
        for tool, avail in caps.items():
            icon = "+" if avail else "-"
            parts.append(f"- [{icon}] `{tool}`")

    # File count
    wf = data.get("workspace_files")
    if wf is not None and wf >= 0:
        parts.append(_kv("Workspace files", wf))

    # Verification cache
    vcache = data.get("verification_cache")
    if vcache and not vcache.get("error"):
        parts.append("")
        parts.append(_section("Verification Cache"))
        parts.append(_kv("Size", vcache.get("cache_size", 0)))
        parts.append(_kv("Verified", len(vcache.get("verified_files", []))))
        parts.append(_kv("Failed", len(vcache.get("failed_files", []))))

    # Tool metrics
    metrics = data.get("tool_metrics", {})
    if metrics:
        parts.append("")
        parts.append(_section("Tool Metrics"))
        rows = []
        for tn, m in sorted(metrics.items()):
            rows.append(
                [
                    tn,
                    str(m.get("call_count", 0)),
                    f"{m.get('avg_duration_seconds', 0):.2f}s",
                    str(m.get("error_count", 0)),
                    str(m.get("timeout_count", 0)),
                ]
            )
        parts.append(
            _table(
                ["Tool", "Calls", "Avg Duration", "Errors", "Timeouts"],
                rows,
            )
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Traceability formatters
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


def _format_ivy_manifest(data: dict) -> str:
    """Dispatcher by mode (detected from response shape)."""
    if "reports" in data:
        # staleness or refresh — differentiate by report fields
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
    return _format_generic(data)


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


# ---------------------------------------------------------------------------
# Visualization formatters
# ---------------------------------------------------------------------------


def _format_ivy_visualize(data: dict) -> str:
    """Dispatcher by view (detected from response shape)."""
    if "states" in data or "transitions" in data:
        return _format_viz_state_machine(data)
    if "layers" in data:
        return _format_viz_layers(data)
    # dependencies (default) — has nodes/edges
    return _format_viz_dependencies(data)


def _format_viz_dependencies(data: dict) -> str:
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    parts = ["## Action Dependencies"]
    parts.append(_kv("Nodes", len(nodes)))
    parts.append(_kv("Edges", len(edges)))

    if nodes:
        parts.append("")
        parts.append(_section("Actions"))
        for n in nodes[:30]:
            name = n.get("name", n.get("id", "?"))
            parts.append(f"- `{name}`")
        if len(nodes) > 30:
            parts.append(f"- ... and {len(nodes) - 30} more")

    if data.get("truncated"):
        parts.append(f"\n*(truncated, {data.get('total', '?')} total)*")

    return "\n".join(parts)


def _format_viz_state_machine(data: dict) -> str:
    states = data.get("states", [])
    transitions = data.get("transitions", [])
    parts = ["## State Machine View"]
    parts.append(_kv("States", len(states)))
    parts.append(_kv("Transitions", len(transitions)))

    if states:
        parts.append("")
        parts.append(_section("States"))
        for s in states[:20]:
            name = s.get("name", s.get("var", "?"))
            type_ = s.get("type", "")
            parts.append(f"- `{name}`" + (f" ({type_})" if type_ else ""))
        if len(states) > 20:
            parts.append(f"- ... and {len(states) - 20} more")

    if transitions:
        parts.append("")
        parts.append(_section("Transitions"))
        for t in transitions[:20]:
            action = t.get("action", t.get("name", "?"))
            reads = t.get("reads", [])
            writes = t.get("writes", [])
            parts.append(f"- `{action}` reads:{len(reads)} writes:{len(writes)}")
        if len(transitions) > 20:
            parts.append(f"- ... and {len(transitions) - 20} more")

    if data.get("truncated"):
        parts.append(f"\n*(truncated, {data.get('total', '?')} total)*")

    return "\n".join(parts)


def _format_viz_layers(data: dict) -> str:
    layers = data.get("layers", [])
    parts = ["## Layered Overview"]
    parts.append(_kv("Layers", len(layers)))

    for layer in layers[:20]:
        name = layer.get("name", layer.get("file", "?"))
        items = layer.get("items", layer.get("symbols", []))
        parts.append(f"- **{name}** ({len(items)} items)")
    if len(layers) > 20:
        parts.append(f"- ... and {len(layers) - 20} more")

    if data.get("truncated"):
        parts.append(f"\n*(truncated, {data.get('total', '?')} total)*")

    return "\n".join(parts)


def _format_ivy_model_summary(data: dict) -> str:
    """Dispatcher by detail (detected from response shape)."""
    if (
        "actions" in data
        or "requirements" in data
        and isinstance(data.get("requirements"), list)
    ):
        return _format_model_summary_requirements(data)
    return _format_model_summary_table(data)


def _format_model_summary_table(data: dict) -> str:
    rows_data = data.get("rows", [])
    parts = ["## Model Summary"]

    if rows_data:
        rows = []
        for r in rows_data:
            name = r.get("actionName", r.get("name", "?"))
            counts = r.get("counts", {})
            total = sum(counts.values()) if isinstance(counts, dict) else 0
            state_vars = r.get("stateVarCount", r.get("state_vars", ""))
            rows.append([f"`{name}`", str(total), str(state_vars)])
        parts.append(_table(["Action", "Requirements", "State Vars"], rows))

        if data.get("truncated"):
            parts.append(f"*(showing {len(rows_data)} of {data.get('total', '?')})*")
        if data.get("hasMore"):
            parts.append("*(more rows available)*")
    else:
        parts.append("No actions found.")

    return "\n".join(parts)


def _format_model_summary_requirements(data: dict) -> str:
    actions = data.get("actions", data.get("requirements", []))
    parts = ["## Action Requirements"]

    if isinstance(actions, list):
        for a in actions[:30]:
            name = a.get("actionName", a.get("name", "?"))
            reqs = a.get("requirements", [])
            parts.append(f"\n**`{name}`** ({len(reqs)} requirements)")
            for r in reqs[:10]:
                kind = r.get("kind", "")
                text = r.get("text", r.get("id", ""))[:80]
                parts.append(f"- {_badge(kind)} {text}" if kind else f"- {text}")
            if len(reqs) > 10:
                parts.append(f"  - ... and {len(reqs) - 10} more")
        if len(actions) > 30:
            parts.append(f"\n... and {len(actions) - 30} more actions")
    else:
        parts.append("No action requirements found.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pattern formatters
# ---------------------------------------------------------------------------


def _format_ivy_patterns(data: dict) -> str:
    # check mode (scaffold check)
    if "completeness_score" in data:
        return _format_patterns_check(data)
    # analyze/validate/compare mode
    return _format_patterns_analysis(data)


def _format_patterns_check(data: dict) -> str:
    protocol = data.get("protocol", "?")
    score = data.get("completeness_score", 0)
    present = data.get("present", 0)
    total = data.get("total_layers", 0)
    parts = [f"## Pattern Check: `{protocol}`"]
    parts.append(_pct_bar(score))
    parts.append(
        f"{present}/{total} layers present ({data.get('total_ivy_files', 0)} .ivy files)"
    )
    parts.append(_kv("Manifest", "Yes" if data.get("has_manifest") else "No"))

    layers_present = data.get("layers_present", [])
    if layers_present:
        parts.append("")
        parts.append(_section("Present Layers"))
        for lp in layers_present:
            files = ", ".join(f"`{f}`" for f in lp.get("files", []))
            parts.append(f"- **{lp.get('layer', '?')}**: {files}")

    layers_missing = data.get("layers_missing", [])
    if layers_missing:
        parts.append("")
        parts.append(_section("Missing Layers"))
        parts.append(_bullet_list(layers_missing))

    suggestions = data.get("suggestions", [])
    if suggestions:
        parts.append("")
        parts.append(_section("Suggestions"))
        for s in suggestions:
            priority = s.get("priority", "medium")
            parts.append(f"- [{priority}] {s.get('suggestion', '')}")

    return "\n".join(parts)


def _format_patterns_analysis(data: dict) -> str:
    parts = ["## Pattern Analysis"]

    patterns = data.get("patterns", [])
    if patterns:
        for p in patterns:
            name = p.get("name", p.get("pattern", "?"))
            count = p.get("count", p.get("instances", 0))
            parts.append(f"- **{name}**: {count} instances")
    elif data.get("protocol"):
        parts.append(_kv("Protocol", data["protocol"]))

    # Pass through any other top-level keys as key-value
    skip = {"patterns", "protocol", "success"}
    for k, v in data.items():
        if k not in skip and not k.startswith("_"):
            if isinstance(v, (str, int, float, bool)):
                parts.append(_kv(k, v))

    return "\n".join(parts)


def _format_ivy_pattern_scaffold(data: dict) -> str:
    parts = ["## Pattern Scaffold"]

    protocol = data.get("protocol")
    pattern = data.get("pattern")
    if protocol:
        parts.append(_kv("Protocol", protocol))
    if pattern:
        parts.append(_kv("Pattern", pattern))

    code = data.get("code", data.get("source", data.get("content", "")))
    if code:
        parts.append("")
        parts.append(_code_block(code, "ivy"))

    suggested_path = data.get("suggested_path")
    if suggested_path:
        parts.append(_kv("Suggested path", f"`{suggested_path}`"))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Quality formatters
# ---------------------------------------------------------------------------


def _format_ivy_quality(data: dict) -> str:
    """Dispatcher by mode (detected from response shape)."""
    if "checks" in data:
        return _format_quality_gate(data)
    return _format_quality_suggestions(data)


def _format_quality_suggestions(data: dict) -> str:
    suggestions = data.get("suggestions", [])
    parts = [f"## Quality Suggestions ({len(suggestions)})"]

    for s in suggestions:
        msg = s.get("message", s.get("suggestion", s.get("text", "")))
        category = s.get("category", s.get("type", ""))
        severity = s.get("severity", s.get("priority", ""))
        prefix = ""
        if severity:
            prefix = f"[{severity}] "
        elif category:
            prefix = f"[{category}] "
        parts.append(f"- {prefix}{msg}")

    if data.get("truncated"):
        parts.append(f"*(showing {len(suggestions)} of {data.get('total', '?')})*")

    return "\n".join(parts)


def _format_quality_gate(data: dict) -> str:
    protocol = data.get("protocol", "?")
    gate_level = data.get("gate_level", "?")
    passed = data.get("passed", False)
    status = "PASSED" if passed else "FAILED"
    checks = data.get("checks", [])
    parts = [f"## Quality Gate: {status} ({gate_level})"]
    parts.append(_kv("Protocol", f"`{protocol}`"))
    parts.append(
        f"{data.get('checks_passed', 0)}/{data.get('checks_total', 0)} checks passed"
    )

    if checks:
        parts.append("")
        for c in checks:
            icon = "+" if c.get("passed") else "X"
            level = c.get("level", "")
            detail = c.get("detail", "")
            parts.append(f"- [{icon}] **{c.get('check', '?')}** ({level}): {detail}")

            unresolved = c.get("unresolved", [])
            for u in unresolved[:5]:
                parts.append(f"  - `{u.get('file', '')}` -> `{u.get('include', '')}`")
            if len(unresolved) > 5:
                parts.append(f"  - ... and {len(unresolved) - 5} more")

    skipped = data.get("skipped_files", [])
    if skipped:
        parts.append("")
        parts.append(f"*{len(skipped)} files skipped (unreadable)*")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Dispatch registry
# ---------------------------------------------------------------------------

_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "ivy_verify": _format_ivy_verify,
    "ivy_compile": _format_ivy_compile,
    "ivy_model_info": _format_ivy_model_info,
    "ivy_diagnostics": _format_ivy_diagnostics,
    "ivy_verification_dashboard": _format_ivy_verification_dashboard,
    "ivy_include_graph": _format_ivy_include_graph,
    "ivy_capabilities": _format_ivy_capabilities,
    "ivy_scope": _format_ivy_scope,
    "ivy_health_check": _format_ivy_health_check,
    "ivy_coverage": _format_ivy_coverage,
    "ivy_extract_requirements": _format_ivy_extract_requirements,
    "ivy_manifest": _format_ivy_manifest,
    "ivy_visualize": _format_ivy_visualize,
    "ivy_model_summary": _format_ivy_model_summary,
    "ivy_patterns": _format_ivy_patterns,
    "ivy_pattern_scaffold": _format_ivy_pattern_scaffold,
    "ivy_quality": _format_ivy_quality,
}


def format_tool_result(tool_name: str, data: dict) -> str:
    """Dispatch to a per-tool formatter, falling back to generic."""
    if not isinstance(data, dict):
        return _code_block(str(data))
    formatter = _FORMATTERS.get(tool_name, _format_generic)
    try:
        return formatter(data)
    except Exception:
        # If a formatter crashes, fall back to generic rather than losing data
        try:
            return _format_generic(data)
        except Exception:
            return _code_block(str(data))


__all__ = ["format_error", "format_tool_result"]
