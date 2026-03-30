"""Tests for the sidecar monitor background task."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ivy_lsp.mcp import client as sidecar_client


@pytest.mark.asyncio
async def test_monitor_skips_when_disabled():
    """Monitor exits immediately when IVY_MCP_DISABLE_UPGRADE=1."""
    from ivy_lsp.mcp.server import _sidecar_monitor

    with patch.dict("os.environ", {"IVY_MCP_DISABLE_UPGRADE": "1"}):
        task = asyncio.create_task(_sidecar_monitor("/workspace"))
        await asyncio.sleep(0.1)
        assert task.done()


@pytest.mark.asyncio
async def test_monitor_sets_client_on_valid_sidecar():
    """Monitor stores sidecar port when sidecar is validated (lazy connection)."""
    from ivy_lsp.mcp.server import _sidecar_monitor

    old_client = sidecar_client.get_sidecar_client()
    old_port = sidecar_client.get_sidecar_port()
    try:
        sidecar_client.set_sidecar_client(None)
        sidecar_client.set_sidecar_port(None)

        with patch("ivy_lsp.mcp.server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "abc123"
            mock_sc.read_port_file.return_value = 19847
            mock_sc.validate_sidecar_workspace = AsyncMock(return_value=True)
            mock_sc.get_sidecar_port = sidecar_client.get_sidecar_port
            mock_sc.set_sidecar_port = sidecar_client.set_sidecar_port

            task = asyncio.create_task(
                _sidecar_monitor("/workspace", _poll_interval=0.05, _max_iterations=2)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert sidecar_client.get_sidecar_port() == 19847
    finally:
        sidecar_client.set_sidecar_client(old_client)
        sidecar_client.set_sidecar_port(old_port)


@pytest.mark.asyncio
async def test_monitor_skips_workspace_mismatch():
    """Monitor does not upgrade when workspace doesn't match."""
    from ivy_lsp.mcp.server import _sidecar_monitor

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(None)

        with patch("ivy_lsp.mcp.server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "abc123"
            mock_sc.read_port_file.return_value = 19847
            mock_sc.validate_sidecar_workspace = AsyncMock(return_value=False)
            mock_sc.get_sidecar_port = sidecar_client.get_sidecar_port

            task = asyncio.create_task(
                _sidecar_monitor("/workspace", _poll_interval=0.05, _max_iterations=2)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert sidecar_client.get_sidecar_port() is None
    finally:
        sidecar_client.set_sidecar_port(old)
