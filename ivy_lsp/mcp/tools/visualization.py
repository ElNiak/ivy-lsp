"""Visualization tools: ivy_visualize.

Consolidated from the original six tools:
- ivy_action_dependency_graph, ivy_state_machine_view, ivy_layered_overview
  -> ivy_visualize (views: dependencies, state_machine, layers)
- ivy_model_summary + ivy_action_requirements
  -> ivy_visualize (views: summary, requirements)
- ivy_coverage_gaps -> moved to traceability.py (ivy_coverage mode="gaps")
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import error_response, safe_tool
from ivy_lsp.mcp.tools._helpers import build_viz_params

logger = logging.getLogger(__name__)


def register_visualization_tools(mcp: Any, ctx: Any) -> None:
    """Register visualization-related MCP tools."""
    # ------------------------------------------------------------------
    # Private helpers (former standalone tool bodies)
    # ------------------------------------------------------------------

    async def _ivy_action_requirements(
        action_name: str | None = None,
        file_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        """Get requirements organized by action boundaries."""
        from ivy_lsp.lsp.visualization import handle_action_requirements

        params, err = build_viz_params(
            ctx, file_path=file_path, test_file=test_file, protocol=protocol
        )
        if err:
            return err
        server_proxy = await ctx.make_viz_server_proxy()
        if action_name:
            params["actionName"] = action_name
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return handle_action_requirements(server_proxy, params)

    async def _ivy_model_summary_logic(
        test_file: str | None = None,
        protocol: str | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Get per-action requirement counts, state variable usage, and RFC coverage."""
        from ivy_lsp.lsp.visualization import handle_model_summary_table

        params, err = build_viz_params(ctx, test_file=test_file, protocol=protocol)
        if err:
            return err
        server_proxy = await ctx.make_viz_server_proxy()
        result = handle_model_summary_table(server_proxy, params)

        # P1: Post-process rows with sort_by and limit
        rows = result.get("rows", [])
        if sort_by == "requirement_count":
            rows.sort(
                key=lambda r: (
                    sum(r.get("counts", {}).values())
                    if isinstance(r.get("counts"), dict)
                    else 0
                ),
                reverse=True,
            )
        elif sort_by == "name":
            rows.sort(key=lambda r: r.get("actionName", ""))

        if limit is not None and limit > 0:
            total_rows = len(rows)
            result["rows"] = rows[:limit]
            if total_rows > limit:
                result["truncated"] = True
                result["total"] = total_rows
            result["hasMore"] = total_rows > limit
        else:
            result["rows"] = rows

        return result

    async def _ivy_action_dependency_graph(
        test_file: str | None = None,
        include_state_vars: bool = False,
        protocol: str | None = None,
    ) -> dict:
        """Return the action dependency graph showing shared-state relationships."""
        from ivy_lsp.lsp.viz_graphs import handle_action_dependency_graph

        params, err = build_viz_params(ctx, test_file=test_file, protocol=protocol)
        if err:
            return err
        server_proxy = await ctx.make_viz_server_proxy()
        if include_state_vars:
            params["includeStateVars"] = True
        return handle_action_dependency_graph(server_proxy, params)

    async def _ivy_state_machine_view(
        test_file: str | None = None,
        state_var_filter: str | None = None,
        protocol: str | None = None,
    ) -> dict:
        """Return a state-machine view of the Ivy specification."""
        from ivy_lsp.lsp.viz_graphs import handle_state_machine_view

        params, err = build_viz_params(ctx, test_file=test_file, protocol=protocol)
        if err:
            return err
        server_proxy = await ctx.make_viz_server_proxy()
        if state_var_filter:
            params["stateVarFilter"] = state_var_filter
        return handle_state_machine_view(server_proxy, params)

    async def _ivy_layered_overview(
        test_file: str | None = None,
        group_by: str = "file",
        protocol: str | None = None,
    ) -> dict:
        """Get a layered overview of the Ivy model organized by file or module."""
        from ivy_lsp.lsp.viz_graphs import handle_layered_overview

        params, err = build_viz_params(ctx, test_file=test_file, protocol=protocol)
        if err:
            return err
        server_proxy = await ctx.make_viz_server_proxy()
        if group_by:
            params["groupBy"] = group_by
        return handle_layered_overview(server_proxy, params)

    # ------------------------------------------------------------------
    # Public MCP tools
    # ------------------------------------------------------------------

    def _apply_max_items(result: dict, list_key: str, max_items: int) -> dict:
        """Apply max_items limit to a result dict's main list field."""
        if max_items <= 0:
            return result
        items = result.get(list_key, [])
        if len(items) > max_items:
            result["total"] = len(items)
            result[list_key] = items[:max_items]
            result["truncated"] = True
        return result

    async def _model_summary_impl(
        detail: str,
        test_file: str | None,
        protocol: str | None,
        sort_by: str | None,
        limit: int | None,
        action_name: str | None,
        file_path: str | None,
        offset: int,
        max_items: int,
    ) -> dict:
        """Dispatch helper for summary/requirements views."""
        effective_limit = (
            limit if limit is not None else (max_items if max_items > 0 else None)
        )
        if detail == "requirements":
            return await _ivy_action_requirements(
                action_name, file_path, test_file, protocol, offset, effective_limit
            )
        else:  # summary
            return await _ivy_model_summary_logic(
                test_file, protocol, sort_by, effective_limit
            )

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_visualize(
        view: Literal[
            "dependencies", "state_machine", "layers", "summary", "requirements"
        ] = "dependencies",
        test_file: str | None = None,
        protocol: str | None = None,
        include_state_vars: bool = False,
        state_var_filter: str | None = None,
        group_by: str = "file",
        max_items: int = 50,
        sort_by: str | None = None,
        action_name: str | None = None,
        file_path: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        """Visualizes Ivy model structure: dependency graphs, state machines, layers, and action summaries.

        Views:
        - dependencies: file/module dependency graph → {nodes: [{id, file, type}], edges: [{source, target}]}
        - state_machine: state transitions per protocol object → {states[], transitions: [{from, to, action, guard?}]}
        - layers: layered architecture overview → {layers: [{name, files[], role}]}
        - summary: per-action requirement counts and state variable usage → {actions: [{name, requirement_count, state_vars[]}]}
        - requirements: requirements organized by action boundaries → {actions: [{name, before[], after[], monitors[]}]}

        Run ivy_index first. For RFC coverage stats (not per-action), use ivy_coverage mode=stats instead.
        """
        _tc = ToolTraceContext(
            "ivy_visualize",
            {"view": view, "test_file": test_file, "protocol": protocol},
        )
        _valid_views = {
            "dependencies",
            "state_machine",
            "layers",
            "summary",
            "requirements",
        }
        if view not in _valid_views:
            return _tc.finish(
                error_response(
                    f"Unknown view '{view}'. Valid views: {sorted(_valid_views)}"
                )
            )
        if view == "state_machine":
            result = await _ivy_state_machine_view(
                test_file, state_var_filter, protocol
            )
            result = _apply_max_items(result, "states", max_items)
            result = _apply_max_items(result, "transitions", max_items)
        elif view == "layers":
            result = await _ivy_layered_overview(test_file, group_by, protocol)
            result = _apply_max_items(result, "layers", max_items)
        elif view in ("summary", "requirements"):
            result = await _model_summary_impl(
                detail=view,
                test_file=test_file,
                protocol=protocol,
                sort_by=sort_by,
                limit=limit,
                action_name=action_name,
                file_path=file_path,
                offset=offset,
                max_items=max_items,
            )
        else:  # default: dependencies
            result = await _ivy_action_dependency_graph(
                test_file, include_state_vars, protocol
            )
            result = _apply_max_items(result, "nodes", max_items)
        if isinstance(result, dict):
            result["view"] = view
        return _tc.finish(result)
