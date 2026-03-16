"""Analysis tools: ivy_lint, ivy_include_graph, ivy_capabilities."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any

from ivy_lsp.tools._helpers import error_response

logger = logging.getLogger(__name__)

_INCLUDE_PATTERN = re.compile(r"^include\s+(\w+)", re.MULTILINE)


def register_analysis_tools(mcp: Any, ctx: Any) -> None:
    """Register analysis-related MCP tools."""

    @mcp.tool()
    async def ivy_lint(relative_path: str) -> str:
        """Fast structural lint of an Ivy file (milliseconds, no subprocess).

        Checks: missing #lang header, unmatched braces, unresolved includes.

        Args:
            relative_path: Relative path to the .ivy file to lint.
        """
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return error_response(str(exc))
        if not os.path.isfile(abs_path):
            return error_response(f"File not found: {relative_path}")

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        resolve_cb = ctx.make_resolve_callback()
        diagnostics = ctx.check_structural_issues(source, abs_path, resolve_cb)
        return json.dumps({
            "success": True,
            "file": relative_path,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
            "warning_count": sum(1 for d in diagnostics if d["severity"] == "warning"),
        })

    @mcp.tool()
    async def ivy_include_graph(relative_path: str | None = None) -> str:
        """Return the include dependency graph for Ivy files.

        If a file is given, returns its includes and files that include it.
        If omitted, returns the full project include graph.

        Args:
            relative_path: Optional .ivy file to focus on.
        """

        def _build_graph():
            graph: dict[str, list[str]] = {}
            skipped_count = 0
            # Use shared basename cache (list-based, no collisions)
            cache = ctx.get_basename_cache()

            for rel_path in ctx.find_ivy_files(ctx.root):
                try:
                    with open(os.path.join(ctx.root, rel_path), encoding="utf-8", errors="replace") as f:
                        source = f.read()
                    graph[rel_path] = _INCLUDE_PATTERN.findall(source)
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
                    skipped_count += 1
                    continue

            return graph, cache, skipped_count

        def _resolve_closest(
            inc_name: str,
            from_file: str,
            cache: dict[str, list[str]],
        ) -> str | None:
            """Resolve include to the closest matching file by path proximity."""
            candidates = cache.get(inc_name)
            if not candidates:
                return None
            if len(candidates) == 1:
                return candidates[0]
            # Prefer the file sharing the longest common path prefix
            from_dir = os.path.dirname(from_file)
            best = candidates[0]
            best_len = 0
            for c in candidates:
                c_dir = os.path.dirname(c)
                if from_dir and c_dir:
                    try:
                        prefix = os.path.commonpath([from_dir, c_dir])
                    except ValueError:
                        prefix = ""
                else:
                    prefix = ""
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best = c
            return best

        graph, basename_cache, _skipped = await asyncio.to_thread(_build_graph)

        if relative_path is not None:
            # C5: Try key variants (with/without protocol-testing/ prefix)
            includes = graph.get(relative_path)
            resolved_key = relative_path
            if includes is None:
                alt_key = "protocol-testing/" + relative_path
                includes = graph.get(alt_key)
                if includes is not None:
                    resolved_key = alt_key
            if includes is None:
                for pfx in ["protocol-testing/", "protocol-testing" + os.sep]:
                    if relative_path.startswith(pfx):
                        stripped = relative_path[len(pfx):]
                        includes = graph.get(stripped)
                        if includes is not None:
                            resolved_key = stripped
                            break
            if includes is None:
                includes = []
            resolved = []
            for inc in includes:
                rp = _resolve_closest(inc, resolved_key, basename_cache)
                entry: dict[str, Any] = {"module": inc, "resolved_path": rp}
                # Flag ambiguous resolution
                candidates = basename_cache.get(inc, [])
                if len(candidates) > 1:
                    entry["candidates"] = candidates
                resolved.append(entry)

            target_basename = os.path.basename(resolved_key)
            if target_basename.endswith(".ivy"):
                target_basename = target_basename[:-4]
            included_by = [fp for fp, incs in graph.items() if target_basename in incs]

            # Transitive includes (using proximity-based resolution)
            transitive: set[str] = set()
            stack = list(includes)
            while stack:
                mod = stack.pop()
                if mod in transitive:
                    continue
                transitive.add(mod)
                mod_path = _resolve_closest(mod, relative_path, basename_cache)
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
    async def ivy_capabilities() -> str:
        """Report which Ivy CLI tools are available on PATH."""
        return json.dumps({
            "success": True,
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
        })
