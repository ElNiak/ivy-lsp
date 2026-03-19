"""Visualization tools: ivy_visualize, ivy_model_summary.

Consolidated from the original six tools:
- ivy_action_dependency_graph, ivy_state_machine_view, ivy_layered_overview
  -> ivy_visualize
- ivy_model_summary + ivy_action_requirements -> ivy_model_summary
- ivy_coverage_gaps -> moved to traceability.py (ivy_coverage mode="gaps")
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from ivy_lsp.debug_trace import ToolTraceContext
from ivy_lsp.tools import error_response, safe_tool

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
    ) -> str:
        """Get requirements organized by action boundaries."""
        from ivy_lsp.features.visualization import handle_action_requirements

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if action_name:
            params["actionName"] = action_name
        if file_path:
            try:
                params["filePath"] = ctx.validate_path(file_path)
            except ValueError as exc:
                return error_response(str(exc))
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return json.dumps(handle_action_requirements(server_proxy, params))

    async def _ivy_model_summary_logic(
        test_file: str | None = None,
        protocol: str | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Get per-action requirement counts, state variable usage, and RFC coverage."""
        from ivy_lsp.features.visualization import handle_model_summary_table

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
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

        return json.dumps(result)

    async def _ivy_action_dependency_graph(
        test_file: str | None = None,
        include_state_vars: bool = False,
        protocol: str | None = None,
    ) -> str:
        """Return the action dependency graph showing shared-state relationships."""
        from ivy_lsp.features.visualization import handle_action_dependency_graph

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if include_state_vars:
            params["includeStateVars"] = True
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_action_dependency_graph(server_proxy, params))

    async def _ivy_state_machine_view(
        test_file: str | None = None,
        state_var_filter: str | None = None,
        protocol: str | None = None,
    ) -> str:
        """Return a state-machine view of the Ivy specification."""
        from ivy_lsp.features.visualization import handle_state_machine_view

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if state_var_filter:
            params["stateVarFilter"] = state_var_filter
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_state_machine_view(server_proxy, params))

    async def _ivy_layered_overview(
        test_file: str | None = None,
        group_by: str = "file",
        protocol: str | None = None,
    ) -> str:
        """Get a layered overview of the Ivy model organized by file or module."""
        from ivy_lsp.features.visualization import handle_layered_overview

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return error_response(str(exc))
        if group_by:
            params["groupBy"] = group_by
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_layered_overview(server_proxy, params))

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

    @mcp.tool()
    @safe_tool
    async def ivy_visualize(
        view: Literal["dependencies", "state_machine", "layers"] = "dependencies",
        test_file: str | None = None,
        protocol: str | None = None,
        include_state_vars: bool = False,
        state_var_filter: str | None = None,
        group_by: str = "file",
        max_items: int = 50,
    ) -> str:
        """Unified model visualization tool.

        Combines dependency graph, state machine view, and layered overview
        into a single tool with view-based dispatch.

        Args:
            view: Visualization view.
                - "dependencies": Action dependency graph showing shared-state
                  relationships. Actions are nodes; edges represent shared
                  state variables (default).
                - "state_machine": State-machine view where state variables
                  are state nodes, actions are transitions (via READS/WRITES),
                  and guards are require/assume clauses.
                - "layers": Layered overview of the Ivy model organized by
                  file or module.
            test_file: Optional test file to scope the analysis to
                (relative path). Used by all views.
            protocol: Protocol name to scope results. Used by all views.
            include_state_vars: When True, include state variable nodes and
                their reads/writes edges in the graph. Used by
                view="dependencies".
            state_var_filter: Optional state variable name to restrict the
                view to. Used by view="state_machine".
            group_by: Grouping strategy: "file" (default) or "module".
                Used by view="layers".
            max_items: Maximum number of items to return in the response
                (default: 50). When the result is truncated, the response
                includes ``"truncated": true`` and ``"total": N``.
                Set to 0 for unlimited.
        """
        _tc = ToolTraceContext(
            "ivy_visualize",
            {"view": view, "test_file": test_file, "protocol": protocol},
        )
        _valid_views = {"dependencies", "state_machine", "layers"}
        if view not in _valid_views:
            return _tc.finish(
                error_response(
                    f"Unknown view '{view}'. Valid views: {sorted(_valid_views)}"
                )
            )
        if view == "state_machine":
            raw = await _ivy_state_machine_view(test_file, state_var_filter, protocol)
            result = json.loads(raw)
            result = _apply_max_items(result, "states", max_items)
            result = _apply_max_items(result, "transitions", max_items)
            return _tc.finish(json.dumps(result))
        elif view == "layers":
            raw = await _ivy_layered_overview(test_file, group_by, protocol)
            result = json.loads(raw)
            result = _apply_max_items(result, "layers", max_items)
            return _tc.finish(json.dumps(result))
        else:  # default: dependencies
            raw = await _ivy_action_dependency_graph(
                test_file, include_state_vars, protocol
            )
            result = json.loads(raw)
            result = _apply_max_items(result, "nodes", max_items)
            return _tc.finish(json.dumps(result))

    @mcp.tool()
    @safe_tool
    async def ivy_model_summary(
        detail: Literal["summary", "requirements"] = "summary",
        test_file: str | None = None,
        protocol: str | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
        action_name: str | None = None,
        file_path: str | None = None,
        offset: int = 0,
        max_items: int = 50,
    ) -> str:
        """Unified model summary and action requirements tool.

        Combines per-action summary statistics with detailed action
        requirement inspection.

        Args:
            detail: Detail level.
                - "summary": Per-action requirement counts, state variable
                  usage, and RFC coverage (default). Returns one row per
                  action.
                - "requirements": Requirements organized by action boundaries
                  (before/after monitors) with their temporal position,
                  kind, and state variables they read or write.
            test_file: Optional test file to scope the analysis to
                (relative path). Used by both detail levels.
            protocol: Protocol name (e.g., "quic") to scope results.
                Used by both detail levels.
            sort_by: Sort rows by field (e.g., "requirement_count", "name").
                Used by detail="summary".
            limit: Maximum number of rows/actions to return. Used by both
                detail levels. Takes priority over max_items when set.
            action_name: Specific action to query. Used by
                detail="requirements". If omitted, returns all actions.
            file_path: Scope to actions defined in this file (relative path).
                Used by detail="requirements".
            offset: Number of actions to skip (default: 0). Used by
                detail="requirements".
            max_items: Maximum number of items to return when limit is not
                set (default: 50). Set to 0 for unlimited. When the result
                is truncated, the response includes ``"truncated": true``
                and ``"total": N``.
        """
        # Use limit if explicitly set, otherwise fall back to max_items
        effective_limit = (
            limit if limit is not None else (max_items if max_items > 0 else None)
        )

        _tc = ToolTraceContext(
            "ivy_model_summary",
            {"detail": detail, "test_file": test_file, "protocol": protocol},
        )
        _valid_details = {"summary", "requirements"}
        if detail not in _valid_details:
            return _tc.finish(
                error_response(
                    f"Unknown detail '{detail}'. Valid details: {sorted(_valid_details)}"
                )
            )
        if detail == "requirements":
            return _tc.finish(
                await _ivy_action_requirements(
                    action_name, file_path, test_file, protocol, offset, effective_limit
                )
            )
        else:  # default: summary
            return _tc.finish(
                await _ivy_model_summary_logic(
                    test_file, protocol, sort_by, effective_limit
                )
            )
