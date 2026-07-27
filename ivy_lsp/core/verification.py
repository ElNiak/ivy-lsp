"""Shared verification functions for both LSP and MCP code paths.

These functions encapsulate staging-dir resolution, auto-isolate detection,
and subprocess execution. They are called by:
- features/commands.py (LSP handlers)
- mcp_server.py (MCP tool handlers)
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from typing import Any

from ivy_lsp.core.environment import detect_z3_dir
from ivy_lsp.infra.observability import LogCategory, log_phase, timed_phase
from ivy_lsp.infra.utils.async_subprocess import run_ivy_subprocess
from ivy_lsp.infra.utils.ivy_output import (
    extract_error_summary,
    parse_check_results,
    parse_ivy_output,
)

log = logging.getLogger(__name__)


def resolve_staging_path(
    filepath: str,
    staging_dir: str | None = None,
    resolver: Any | None = None,
) -> str:
    """Resolve a filepath through the staging directory if available.

    Ivy's include resolution is CWD-relative. The staging directory
    contains flat symlinks so that ivy_check can resolve includes
    correctly regardless of the original file layout.

    When a *resolver* with layered staging is available, prefer the
    file's own layer staging directory over flat staging.  This ensures
    the Ivy compiler's CWD-relative resolution picks the correct
    variant for colliding basenames (e.g., ``ivy_quic_server.ivy``
    exists in both quic and apt layers).
    """
    basename = os.path.basename(filepath)

    # 1. Try layer-specific staging (when resolver knows the file's layer)
    if resolver is not None:
        file_to_layer = getattr(resolver, "_file_to_layer", None)
        partition_staging = getattr(resolver, "_partition_staging", None)
        if file_to_layer and partition_staging:
            layer_id = file_to_layer.get(os.path.abspath(filepath))
            if layer_id and layer_id in partition_staging:
                candidate = os.path.join(partition_staging[layer_id], basename)
                if os.path.exists(candidate):
                    return candidate

    # 2. Fall back to flat staging
    if staging_dir is not None:
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
    return [s["name"] for s in symbols if s.get("kind") in ("isolate", "extract")]


async def run_ivy_check(
    filepath: str,
    workspace_root: str,
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 120.0,
    resolver: Any | None = None,
) -> dict[str, Any]:
    """Run ivy_check and return structured diagnostics.

    If staging_dir is provided, resolves the filepath through it
    so that include resolution works correctly.
    """
    with timed_phase(
        log,
        category=LogCategory.PERFORMANCE,
        phase="verification",
        name="ivy_check",
        channel="tool",
        payload={"filepath": filepath, "isolate": isolate},
    ):
        resolved = resolve_staging_path(filepath, staging_dir, resolver=resolver)
        cwd = os.path.dirname(resolved)
        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(os.path.basename(resolved))

        result = await run_ivy_subprocess(cmd, timeout=timeout, cwd=cwd)
        raw_output = "\n".join(result.output_lines)
        diagnostics = parse_ivy_output(raw_output)
        check_results = parse_check_results(raw_output)

    timed_out = "Timed out" in result.message
    response: dict[str, Any] = {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics)
        and check_results["failed"] == 0,
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics, check_results),
        "check_results": check_results,
        "raw_output": raw_output.strip(),
        "duration_seconds": round(result.duration, 2),
    }
    if timed_out:
        response["timed_out"] = True
        response["error_summary"] = result.message
    log_phase(
        log,
        category=LogCategory.DIAGNOSTIC,
        phase="verification",
        message="ivy_check completed",
        data={
            "filepath": filepath,
            "isolate": isolate,
            "success": response["success"],
            "diagnostic_count": response["diagnostic_count"],
        },
        level=logging.INFO,
    )
    return response


def _find_workspace_root(filepath: str) -> str | None:
    """Walk up from filepath looking for .ivyworkspace marker."""
    current = os.path.dirname(os.path.abspath(filepath))
    for _ in range(10):
        if os.path.isfile(os.path.join(current, ".ivyworkspace")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _get_ivy_include_dir() -> str | None:
    """Find the Ivy include/1.7 directory from the installed package."""
    try:
        import ivy as _ivy

        return os.path.join(os.path.dirname(_ivy.__file__), "include", "1.7")
    except ImportError:
        return None


def _stage_workspace_files(workspace_root: str, include_dir: str) -> list[str]:
    """Copy all .ivy files from workspace subdirectories into include_dir.

    Returns list of absolute paths of staged files (for cleanup).
    """
    staged: list[str] = []
    for ivy_file in glob.glob(
        os.path.join(workspace_root, "**", "*.ivy"), recursive=True
    ):
        if "_tests" in ivy_file or "_test" in os.path.basename(ivy_file):
            continue
        dest = os.path.join(include_dir, os.path.basename(ivy_file))
        shutil.copy2(ivy_file, dest)
        staged.append(dest)
    return staged


async def run_ivy_compile(
    filepath: str,
    workspace_root: str,
    target: str = "test",
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 300.0,
    resolver: Any | None = None,
) -> dict[str, Any]:
    """Run ivyc and return compilation result with structured diagnostics."""
    with timed_phase(
        log,
        category=LogCategory.PERFORMANCE,
        phase="verification",
        name="ivyc",
        channel="tool",
        payload={"filepath": filepath, "target": target, "isolate": isolate},
    ):
        resolved = resolve_staging_path(filepath, staging_dir, resolver=resolver)
        cwd = os.path.dirname(resolved)
        os.makedirs(os.path.join(cwd, "build"), exist_ok=True)

        # Stage workspace files into Ivy's include dir for target=test
        staged_files: list[str] = []
        if target == "test":
            ws_root = _find_workspace_root(filepath)
            include_dir = _get_ivy_include_dir()
            if ws_root and include_dir and os.path.isdir(include_dir):
                staged_files = _stage_workspace_files(ws_root, include_dir)

        # Build Z3DIR-aware environment
        env = None
        z3_dir = detect_z3_dir()
        if z3_dir:
            env = {**os.environ, "Z3DIR": z3_dir}

        cmd = ["ivyc", f"target={target}"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(os.path.basename(resolved))

        try:
            result = await run_ivy_subprocess(cmd, timeout=timeout, cwd=cwd, env=env)
        finally:
            for f in staged_files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        raw_output = "\n".join(result.output_lines).strip()
        diagnostics = parse_ivy_output(raw_output)

    response = {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics),
        "raw_output": raw_output,
        "target": target,
        "duration_seconds": round(result.duration, 2),
    }
    log_phase(
        log,
        category=LogCategory.DIAGNOSTIC,
        phase="verification",
        message="ivyc completed",
        data={
            "filepath": filepath,
            "target": target,
            "isolate": isolate,
            "success": response["success"],
            "diagnostic_count": response["diagnostic_count"],
        },
        level=logging.INFO,
    )
    return response


async def run_ivy_show(
    filepath: str,
    workspace_root: str,
    isolate: str | None = None,
    staging_dir: str | None = None,
    timeout: float = 30.0,
    resolver: Any | None = None,
    coi: bool = True,
) -> dict[str, Any]:
    """Run ivy_show and return model info with structured diagnostics."""
    with timed_phase(
        log,
        category=LogCategory.PERFORMANCE,
        phase="verification",
        name="ivy_show",
        channel="tool",
        payload={"filepath": filepath, "isolate": isolate, "coi": coi},
    ):
        resolved = resolve_staging_path(filepath, staging_dir, resolver=resolver)
        cwd = os.path.dirname(resolved)
        cmd = ["ivy_show"]
        if not coi:
            cmd.append("coi=false")
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(os.path.basename(resolved))

        result = await run_ivy_subprocess(
            cmd,
            timeout=timeout,
            cwd=cwd,
            use_semaphore=False,
        )
        raw_output = "\n".join(result.output_lines).strip()
        diagnostics = parse_ivy_output(raw_output)

    response = {
        "success": result.success
        and not any(d["severity"] == "error" for d in diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_summary": extract_error_summary(raw_output, diagnostics),
        "raw_output": raw_output,
        "duration_seconds": round(result.duration, 2),
    }
    log_phase(
        log,
        category=LogCategory.DIAGNOSTIC,
        phase="verification",
        message="ivy_show completed",
        data={
            "filepath": filepath,
            "isolate": isolate,
            "success": response["success"],
            "diagnostic_count": response["diagnostic_count"],
        },
        level=logging.INFO,
    )
    return response
