"""MCP server startup and app creation."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from typing import Any

if sys.version_info >= (3, 11):
    _BaseExceptionGroup = BaseExceptionGroup
else:
    from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup

from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import LogCategory, log_phase, timed_phase
from ivy_lsp.mcp.server import McpServerState, _sidecar_monitor, create_mcp_app

logger = logging.getLogger(__name__)


def _restore_workspace_at_boot(ctx: Any, root: str) -> None:
    """Restore persisted workspace state before prewarm.

    Mirrors tools/workspace.py:_handle_get fall-back-restore branch so the
    model build sees the filtered resolver scope. Without this, prewarm
    iterates the full global scope (e.g. 643 .ivy files) and times out.
    """
    from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace

    state_path = os.path.join(root, ".ivy-workspace-state.json")
    if not os.path.exists(state_path):
        return
    try:
        ws = ActiveWorkspace.load(state_path)
    except Exception:
        logger.warning(
            "Failed to restore persisted workspace at boot",
            exc_info=True,
        )
        return
    if not ws.is_set():
        return
    ctx.active_workspace = ws
    if ctx.include_resolver is not None and hasattr(
        ctx.include_resolver, "set_active_workspace"
    ):
        ctx.include_resolver.set_active_workspace(ws.active_layers)
    logger.info(
        "[MCP-WORKSPACE] Restored workspace at boot: %s (%d layers)",
        ws.active_group,
        len(ws.active_layers),
    )


def start_mcp(
    workspace_root: str | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
    docker_image: str | None = None,
    base_path: str | None = None,
    staging_dir: str | None = None,
    _return_app: bool = False,
    ws_config: Any = None,
) -> Any:
    """Start the MCP server exposing Ivy tools.

    Args:
        workspace_root: Root directory for the workspace.
        semantic_model: Optional SemanticModel for shared-process mode.
        requirement_graph: Optional RequirementGraph (or ScopedRequirementModel)
            for visualization tools. When provided, enables
            ivy_visualize (view="summary"|"requirements") and
            ivy_coverage (mode="gaps") MCP tools.
        docker_image: Docker image for Ivy compilation (e.g. "panther_ivy:latest").
            When set, compilation tools use Docker instead of native subprocess.
        base_path: Base protocol-testing path for compile command generation.
        staging_dir: Optional staging directory for include resolution.
            When set, ivy_verify/ivy_compile/ivy_model_info resolve paths
            through this directory (flat symlinks for CWD-relative includes).
        _return_app: Internal flag for testing. When True, returns the FastMCP
            instance without starting the server.
        ws_config: Optional workspace configuration object.  Used locally for
            include/exclude path extraction and resolver setup; not stored.
    """
    with timed_phase(
        logger,
        category=LogCategory.MILESTONE,
        phase="mcp",
        name="start_mcp",
        channel="mcp",
        payload={"workspace_root": workspace_root, "docker_image": docker_image},
    ):
        try:
            import mcp.server.fastmcp  # type: ignore[import-not-found]  # noqa: F401 — validate dependency
        except ImportError:
            raise ImportError(
                "MCP mode requires the 'mcp' package. "
                "Install with: pip install ivy-lsp[mcp]"
            )

        root = workspace_root or os.getcwd()
        log_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="mcp",
            message="MCP startup initialized",
            data={"root": root, "return_app": _return_app},
            level=logging.INFO,
        )

    # --- Reset stale circuit breaker state from previous sessions ---
    # The health-check hook at check-mcp-health.py persists failure counts
    # to /tmp/ivy-mcp-health-state.json. If that file is stale (>60s old),
    # clear it so a fresh MCP server doesn't inherit old failures.
    _cb_state_file = "/tmp/ivy-mcp-health-state.json"
    try:
        if os.path.exists(_cb_state_file):
            import json as _json

            _cb_mtime = os.path.getmtime(_cb_state_file)
            if time.time() - _cb_mtime > 60:
                with open(_cb_state_file, "w") as _cb_f:
                    _json.dump(
                        {"consecutive_failures": 0, "last_update": time.time()},
                        _cb_f,
                    )
                logger.info(
                    "Reset stale circuit breaker state (age=%.0fs)",
                    time.time() - _cb_mtime,
                )
    except OSError:
        pass  # Best-effort; state file is optional

    # --- Workspace scoping: respect include/exclude paths from detection ---
    if ws_config is not None:
        _include_paths = ws_config.include_paths
        _extra_exclude_dirs = frozenset(ws_config.exclude_paths)
    else:
        _cfg = get_config()
        _include_paths = _cfg.include_paths
        _extra_exclude_dirs = frozenset(_cfg.exclude_paths)

    if _include_paths:
        logger.info("Workspace include paths: %s", _include_paths)
    if _extra_exclude_dirs:
        logger.info("Workspace extra exclude dirs: %s", _extra_exclude_dirs)

    # --- Include resolver for cross-directory includes ---
    _resolver = None
    try:
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        # Use passed-in ws_config; only re-detect if not provided
        if ws_config is None:
            from ivy_lsp.core.workspace.detection import detect_ivy_workspace

            ws_config = detect_ivy_workspace(start_dir=root)
            logger.info(
                "MCP fallback workspace detection: root=%s, detected_by=%s",
                ws_config.workspace_root,
                ws_config.detected_by,
            )

        _stdlib_path = None
        if ws_config.standard_library:
            _stdlib_path = os.path.join(root, ws_config.standard_library)

        _resolver = IncludeResolver(
            root,
            ivy_include_path=_stdlib_path,
            exclude_paths=list(_extra_exclude_dirs),
            include_paths=_include_paths or [],
            workspace_layers=ws_config.workspace_layers,
        )
        _staging = _resolver.create_staging_directory()
        logger.info("MCP staging directory: %s", _staging)
        if ws_config.workspace_layers:
            _resolver.build_layered_staging()
            logger.info(
                "Built layered staging for %d layers",
                len(ws_config.workspace_layers),
            )
        # Use resolver's staging dir if none was explicitly passed
        if _resolver._staging_dir and not staging_dir:
            staging_dir = _resolver._staging_dir
    except Exception:
        logger.warning(
            "IncludeResolver init failed; tier-1 parsing will be limited",
            exc_info=True,
        )

    # Create executor for Docker-aware compilation
    executor = None
    if docker_image:
        try:
            from panther_ivy.api.executor import (  # type: ignore[import-not-found]
                IvyExecutor,
            )

            executor = IvyExecutor(docker_image=docker_image)
            logger.info("Docker executor configured with image: %s", docker_image)
        except ImportError:
            logger.warning(
                "panther_ivy.api.executor not available; "
                "falling back to native subprocess"
            )

    # --- Build McpServerState with all mutable state ---

    state = McpServerState(
        root=root,
        staging_dir=staging_dir,
        semantic_model=semantic_model,
        requirement_graph=requirement_graph,
        resolver=_resolver,
        include_paths=_include_paths,
        exclude_dirs=_extra_exclude_dirs,
        executor=executor,
        base_path=base_path,
    )

    ctx = state.build_tool_context()

    # Populate workspace_groups from ws_config (if available)
    if ws_config is not None and hasattr(ws_config, "workspace_groups"):
        ctx.workspace_groups = ws_config.workspace_groups or {}

    # Fallback: auto-discover workspace groups from protocol-testing/ subdirs
    if not ctx.workspace_groups:
        pt_dir = os.path.join(root, "protocol-testing")
        if os.path.isdir(pt_dir):
            for entry in sorted(os.listdir(pt_dir)):
                entry_path = os.path.join(pt_dir, entry)
                if not os.path.isdir(entry_path) or entry.startswith("."):
                    continue
                layers = [
                    d
                    for d in sorted(os.listdir(entry_path))
                    if os.path.isdir(os.path.join(entry_path, d))
                    and not d.startswith(".")
                ]
                if layers:
                    ctx.workspace_groups[entry] = layers
            if ctx.workspace_groups:
                logger.info(
                    "Auto-discovered workspace groups: %s",
                    sorted(ctx.workspace_groups),
                )

    # WorkspaceContext loading is deferred to the prewarm background thread
    # to avoid blocking the MCP stdio handshake (which has a 30s timeout).
    # All workspace_context consumers already guard with `is not None` checks.

    mcp = create_mcp_app(ctx)

    # Log MCP-READY immediately after tool registration — tools are callable now.
    # This is polled by wait-for-indexing.sh; logging it before prewarm avoids
    # a 5-15s delay in readiness detection.
    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    logger.info("[MCP-READY] Server initialized, tools registered")

    # --- Start or return ---

    if _return_app:
        # Synchronous load for test/sidecar callers (no stdio timeout concern)
        try:
            from ivy_lsp.core.workspace.context import WorkspaceContext

            ws_ctx = WorkspaceContext.load(root)
            ctx.workspace_context = ws_ctx
            state.workspace_context = ws_ctx
        except Exception:
            logger.debug("WorkspaceContext loading failed in MCP", exc_info=True)
        _restore_workspace_at_boot(ctx, root)
        return mcp

    # --- Background workspace load + pre-warming (non-blocking) ---
    # Workspace context and model/graph pre-warming run in a daemon thread
    # so the MCP stdio transport starts immediately (avoids 30s timeout).
    _restore_workspace_at_boot(ctx, root)
    state.prewarm(tool_context=ctx)

    # Start sidecar upgrade monitor in a daemon thread
    # (FastMCP's run() blocks the main event loop, so we use a separate thread)
    if os.environ.get("IVY_MCP_DISABLE_UPGRADE") != "1":

        def _start_monitor_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_sidecar_monitor(root))
            finally:
                loop.close()

        monitor_thread = threading.Thread(
            target=_start_monitor_in_thread,
            name="sidecar-monitor",
            daemon=True,
        )
        monitor_thread.start()
        logger.info("[SIDECAR-MONITOR] Started background upgrade monitor")

    try:
        mcp.run(transport="stdio")
    except _BaseExceptionGroup as eg:
        # Filter: only exit cleanly if ALL sub-exceptions are cancel-scope related.
        # Mixed groups (cancel-scope + real error) must re-raise to avoid masking.
        cancel_scope_errors = [
            e
            for e in eg.exceptions
            if isinstance(e, (RuntimeError, _BaseExceptionGroup))
            and "cancel scope" in str(e).lower()
        ]
        non_cancel_errors = [e for e in eg.exceptions if e not in cancel_scope_errors]
        if cancel_scope_errors and not non_cancel_errors:
            logger.warning(
                "[MCP] Cancel scope crash caught at transport level, exiting cleanly: %s",
                eg,
            )
            sys.exit(0)  # Clean exit -> Claude Code auto-restarts
        raise
    except RuntimeError as exc:
        if "cancel scope" in str(exc).lower():
            logger.warning("[MCP] Cancel scope RuntimeError, exiting cleanly: %s", exc)
            sys.exit(0)
        raise
    finally:
        state._tool_executor.shutdown(wait=False, cancel_futures=True)
        logger.debug("[MCP] Tool executor shut down")
