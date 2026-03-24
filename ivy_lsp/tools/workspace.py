"""Workspace management tool: ivy_workspace.

Allows switching the active protocol workspace at runtime, which controls
scoped symbol resolution, diagnostic filtering, and coverage tools.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from ivy_lsp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)


def register_workspace_tools(mcp: Any, ctx: Any) -> None:
    """Register workspace management MCP tools."""

    @mcp.tool()
    @safe_tool
    async def ivy_workspace(
        action: Literal["set", "get", "list", "clear"],
        target: str | None = None,
        roles: str | None = None,
    ) -> dict:
        """Manage the active Ivy protocol workspace.

        Controls which protocol layers are "in scope" for verification,
        diagnostics, and coverage tools.

        Args:
            action: One of "set", "get", "list", or "clear".
            target: For action="set": workspace group name (e.g. "quic")
                or a specific .ivy test file path.
            roles: For action="set": comma-separated role filter
                (e.g. "client,server") to narrow scope to a role pair.
        """
        if action == "set":
            return await _handle_set(ctx, target, roles)
        elif action == "get":
            return _handle_get(ctx)
        elif action == "list":
            return _handle_list(ctx)
        elif action == "clear":
            return _handle_clear(ctx)
        else:
            return error_response(
                f"Unknown action '{action}'. " "Valid actions: set, get, list, clear."
            )


async def _handle_set(ctx: Any, target: str | None, roles: str | None) -> dict:
    """Handle action='set' for ivy_workspace."""
    from ivy_lsp.active_workspace import ActiveWorkspace

    if target is None:
        return error_response("action='set' requires a 'target' parameter.")

    # If target ends with .ivy, use from_test_file
    if target.endswith(".ivy"):
        file_to_layer = {}
        if ctx.include_resolver is not None and hasattr(
            ctx.include_resolver, "_file_to_layer"
        ):
            file_to_layer = ctx.include_resolver._file_to_layer

        ws = ActiveWorkspace.from_test_file(
            test_file=target,
            file_to_layer=file_to_layer,
            workspace_groups=ctx.workspace_groups,
        )
    else:
        # Look up target in workspace_groups
        groups = ctx.workspace_groups
        if target not in groups:
            available = sorted(groups.keys()) if groups else []
            return error_response(
                f"Unknown workspace group '{target}'. "
                f"Available groups: {', '.join(available) if available else '(none)'}."
            )

        layers = set(groups[target])
        granularity = "protocol"
        active_tests: list[str] = []

        # If roles provided, narrow to role_pair granularity
        if roles:
            granularity = "role_pair"
            # Filter active_tests by role names (future: use role metadata)
            role_list = [r.strip() for r in roles.split(",") if r.strip()]
            # Role filtering of tests would use file naming conventions;
            # for now we store the role filter in active_tests as metadata
            active_tests = [f"role:{r}" for r in role_list]

        ws = ActiveWorkspace(
            active_group=target,
            active_layers=layers,
            active_tests=active_tests,
            granularity=granularity,
            set_by="explicit",
        )

    # Persist state
    state_path = os.path.join(ctx.root, ".ivy-workspace-state.json")
    ws.save(state_path)

    # In-process mutation: update resolver active-layer filter (instant,
    # no filesystem I/O — just sets a filter flag).
    if ctx.include_resolver is not None and hasattr(
        ctx.include_resolver, "set_active_workspace"
    ):
        ctx.include_resolver.set_active_workspace(ws.active_layers)

    # Store on context
    ctx.active_workspace = ws

    # Compute files_in_scope for the response
    files_in_scope = 0
    if ctx.include_resolver is not None and hasattr(
        ctx.include_resolver, "_file_to_layer"
    ):
        file_to_layer = ctx.include_resolver._file_to_layer
        files_in_scope = sum(
            1 for layer in file_to_layer.values() if layer in ws.active_layers
        )

    return {
        "status": "ok",
        "active_group": ws.active_group,
        "active_layers": sorted(ws.active_layers),
        "granularity": ws.granularity,
        "files_in_scope": files_in_scope,
    }


def _handle_get(ctx: Any) -> dict:
    """Handle action='get' for ivy_workspace."""
    ws = ctx.active_workspace
    if ws is None:
        from ivy_lsp.active_workspace import ActiveWorkspace

        ws = ActiveWorkspace.cleared()

    return {
        "status": "ok",
        "active_group": ws.active_group,
        "active_layers": sorted(ws.active_layers),
        "active_tests": ws.active_tests,
        "granularity": ws.granularity,
        "set_by": ws.set_by,
    }


def _handle_list(ctx: Any) -> dict:
    """Handle action='list' for ivy_workspace."""
    ws = ctx.active_workspace
    active_group = ws.active_group if ws is not None else None

    return {
        "status": "ok",
        "active_group": active_group,
        "available_groups": dict(ctx.workspace_groups),
    }


def _handle_clear(ctx: Any) -> dict:
    """Handle action='clear' for ivy_workspace."""
    from ivy_lsp.active_workspace import ActiveWorkspace

    ws = ActiveWorkspace.cleared()

    # Persist cleared state
    state_path = os.path.join(ctx.root, ".ivy-workspace-state.json")
    ws.save(state_path)

    # In-process mutation: clear resolver filter
    if ctx.include_resolver is not None and hasattr(
        ctx.include_resolver, "set_active_workspace"
    ):
        ctx.include_resolver.set_active_workspace(set())

    # Store on context
    ctx.active_workspace = ws

    return {
        "status": "ok",
        "active_group": ws.active_group,
        "active_layers": sorted(ws.active_layers),
        "granularity": ws.granularity,
    }
