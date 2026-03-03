"""Shared verification functions for both LSP and MCP code paths.

These functions encapsulate staging-dir resolution, auto-isolate detection,
and subprocess execution. They are called by:
- features/commands.py (LSP handlers)
- mcp_server.py (MCP tool handlers)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ivy_lsp.utils.async_subprocess import run_ivy_subprocess
from ivy_lsp.utils.ivy_output import (
    extract_error_summary,
    parse_ivy_output,
)

log = logging.getLogger(__name__)


def resolve_staging_path(
    filepath: str,
    staging_dir: str | None = None,
) -> str:
    """Resolve a filepath through the staging directory if available.

    Ivy's include resolution is CWD-relative. The staging directory
    contains flat symlinks so that ivy_check can resolve includes
    correctly regardless of the original file layout.
    """
    if staging_dir is None:
        return filepath

    basename = os.path.basename(filepath)
    staged = os.path.join(staging_dir, basename)
    if os.path.exists(staged):
        return staged
    return filepath


def detect_isolates_for_file(
    symbols: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Detect available isolates from a symbol list.

    If symbols are provided (from an indexer), extract isolate names.
    Otherwise returns empty list.
    """
    if not symbols:
        return []
    return [
        s["name"]
        for s in symbols
        if s.get("kind") in ("isolate", "extract")
    ]


async def run_ivy_check(
    filepath: str,
    workspace_root: str,
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run ivy_check and return structured diagnostics.

    If staging_dir is provided, resolves the filepath through it
    so that include resolution works correctly.
    """
    resolved = resolve_staging_path(filepath, staging_dir)
    cmd = ["ivy_check"]
    if isolate:
        cmd.append(f"isolate={isolate}")
    cmd.append(resolved)

    result = await run_ivy_subprocess(
        cmd, timeout=timeout, cwd=os.path.dirname(resolved)
    )
    raw_output = "\n".join(result.output_lines)
    # Use the unified parser so all three functions return diagnostics
    # with a consistent schema (including the "source" field).
    diagnostics = parse_ivy_output(raw_output)

    return {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics),
        "raw_output": raw_output.strip(),
        "duration_seconds": round(result.duration, 2),
    }


async def run_ivy_compile(
    filepath: str,
    workspace_root: str,
    target: str = "test",
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run ivyc and return compilation result with structured diagnostics."""
    resolved = resolve_staging_path(filepath, staging_dir)
    cmd = ["ivyc", f"target={target}"]
    if isolate:
        cmd.append(f"isolate={isolate}")
    cmd.append(resolved)

    result = await run_ivy_subprocess(
        cmd, timeout=timeout, cwd=os.path.dirname(resolved)
    )
    raw_output = "\n".join(result.output_lines).strip()
    diagnostics = parse_ivy_output(raw_output)

    return {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics),
        "raw_output": raw_output,
        "target": target,
        "duration_seconds": round(result.duration, 2),
    }


async def run_ivy_show(
    filepath: str,
    workspace_root: str,
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run ivy_show and return model info with structured diagnostics."""
    resolved = resolve_staging_path(filepath, staging_dir)
    cmd = ["ivy_show"]
    if isolate:
        cmd.append(f"isolate={isolate}")
    cmd.append(resolved)

    result = await run_ivy_subprocess(
        cmd,
        timeout=timeout,
        cwd=os.path.dirname(resolved),
        use_semaphore=False,
    )
    raw_output = "\n".join(result.output_lines).strip()
    diagnostics = parse_ivy_output(raw_output)

    return {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics),
        "raw_output": raw_output,
        "duration_seconds": round(result.duration, 2),
    }
