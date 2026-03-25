"""Formatters for verification-related MCP tools.

Covers: ivy_verify, ivy_compile, ivy_model_info, ivy_diagnostics,
ivy_verification_dashboard.
"""

from __future__ import annotations

from ivy_lsp.tools.formatters._primitives import (
    _bullet_list,
    _code_block,
    _diag_line,
    _kv,
    _section,
    _table,
)


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
