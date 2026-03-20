"""Tests for the sidecar monitor background task."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ivy_lsp import sidecar_client


@pytest.mark.asyncio
async def test_monitor_skips_when_disabled():
    """Monitor exits immediately when IVY_MCP_DISABLE_UPGRADE=1."""
    from ivy_lsp.mcp_server import _sidecar_monitor

    with patch.dict("os.environ", {"IVY_MCP_DISABLE_UPGRADE": "1"}):
        task = asyncio.create_task(_sidecar_monitor("/workspace"))
        await asyncio.sleep(0.1)
        assert task.done()


@pytest.mark.asyncio
async def test_monitor_sets_client_on_valid_sidecar():
    """Monitor sets sidecar_client when sidecar is validated."""
    from ivy_lsp.mcp_server import _sidecar_monitor

    mock_client = MagicMock()

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(None)

        with patch("ivy_lsp.mcp_server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "abc123"
            mock_sc.read_port_file.return_value = 19847
            mock_sc.validate_sidecar_workspace = AsyncMock(return_value=True)
            mock_sc.connect_to_sidecar = AsyncMock(return_value=mock_client)
            mock_sc.get_sidecar_client.return_value = None
            mock_sc.set_sidecar_client = sidecar_client.set_sidecar_client

            task = asyncio.create_task(
                _sidecar_monitor("/workspace", _poll_interval=0.05, _max_iterations=2)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert sidecar_client.get_sidecar_client() is mock_client
    finally:
        sidecar_client.set_sidecar_client(old)


@pytest.mark.asyncio
async def test_monitor_skips_workspace_mismatch():
    """Monitor does not upgrade when workspace doesn't match."""
    from ivy_lsp.mcp_server import _sidecar_monitor

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(None)

        with patch("ivy_lsp.mcp_server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "abc123"
            mock_sc.read_port_file.return_value = 19847
            mock_sc.validate_sidecar_workspace = AsyncMock(return_value=False)
            mock_sc.get_sidecar_client.return_value = None

            task = asyncio.create_task(
                _sidecar_monitor("/workspace", _poll_interval=0.05, _max_iterations=2)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert sidecar_client.get_sidecar_client() is None
    finally:
        sidecar_client.set_sidecar_client(old)
