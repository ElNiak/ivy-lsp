"""MCP HTTP sidecar for the unified LSP+MCP process.

Runs the MCP server as a Streamable HTTP endpoint in a daemon thread,
sharing the LSP server's live workspace state (SemanticModel,
RequirementGraph, WorkspaceIndexer) via ToolContext.from_lsp_server().

Usage (from IvyLanguageServer):
    thread, port = start_mcp_http_thread(lsp_server, port=19847)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import threading
from typing import Any

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


def _workspace_hash(root: str) -> str:
    """Stable short hash of the workspace root for port file naming."""
    return hashlib.sha256(root.encode()).hexdigest()[:12]


def _write_port_file(root: str, port: int) -> str:
    """Write port to /tmp/ivy-mcp-{workspace_hash}.port, return the path."""
    ws_hash = _workspace_hash(root)
    path = os.path.join("/tmp", f"ivy-mcp-{ws_hash}.port")
    try:
        with open(path, "w") as f:
            f.write(str(port))
        logger.info("MCP port file written: %s (port=%d)", path, port)
    except OSError as exc:
        logger.warning("Failed to write MCP port file %s: %s", path, exc)
    return path


def _remove_port_file(root: str) -> None:
    """Remove the port file on shutdown."""
    ws_hash = _workspace_hash(root)
    path = os.path.join("/tmp", f"ivy-mcp-{ws_hash}.port")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to remove MCP port file: %s", exc)


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
    from ivy_lsp.mcp_server import ToolContext, create_mcp_app

    ctx = ToolContext.from_lsp_server(lsp_server)

    if port <= 0:
        port = _DEFAULT_MCP_PORT

    actual_port = _find_available_port(port)

    mcp_app = create_mcp_app(ctx)
    port_file_path = _write_port_file(ctx.root, actual_port)

    def _run_sidecar():
        """Entry point for the daemon thread — runs its own event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve_mcp_http(mcp_app, actual_port, ctx.root))
        except Exception:
            logger.error("MCP HTTP sidecar crashed", exc_info=True)
        finally:
            loop.close()

    thread = threading.Thread(
        target=_run_sidecar,
        name="mcp-http-sidecar",
        daemon=True,
    )
    thread.start()
    logger.info(
        "MCP HTTP sidecar started on 127.0.0.1:%d (thread=%s, port_file=%s)",
        actual_port,
        thread.name,
        port_file_path,
    )
    return thread, actual_port


async def _serve_mcp_http(mcp_app: Any, port: int, workspace_root: str) -> None:
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

    config = uvicorn.Config(
        app=asgi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("[MCP-HTTP-READY] Serving on http://127.0.0.1:%d/mcp", port)
    await server.serve()
