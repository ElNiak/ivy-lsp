"""Formatters for visualization, analysis, pattern, and quality MCP tools.

Covers: ivy_include_graph, ivy_capabilities, ivy_scope, ivy_health_check,
ivy_visualize, ivy_model_summary, ivy_patterns, ivy_pattern_scaffold,
ivy_quality.
"""

from __future__ import annotations

from ivy_lsp.mcp.tools.formatters.primitives import (
    _badge,
    _bullet_list,
    _code_block,
    _kv,
    _pct_bar,
    _section,
    _table,
)

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
# Visualization formatters
# ---------------------------------------------------------------------------


def _format_ivy_visualize(data: dict) -> str:
    """Dispatcher by view (detected from response shape)."""
    if "states" in data or "transitions" in data:
        return _format_viz_state_machine(data)
    if "layers" in data:
        return _format_viz_layers(data)
    # dependencies (default) -- has nodes/edges
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
