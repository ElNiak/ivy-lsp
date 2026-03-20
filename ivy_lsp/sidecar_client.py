"""Sidecar client for the lazy bridge upgrade mechanism.

Handles:
- Global sidecar client state (get/set)
- Port file reading and workspace hash computation
- Sidecar health check and workspace validation
- MCP client connection via streamablehttp_client

This module is imported by both mcp_server.py (monitor) and
tools/__init__.py (safe_tool delegation). It must NOT import
from either to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# --- Global sidecar client state ---
_sidecar_client: Any | None = None


def get_sidecar_client() -> Any | None:
    """Read the current sidecar client. Used by safe_tool."""
    return _sidecar_client


def set_sidecar_client(client: Any | None) -> None:
    """Swap the sidecar client reference (atomic under CPython GIL)."""
    global _sidecar_client
    _sidecar_client = client


def workspace_hash(root: str) -> str:
    """Stable 12-char hash of the workspace root path.

    Must match ``_workspace_hash`` in ``mcp_sidecar.py`` — SHA-256, first 12
    hex chars.
    """
    return hashlib.sha256(root.encode()).hexdigest()[:12]


def read_port_file(port_dir: str = "/tmp", ws_hash: str | None = None) -> int | None:
    """Read the sidecar port from ``/tmp/ivy-mcp-{ws_hash}.port``.

    Returns ``None`` if the file is missing, corrupt, or *ws_hash* is None.
    """
    if ws_hash is None:
        return None
    path = os.path.join(port_dir, f"ivy-mcp-{ws_hash}.port")
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _fetch_health_sync(port: int) -> dict | None:
    """Fetch sidecar ``/health`` (synchronous, for use via ``asyncio.to_thread``)."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except Exception:
        logger.debug("Sidecar health check failed on port %d", port)
    return None


async def _fetch_health(port: int) -> dict | None:
    """Async wrapper around the synchronous health check."""
    return await asyncio.to_thread(_fetch_health_sync, port)


async def validate_sidecar_workspace(port: int, expected_root: str) -> bool:
    """Check that the sidecar's ``workspace_root`` matches ours."""
    health = await _fetch_health(port)
    if health is None:
        return False
    sidecar_root = health.get("workspace_root", "")
    if not sidecar_root:
        logger.debug("Sidecar /health missing workspace_root")
        return False
    match = os.path.realpath(sidecar_root) == os.path.realpath(expected_root)
    if not match:
        logger.debug(
            "Workspace mismatch: ours=%s sidecar=%s", expected_root, sidecar_root
        )
    return match


async def connect_to_sidecar(port: int) -> Any:
    """Establish an MCP client session to the sidecar.

    Returns a ``ClientSession`` that can call tools, or ``None`` on failure.
    Stores the transport context manager on the session as ``_transport_ctx``
    for cleanup by :func:`disconnect_sidecar`.
    """
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        url = f"http://127.0.0.1:{port}/mcp"
        transport_ctx = streamablehttp_client(url)
        read_stream, write_stream, _ = await transport_ctx.__aenter__()

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        session._transport_ctx = transport_ctx  # type: ignore[attr-defined]
        logger.info("[SIDECAR-CLIENT] Connected to sidecar on port %d", port)
        return session
    except Exception:
        logger.warning(
            "[SIDECAR-CLIENT] Failed to connect to port %d", port, exc_info=True
        )
        return None


async def disconnect_sidecar(session: Any) -> None:
    """Clean up an MCP client session and its transport."""
    try:
        await session.__aexit__(None, None, None)
    except Exception:
        logger.debug("Session cleanup failed", exc_info=True)
    try:
        ctx = getattr(session, "_transport_ctx", None)
        if ctx is not None:
            await ctx.__aexit__(None, None, None)
    except Exception:
        logger.debug("Transport cleanup failed", exc_info=True)
