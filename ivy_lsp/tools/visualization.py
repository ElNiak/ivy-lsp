"""Visualization tools: ivy_action_requirements, ivy_model_summary,
ivy_coverage_gaps, ivy_action_dependency_graph, ivy_state_machine_view,
ivy_layered_overview.
"""

from __future__ import annotations

import json
from typing import Any


def register_visualization_tools(mcp: Any, ctx: Any) -> None:
    """Register visualization-related MCP tools."""

    @mcp.tool()
    async def ivy_action_requirements(
        action_name: str | None = None,
        file_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Get requirements organized by action boundaries (before/after monitors).

        Returns requirements grouped by the action they monitor, their temporal
        position (before/after), kind (require/ensure/assume/assert), and the
        state variables they read or write.

        Args:
            action_name: Specific action to query. If omitted, returns all actions.
            file_path: Scope to actions defined in this file (relative path).
            test_file: Optional test file to scope the analysis to (relative path).
            protocol: Protocol name (e.g., "quic") to scope results.
            offset: Number of actions to skip (default: 0).
            limit: Maximum number of actions to return. If omitted, returns all.
        """
        from ivy_lsp.features.visualization import handle_action_requirements

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if action_name:
            params["actionName"] = action_name
        if file_path:
            try:
                params["filePath"] = ctx.validate_path(file_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return json.dumps(handle_action_requirements(server_proxy, params))

    @mcp.tool()
    async def ivy_model_summary(
        test_file: str | None = None,
        protocol: str | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Get per-action requirement counts, state variable usage, and RFC coverage.

        Returns one row per action with counts of before/after requirements by kind,
        state variables read/written, and RFC bracket tags covered.

        Args:
            test_file: Optional test file to scope the summary to (relative path).
            protocol: Protocol name (e.g., "quic") to scope results.
            sort_by: Sort rows by field (e.g., "requirement_count", "name").
                Defaults to original order.
            limit: Maximum number of rows to return. If omitted, returns all.
        """
        from ivy_lsp.features.visualization import handle_model_summary_table

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        result = handle_model_summary_table(server_proxy, params)

        # P1: Post-process rows with sort_by and limit
        rows = result.get("rows", [])
        if sort_by == "requirement_count":
            rows.sort(
                key=lambda r: sum(r.get("counts", {}).values()) if isinstance(r.get("counts"), dict) else 0,
                reverse=True,
            )
        elif sort_by == "name":
            rows.sort(key=lambda r: r.get("actionName", ""))

        if limit is not None and limit > 0:
            result["rows"] = rows[:limit]
            result["hasMore"] = len(rows) > limit
        else:
            result["rows"] = rows

        return json.dumps(result)

    @mcp.tool()
    async def ivy_coverage_gaps(
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> str:
        """Identify coverage gaps: unguarded state vars, uncovered RFC requirements.

        Finds state variables written but never guarded, RFC sections with no
        covering assertions, and requirements whose monitored action does not exist.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
        """
        from ivy_lsp.features.visualization import handle_coverage_gaps

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_coverage_gaps(server_proxy, params))

    @mcp.tool()
    async def ivy_action_dependency_graph(
        test_file: str | None = None,
        include_state_vars: bool = False,
        protocol: str | None = None,
    ) -> str:
        """Return the action dependency graph showing shared-state relationships.

        Actions are nodes; edges represent shared state variables (action A writes
        a variable that action B reads). Optionally includes state variable nodes
        with explicit reads/writes edges.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
            include_state_vars: When True, include state variable nodes and their
                reads/writes edges in the graph.
        """
        from ivy_lsp.features.visualization import handle_action_dependency_graph

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if include_state_vars:
            params["includeStateVars"] = True
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_action_dependency_graph(server_proxy, params))

    @mcp.tool()
    async def ivy_state_machine_view(
        test_file: str | None = None,
        state_var_filter: str | None = None,
        protocol: str | None = None,
    ) -> str:
        """Return a state-machine view of the Ivy specification.

        Models the specification as a state machine where state variables are
        state nodes, actions are transitions between them (via READS/WRITES),
        and guards are require/assume clauses on the action's monitors.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
            state_var_filter: Optional state variable name to restrict the view to.
        """
        from ivy_lsp.features.visualization import handle_state_machine_view

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if state_var_filter:
            params["stateVarFilter"] = state_var_filter
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_state_machine_view(server_proxy, params))

    @mcp.tool()
    async def ivy_layered_overview(
        test_file: str | None = None,
        group_by: str = "file",
        protocol: str | None = None,
    ) -> str:
        """Get a layered overview of the Ivy model organized by file or module.

        Args:
            test_file: Optional test file to scope the overview to (relative path).
            group_by: Grouping strategy: "file" (default) or "module".
        """
        from ivy_lsp.features.visualization import handle_layered_overview

        server_proxy = await ctx.make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = ctx.validate_path(test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if group_by:
            params["groupBy"] = group_by
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_layered_overview(server_proxy, params))
