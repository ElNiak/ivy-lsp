"""Analysis tools: ivy_include_graph, ivy_capabilities, ivy_scope."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any

from ivy_lsp.debug_trace import ToolTraceContext
from ivy_lsp.parsing.tiered_extractor import TieredExtractor
from ivy_lsp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)

_extractor = TieredExtractor()


def register_analysis_tools(mcp: Any, ctx: Any) -> None:
    """Register analysis-related MCP tools."""

    @mcp.tool()
    @safe_tool
    async def ivy_include_graph(
        relative_path: str | None = None,
        detail: str = "summary",
        limit: int = 30,
        scope: str = "",
    ) -> dict:
        """Return the include dependency graph for Ivy files.

        If a file is given, returns its includes and files that include it.
        If omitted, returns a workspace-level summary.

        Args:
            relative_path: Optional .ivy file to focus on.
            detail: "summary" (default) returns file counts and top
                entry points; "full" returns every file with its includes.
            limit: Max files to return in summary mode (default 30).
            scope: Optional test scope name.  When set, the graph is
                filtered to only include edges within the scope's include
                closure.  Empty string (default) = no scoping.
        """
        logger.debug(
            "[ivy_include_graph] workspace=%s, args=%r",
            ctx.root,
            {"relative_path": relative_path, "scope": scope},
        )

        # Resolve scope for graph filtering
        _scope_files: frozenset[str] | None = None
        if scope and getattr(ctx, "workspace_context", None) is not None:
            _resolved_scope = ctx.workspace_context.get_test_scope(scope)
            if _resolved_scope is not None:
                _scope_files = _resolved_scope.include_closure
            else:
                logger.warning(
                    "[ivy_include_graph] Unknown scope '%s'; proceeding without scoping",
                    scope,
                )

        _tc = ToolTraceContext(
            "ivy_include_graph", {"relative_path": relative_path, "scope": scope}
        )

        def _build_graph():
            graph: dict[str, list[str]] = {}
            skipped_count = 0
            # Use shared basename cache (list-based, no collisions)
            cache = ctx.get_basename_cache()

            for rel_path in ctx.find_ivy_files(ctx.root):
                try:
                    with open(
                        os.path.join(ctx.root, rel_path),
                        encoding="utf-8",
                        errors="replace",
                    ) as f:
                        source = f.read()
                    abs_path = os.path.join(ctx.root, rel_path)
                    result = _extractor.extract(source, abs_path)
                    graph[rel_path] = result.includes
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

        # Task 3.2: Filter graph to scope's include closure when set.
        # include_closure contains absolute paths; graph keys are relative.
        if _scope_files is not None:
            scope_rel_files = {os.path.relpath(f, ctx.root) for f in _scope_files}
            graph = {fp: incs for fp, incs in graph.items() if fp in scope_rel_files}

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
                        stripped = relative_path[len(pfx) :]
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

            _file_result: dict[str, Any] = {
                "file": relative_path,
                "includes": resolved,
                "included_by": included_by,
                "transitive_includes": sorted(transitive),
            }
            if _scope_files is not None:
                _file_result["scope"] = scope
            return _tc.finish(_file_result)
        else:
            # Compute entry points (files not included by any other file)
            all_included = set()
            for incs in graph.values():
                all_included.update(incs)
            entry_points = sorted(
                fp
                for fp in graph
                if os.path.splitext(os.path.basename(fp))[0] not in all_included
            )

            if detail == "full":
                # Full graph — may be very large
                files_data = {fp: {"includes": incs} for fp, incs in graph.items()}
                if limit > 0 and len(files_data) > limit:
                    # Truncate
                    truncated_keys = sorted(files_data.keys())[:limit]
                    files_data = {k: files_data[k] for k in truncated_keys}
                    _trunc_result: dict[str, Any] = {
                        "files": files_data,
                        "total_files": len(graph),
                        "truncated": True,
                        "showing": limit,
                    }
                    if _scope_files is not None:
                        _trunc_result["scope"] = scope
                    return _tc.finish(_trunc_result)
                _full_result: dict[str, Any] = {
                    "files": files_data,
                    "total_files": len(graph),
                }
                if _scope_files is not None:
                    _full_result["scope"] = scope
                return _tc.finish(_full_result)
            else:
                # Summary mode (default) — compact overview
                # Top files by include count (most-included)
                include_counts: dict[str, int] = {}
                for incs in graph.values():
                    for inc in incs:
                        include_counts[inc] = include_counts.get(inc, 0) + 1
                most_included = sorted(
                    include_counts.items(), key=lambda x: x[1], reverse=True
                )[:limit]

                _summary_result: dict[str, Any] = {
                    "total_files": len(graph),
                    "entry_points": entry_points[:limit],
                    "entry_point_count": len(entry_points),
                    "most_included": [
                        {"module": mod, "included_by_count": cnt}
                        for mod, cnt in most_included
                    ],
                    "detail": "summary",
                    "hint": "Use detail='full' for the complete graph.",
                }
                if _scope_files is not None:
                    _summary_result["scope"] = scope
                return _tc.finish(_summary_result)

    @mcp.tool()
    @safe_tool
    async def ivy_capabilities() -> dict:
        """Report which Ivy CLI tools are available on PATH, MCP tools, and staging health."""
        from ivy_lsp.tools import get_tool_metadata

        _tc = ToolTraceContext("ivy_capabilities", {})
        result: dict[str, Any] = {
            "success": True,
            "cli_tools": {
                "ivy_check": shutil.which("ivy_check") is not None,
                "ivyc": shutil.which("ivyc") is not None,
                "ivy_show": shutil.which("ivy_show") is not None,
            },
            # Also report legacy flat keys for backward compat
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
            "mcp_tools": {
                name: {
                    "category": meta.get("category", ""),
                    "cost": meta.get("cost", ""),
                }
                for name, meta in get_tool_metadata().items()
            },
            "mcp_tool_count": len(get_tool_metadata()),
        }
        if ctx.include_resolver is not None and hasattr(
            ctx.include_resolver, "staging_health"
        ):
            try:
                result["staging_health"] = ctx.include_resolver.staging_health()
            except Exception:
                pass  # staging health is optional
        return _tc.finish(result)

    @mcp.tool()
    @safe_tool
    async def ivy_scope(relative_path: str) -> dict:
        """Return endpoint mirror scope info for an Ivy file.

        Reports (all dynamically computed from the include graph):
        - Endpoint mirror test(s) for the given file
        - Tester role (client/server/mim)
        - Scope partition (if partitioned staging is active)
        - Full include closure
        - Collision report (basenames with cross-partition conflicts)

        Args:
            relative_path: Relative path to the .ivy file.
        """
        logger.debug(
            "[ivy_scope] workspace=%s, args=%r",
            ctx.root,
            {"relative_path": relative_path},
        )
        _tc = ToolTraceContext("ivy_scope", {"relative_path": relative_path})
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return _tc.finish(error_response(str(exc)))
        if not os.path.isfile(abs_path):
            return _tc.finish(error_response(f"File not found: {relative_path}"))

        result: dict[str, Any] = {
            "file": relative_path,
            "abs_path": abs_path,
        }

        # Try to get scope info from the requirement graph
        req_graph = ctx.get_req_graph()
        if req_graph is not None and hasattr(req_graph, "get_tests_for_file"):
            tests = sorted(req_graph.get_tests_for_file(abs_path))
            result["endpoint_mirrors"] = [os.path.relpath(t, ctx.root) for t in tests]
            result["endpoint_mirror_count"] = len(tests)

            # Get scope details from the first mirror
            if tests:
                scope = req_graph.get_test_scope(tests[0])
                if scope is not None:
                    result["tester_role"] = scope.tester_role
                    result["include_closure_size"] = len(scope.include_closure)
                    result["include_closure"] = sorted(
                        os.path.relpath(f, ctx.root) for f in scope.include_closure
                    )
                    result["exported_actions"] = sorted(scope.exported_actions)
                    result["imported_actions"] = sorted(scope.imported_actions)

            # Multiple mirrors — show all roles
            if len(tests) > 1:
                roles = {}
                for t in tests:
                    sc = req_graph.get_test_scope(t)
                    if sc:
                        roles[os.path.relpath(t, ctx.root)] = sc.tester_role
                result["mirror_roles"] = roles
        else:
            result["endpoint_mirrors"] = []
            result["endpoint_mirror_count"] = 0

        # Partition info
        if ctx.staging_dir:
            resolve_cb = ctx.make_resolve_callback()
            # Check if the resolver has partition info
            if hasattr(resolve_cb, "__self__") and hasattr(
                resolve_cb.__self__, "get_partition_for_file"
            ):
                resolver = resolve_cb.__self__
                partition = resolver.get_partition_for_file(abs_path)
                result["partition"] = partition
                if resolver.collision_map:
                    # Report collisions relevant to this file
                    basename = os.path.basename(abs_path)
                    if basename in resolver.collision_map:
                        result["collision_report"] = {
                            "basename": basename,
                            "variants": [
                                os.path.relpath(p, ctx.root)
                                for p in resolver.collision_map[basename]
                            ],
                        }

        return _tc.finish(result)

    @mcp.tool()
    @safe_tool
    async def ivy_health_check() -> dict:
        """Server health check: uptime, cache status, tool metrics, model status.

        Returns server health information including:
        - Server uptime and memory usage
        - Verification cache status (entries, hit rate)
        - Model build status
        - Tool call metrics (count, avg duration, error count per tool)
        """
        from ivy_lsp.tools import get_tool_metrics

        _tc = ToolTraceContext("ivy_health_check", {})

        health: dict[str, Any] = {
            "success": True,
            "server": {
                "workspace": ctx.root,
                "staging_dir": ctx.staging_dir,
            },
            "model_status": ctx.get_model_status(),
        }

        # Verification cache summary
        if hasattr(ctx, "get_verify_cache_summary"):
            try:
                health["verification_cache"] = ctx.get_verify_cache_summary()
            except Exception as exc:
                health["verification_cache"] = {"error": str(exc)}

        # Tool metrics
        metrics = get_tool_metrics()
        tool_stats = {}
        for tool_name, m in metrics.items():
            avg_duration = (
                round(m.total_duration / m.call_count, 2) if m.call_count > 0 else 0
            )
            tool_stats[tool_name] = {
                "call_count": m.call_count,
                "avg_duration_seconds": avg_duration,
                "error_count": m.error_count,
                "timeout_count": m.timeout_count,
            }
        health["tool_metrics"] = tool_stats

        # Capabilities
        health["capabilities"] = {
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
        }

        # File counts
        try:
            ivy_files = ctx.find_ivy_files(ctx.root)
            health["workspace_files"] = len(ivy_files)
        except Exception:
            health["workspace_files"] = -1

        return _tc.finish(health)

    @mcp.tool()
    @safe_tool
    async def ivy_index(
        protocol: str = "all",
        fast: bool = False,
        status: bool = False,
    ) -> dict:
        """Build or check the offline .ivy-index/ for a protocol.

        Args:
            protocol: Protocol name (e.g. "quic") or "all" for all protocols.
            fast: Use Tier 2 (lexer) only, skip full parser.
            status: Check staleness without rebuilding.

        Returns:
            Build summary or staleness report.
        """
        _tc = ToolTraceContext("ivy_index")

        try:
            from ivy_lsp.index_builder import IndexBuilder
            from ivy_lsp.workspace_detection import detect_ivy_workspace
        except ImportError as exc:
            return _tc.finish(
                error_response(
                    f"Index builder not available: {exc}",
                    "ivy_index",
                )
            )

        ws_config = detect_ivy_workspace(start_dir=ctx.root)

        if status:
            builder = IndexBuilder(ctx.root, ws_config)
            if protocol == "all":
                results = []
                for proto_dir in sorted(
                    __import__("glob").glob(
                        os.path.join(ctx.root, "protocol-testing", "*")
                    )
                ):
                    if os.path.isdir(proto_dir):
                        results.append(builder.check_status(proto_dir))
                return _tc.finish({"status_reports": results})
            else:
                proto_dir = os.path.join(ctx.root, "protocol-testing", protocol)
                return _tc.finish(builder.check_status(proto_dir))

        builder = IndexBuilder(ctx.root, ws_config, fast=fast)
        loop = asyncio.get_running_loop()

        if protocol == "all":
            summaries = await loop.run_in_executor(None, builder.build_all)
        else:
            proto_dir = os.path.join(ctx.root, "protocol-testing", protocol)
            summary = await loop.run_in_executor(
                None, builder.build_protocol, proto_dir
            )
            summaries = [summary]

        # Reload workspace context after build
        ws_ctx = getattr(ctx, "workspace_context", None)
        if ws_ctx is not None:
            try:
                from ivy_lsp.workspace_context import WorkspaceContext

                ctx.workspace_context = WorkspaceContext.load(ctx.root)
                logger.info("Reloaded WorkspaceContext after index build")
            except Exception:
                logger.debug("WorkspaceContext reload failed", exc_info=True)

        return _tc.finish({"summaries": summaries})
