"""Workspace management tool: ivy_workspace.

Allows switching the active protocol workspace at runtime, which controls
scoped symbol resolution, diagnostic filtering, and coverage tools.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from ivy_lsp.mcp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)


def register_workspace_tools(mcp: Any, ctx: Any) -> None:
    """Register workspace management MCP tools."""

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_workspace(
        action: Literal["set", "get", "list", "clear"],
        target: str | None = None,
        roles: str | None = None,
    ) -> dict:
        """Controls which protocol layers are in scope for verification, diagnostics, and coverage tools.

        Responses are trimmed to novel information only (echoed input params
        are omitted). Success paths return ``{"success": True, ...}``; failures
        go through ``error_response()``.

        Modes (return shapes):
        - set: {success, active_layers, files_in_scope}
        - get: {success, active_group, active_layers, active_tests, granularity, set_by}
        - list: {success, active_group, available_groups}
        - clear: {success}

        Set workspace before running ivy_coverage or ivy_diagnostics to scope results to a specific test.

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
    from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace

    if target is None:
        return error_response("action='set' requires a 'target' parameter.")

    # If target ends with .ivy, use from_test_file
    if target.endswith(".ivy"):
        # `roles` narrows a workspace group to a role-pair scope; it has no
        # well-defined semantics when `target` is already a single test file
        # (from_test_file scopes to granularity="test" with active_tests=[
        # test_file]). Reject the combination loudly rather than silently
        # dropping `roles` and returning workspace-wide diagnostics — that
        # silent drop misled callers expecting role-pair scoping.
        if roles:
            return error_response(
                "ivy_workspace(action='set', target=<.ivy file>, roles=...) "
                "is not supported: 'roles' narrows a workspace group, but a "
                f"specific test file ({target!r}) is already a single-file "
                "scope. Either omit 'roles' to scope to the test file's "
                "owning group, or pass target=<group_name> with 'roles' to "
                "narrow the group."
            )

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

    # Invalidate basename cache so it rebuilds with new layer scope
    if hasattr(ctx, "_basename_cache_invalidate"):
        ctx._basename_cache_invalidate()

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
        "success": True,
        "active_layers": sorted(ws.active_layers),
        "files_in_scope": files_in_scope,
    }


def _handle_get(ctx: Any) -> dict:
    """Handle action='get' for ivy_workspace."""
    ws = ctx.active_workspace
    if ws is None:
        from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace

        # Fall back to persisted state before returning cleared.
        state_path = os.path.join(ctx.root, ".ivy-workspace-state.json")
        if os.path.exists(state_path):
            try:
                ws = ActiveWorkspace.load(state_path)
            except Exception:
                logger.warning(
                    "Failed to load persisted workspace state", exc_info=True
                )
                ws = None
            if ws is not None and ws.is_set():
                ctx.active_workspace = ws  # Restore in-memory state
                if ctx.include_resolver is not None and hasattr(
                    ctx.include_resolver, "set_active_workspace"
                ):
                    ctx.include_resolver.set_active_workspace(ws.active_layers)
                if hasattr(ctx, "_basename_cache_invalidate"):
                    ctx._basename_cache_invalidate()
                logger.info(
                    "Restored workspace from persisted state: %s", ws.active_group
                )
        if ws is None or not ws.is_set():
            ws = ActiveWorkspace.cleared()

    return {
        "success": True,
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
        "success": True,
        "active_group": active_group,
        "available_groups": dict(ctx.workspace_groups),
    }


def _handle_clear(ctx: Any) -> dict:
    """Handle action='clear' for ivy_workspace."""
    from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace

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

    return {"success": True}
