"""MCP server mode for ivy-lsp.

Exposes Ivy verification tools, structured diagnostics, include graph,
and fast lint as MCP tools via the Model Context Protocol. Shares the
same parsing and indexing code as the LSP server.

Usage:
    python -m ivy_lsp --mcp
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)


def _find_ivy_files(root: str, exclude_dirs: set[str] | None = None) -> list[str]:
    """Walk the project and return relative paths to all .ivy files."""
    if exclude_dirs is None:
        exclude_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            "build", "dist", "submodules", "test",
        }
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fname in filenames:
            if fname.endswith(".ivy"):
                results.append(os.path.relpath(
                    os.path.join(dirpath, fname), root
                ))
    return results


def _parse_ivy_check_output(output: str) -> list[dict[str, Any]]:
    """Parse ivy_check output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    for line in output.splitlines():
        m = re.match(r"(.*?):(\d+):\s*(error|warning):\s*(.*)", line)
        if m:
            diagnostics.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "severity": m.group(3),
                "message": m.group(4),
            })
    return diagnostics


def _check_structural_issues(source: str, filepath: str) -> list[dict[str, Any]]:
    """Fast structural checks without full parsing."""
    diags: list[dict[str, Any]] = []
    lines = source.split("\n")

    # Missing #lang header
    stripped = source.lstrip()
    if not stripped.startswith("#lang"):
        diags.append({
            "line": 1, "severity": "warning",
            "message": "Missing '#lang ivy1.7' header", "source": "ivy-lint",
        })

    # Unmatched braces
    depth = 0
    for i, line_text in enumerate(lines):
        code = line_text if line_text.strip().startswith("#lang") else line_text.split("#")[0]
        for ch in code:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth < 0:
                diags.append({
                    "line": i + 1, "severity": "error",
                    "message": "Unmatched closing brace", "source": "ivy-lint",
                })
                depth = 0
    if depth > 0:
        diags.append({
            "line": len(lines), "severity": "error",
            "message": f"Unmatched opening brace ({depth} unclosed)",
            "source": "ivy-lint",
        })

    # Unresolved includes
    parent_dir = os.path.dirname(filepath)
    for match in re.finditer(r"^include\s+(\w+)", source, re.MULTILINE):
        inc_name = match.group(1)
        candidate = os.path.join(parent_dir, inc_name + ".ivy")
        if not os.path.isfile(candidate):
            line_no = source[: match.start()].count("\n") + 1
            diags.append({
                "line": line_no, "severity": "warning",
                "message": f"Unresolved include: {inc_name}",
                "source": "ivy-lint",
            })

    return diags


def start_mcp(workspace_root: str | None = None) -> None:
    """Start the MCP server exposing Ivy tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.critical(
            "MCP mode requires the 'mcp' package. "
            "Install with: pip install ivy-lsp[mcp]"
        )
        sys.exit(1)

    root = workspace_root or os.getcwd()
    mcp = FastMCP(
        "ivy-lsp",
        instructions=(
            "Ivy Language Server MCP tools for formal verification. "
            "Provides verification (ivy_check), compilation (ivyc), "
            "model inspection (ivy_show), fast linting, and include graph analysis."
        ),
    )

    @mcp.tool()
    def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Run ivy_check on an Ivy file to verify formal properties.

        Returns structured diagnostics with file, line, severity, and message.

        Args:
            relative_path: Relative path to the .ivy file to check.
            isolate: Optional isolate name to check in isolation.
        """
        import subprocess

        abs_path = os.path.join(root, relative_path)
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivy_check not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 120s"})

        duration = time.monotonic() - start
        raw_output = result.stderr + "\n" + result.stdout
        diagnostics = _parse_ivy_check_output(raw_output)

        return json.dumps({
            "success": result.returncode == 0,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "raw_output": raw_output.strip(),
            "duration_seconds": round(duration, 2),
        })

    @mcp.tool()
    def ivy_compile(
        relative_path: str,
        target: str = "test",
        isolate: str | None = None,
    ) -> str:
        """Compile an Ivy file to a test executable using ivyc.

        Args:
            relative_path: Relative path to the .ivy file to compile.
            target: Compilation target (default: "test").
            isolate: Optional isolate name to compile in isolation.
        """
        import subprocess

        abs_path = os.path.join(root, relative_path)
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        cmd = ["ivyc", f"target={target}"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivyc not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 300s"})

        return json.dumps({
            "success": result.returncode == 0,
            "output": (result.stderr + "\n" + result.stdout).strip(),
            "duration_seconds": round(time.monotonic() - start, 2),
        })

    @mcp.tool()
    def ivy_model_info(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Display the structure of an Ivy model using ivy_show.

        Args:
            relative_path: Relative path to the .ivy file to inspect.
            isolate: Optional isolate name for a specific isolate.
        """
        import subprocess

        abs_path = os.path.join(root, relative_path)
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        cmd = ["ivy_show"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivy_show not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 30s"})

        return json.dumps({
            "success": result.returncode == 0,
            "output": (result.stderr + "\n" + result.stdout).strip(),
            "duration_seconds": round(time.monotonic() - start, 2),
        })

    @mcp.tool()
    def ivy_lint(relative_path: str) -> str:
        """Fast structural lint of an Ivy file (milliseconds, no subprocess).

        Checks: missing #lang header, unmatched braces, unresolved includes.

        Args:
            relative_path: Relative path to the .ivy file to lint.
        """
        abs_path = os.path.join(root, relative_path)
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        diagnostics = _check_structural_issues(source, abs_path)
        return json.dumps({
            "file": relative_path,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
            "warning_count": sum(1 for d in diagnostics if d["severity"] == "warning"),
        })

    @mcp.tool()
    def ivy_include_graph(relative_path: str | None = None) -> str:
        """Return the include dependency graph for Ivy files.

        If a file is given, returns its includes and files that include it.
        If omitted, returns the full project include graph.

        Args:
            relative_path: Optional .ivy file to focus on.
        """
        graph: dict[str, list[str]] = {}
        file_by_basename: dict[str, str] = {}

        for rel_path in _find_ivy_files(root):
            basename = os.path.basename(rel_path)[:-4]
            file_by_basename[basename] = rel_path
            try:
                with open(os.path.join(root, rel_path), encoding="utf-8", errors="replace") as f:
                    source = f.read()
                graph[rel_path] = re.findall(r"^include\s+(\w+)", source, re.MULTILINE)
            except OSError:
                continue

        if relative_path is not None:
            includes = graph.get(relative_path, [])
            resolved = [{"module": inc, "resolved_path": file_by_basename.get(inc)} for inc in includes]

            target_basename = os.path.basename(relative_path)
            if target_basename.endswith(".ivy"):
                target_basename = target_basename[:-4]
            included_by = [fp for fp, incs in graph.items() if target_basename in incs]

            # Transitive includes
            transitive: set[str] = set()
            stack = list(includes)
            while stack:
                mod = stack.pop()
                if mod in transitive:
                    continue
                transitive.add(mod)
                mod_path = file_by_basename.get(mod)
                if mod_path and mod_path in graph:
                    stack.extend(graph[mod_path])

            return json.dumps({
                "file": relative_path,
                "includes": resolved,
                "included_by": included_by,
                "transitive_includes": sorted(transitive),
            })
        else:
            return json.dumps({
                "files": {fp: {"includes": incs} for fp, incs in graph.items()},
                "total_files": len(graph),
            })

    @mcp.tool()
    def ivy_capabilities() -> str:
        """Report which Ivy CLI tools are available on PATH."""
        return json.dumps({
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
        })

    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    mcp.run(transport="stdio")
