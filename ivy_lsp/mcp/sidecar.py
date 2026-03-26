"""MCP HTTP sidecar for the unified LSP+MCP process.

Runs the MCP server as a Streamable HTTP endpoint in a daemon thread,
sharing the LSP server's live workspace state (SemanticModel,
RequirementGraph, WorkspaceIndexer) via ToolContext.from_lsp_server().

Usage (from IvyLanguageServer):
    thread, port = start_mcp_http_thread(lsp_server, port=19847)
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import socket
import threading
import time as _time
from typing import Any

from ivy_lsp.infra.observability import (
    LogCategory,
    log_phase,
    timed_phase,
    workspace_hash,
)

logger = logging.getLogger(__name__)

_DEFAULT_MCP_PORT = 19847
_PORT_RETRY_RANGE = 10


def _find_available_port(start: int) -> int:
    """Find an available port starting from *start*, trying up to +10."""
    for offset in range(_PORT_RETRY_RANGE + 1):
        port = start + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(
        f"No available port found in range {start}-{start + _PORT_RETRY_RANGE}"
    )


def _write_port_file(root: str, port: int) -> str:
    """Write port to /tmp/ivy-mcp-{workspace_hash}.port, return the path."""
    ws_hash = workspace_hash(root)
    path = os.path.join("/tmp", f"ivy-mcp-{ws_hash}.port")
    try:
        with open(path, "w") as f:
            f.write(str(port))
            f.flush()
            os.fsync(f.fileno())
        logger.info("MCP port file written: %s (port=%d)", path, port)
    except OSError as exc:
        logger.warning("Failed to write MCP port file %s: %s", path, exc)
    return path


def _remove_port_file(root: str) -> None:
    """Remove the port file on shutdown."""
    ws_hash = workspace_hash(root)
    path = os.path.join("/tmp", f"ivy-mcp-{ws_hash}.port")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to remove MCP port file: %s", exc)


def _health_middleware_factory(ctx: Any, start_time: float) -> Any:
    """Create an ASGI middleware that handles /health requests.

    Returns an async callable(scope, receive, send) that responds to
    ``GET /health`` with JSON containing status, uptime, tool count,
    model status, and workspace_root.
    """

    async def _health_handler(scope: dict, receive: Any, send: Any) -> None:
        model_status = "unknown"
        if ctx is not None and hasattr(ctx, "get_model_status"):
            try:
                model_status = ctx.get_model_status().get("state", "unknown")
            except Exception:
                model_status = "error"
        body = _json.dumps(
            {
                "status": "ok",
                "uptime_seconds": round(_time.monotonic() - start_time, 1),
                "tools_registered": 13,
                "model_status": model_status,
                "workspace_root": getattr(ctx, "root", ""),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return _health_handler


def start_mcp_http_thread(
    lsp_server: Any,
    port: int = 0,
) -> tuple[threading.Thread, int]:
    """Start the MCP HTTP sidecar in a daemon thread.

    Args:
        lsp_server: The IvyLanguageServer instance to bridge.
        port: Desired port (0 = use default 19847). Auto-increments
            on conflict up to +10.

    Returns:
        (thread, actual_port) — the daemon thread and the bound port.

    Raises:
        RuntimeError: If no port is available or dependencies are missing.
    """
    from ivy_lsp.mcp.server import ToolContext, create_mcp_app

    with timed_phase(
        logger,
        category=LogCategory.MILESTONE,
        phase="mcp-sidecar",
        name="start_mcp_http_thread",
        channel="mcp",
        payload={"requested_port": port},
    ):
        ctx = ToolContext.from_lsp_server(lsp_server)

        # Fallback: indexer not initialized yet, use env var from start-ivy-server.sh
        if not ctx.root:
            ctx.root = os.environ.get("IVY_WORKSPACE_ROOT", os.getcwd())
            logger.info("Sidecar using workspace root from env: %s", ctx.root)

        if port <= 0:
            port = _DEFAULT_MCP_PORT

        actual_port = _find_available_port(port)

        mcp_app = create_mcp_app(ctx)

        # Readiness barrier: port file is written only after uvicorn binds,
        # preventing health-check race conditions.
        ready_event = threading.Event()

        def _run_sidecar():
            """Entry point for the daemon thread — runs its own event loop."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    _serve_mcp_http(
                        mcp_app,
                        actual_port,
                        ctx.root,
                        ctx=ctx,
                        ready_event=ready_event,
                    )
                )
            except Exception:
                logger.error("MCP HTTP sidecar crashed", exc_info=True)
            finally:
                ready_event.set()  # unblock main thread even on crash
                loop.close()

        thread = threading.Thread(
            target=_run_sidecar,
            name="mcp-http-sidecar",
            daemon=True,
        )
        thread.start()

        # Wait for uvicorn to actually bind the port before writing the port file
        if not ready_event.wait(timeout=10.0):
            logger.warning(
                "MCP sidecar did not become ready within 10s; "
                "writing port file anyway"
            )
        port_file_path = _write_port_file(ctx.root, actual_port)

    logger.info(
        "MCP HTTP sidecar started on 127.0.0.1:%d (thread=%s, port_file=%s)",
        actual_port,
        thread.name,
        port_file_path,
    )
    log_phase(
        logger,
        category=LogCategory.MILESTONE,
        phase="mcp-sidecar",
        message="MCP sidecar thread started",
        data={"port": actual_port, "workspace_root": ctx.root},
        level=logging.INFO,
    )
    return thread, actual_port


async def _serve_mcp_http(
    mcp_app: Any,
    port: int,
    workspace_root: str,
    ctx: Any = None,
    ready_event: threading.Event | None = None,
) -> None:
    """Run the FastMCP app via uvicorn's Streamable HTTP transport."""
    try:
        import uvicorn
    except ImportError:
        logger.error(
            "uvicorn not installed — MCP HTTP sidecar disabled. "
            "Install with: pip install ivy-lsp[mcp]"
        )
        return

    # FastMCP exposes an ASGI app via streamable_http_app()
    try:
        asgi_app = mcp_app.streamable_http_app()
    except AttributeError:
        # Fallback: try sse_app() for older mcp versions
        try:
            asgi_app = mcp_app.sse_app()
            logger.info("Using SSE transport (mcp <1.8)")
        except AttributeError:
            logger.error(
                "FastMCP has no streamable_http_app() or sse_app() — "
                "upgrade mcp package to >= 1.0"
            )
            return

    # Wrap the ASGI app with a health-check middleware
    sidecar_start_time = _time.monotonic()
    _health_handler = _health_middleware_factory(ctx, start_time=sidecar_start_time)

    async def _health_middleware(scope: dict, receive: Any, send: Any) -> None:
        """ASGI middleware that intercepts /health requests."""
        if scope["type"] == "http" and scope["path"] == "/health":
            await _health_handler(scope, receive, send)
            return
        await asgi_app(scope, receive, send)

    async def _prewarm_model():
        """Pre-warm the semantic model after LSP initialization completes."""
        from ivy_lsp.infra.config import get_config

        cfg = get_config()
        if not cfg.prewarm_model:
            logger.info("[MCP-PREWARM] Model pre-warm disabled by config")
            return
        # Wait for LSP to finish initializing
        await asyncio.sleep(5)
        try:
            if hasattr(ctx, "get_model") and callable(ctx.get_model):
                import inspect

                result = ctx.get_model()
                if inspect.isawaitable(result):
                    await result
                logger.info("[MCP-PREWARM] Model pre-warm completed")
        except Exception:
            logger.warning("[MCP-PREWARM] Model pre-warm failed", exc_info=True)

    if ctx is not None:
        asyncio.create_task(_prewarm_model())

    config = uvicorn.Config(
        app=_health_middleware,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def _signal_ready() -> None:
        """Poll uvicorn until it starts, then signal the readiness event."""
        while not server.started:
            await asyncio.sleep(0.05)
        if ready_event is not None:
            ready_event.set()

    asyncio.create_task(_signal_ready())

    logger.info("[MCP-HTTP-READY] Serving on http://127.0.0.1:%d/mcp", port)
    log_phase(
        logger,
        category=LogCategory.MILESTONE,
        phase="mcp-sidecar",
        message="MCP HTTP ready",
        data={"port": port, "workspace_root": workspace_root},
        level=logging.INFO,
    )
    await server.serve()
