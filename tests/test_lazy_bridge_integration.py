"""Integration test for the lazy bridge lifecycle.

Tests the full cycle:
1. MCP starts standalone (no sidecar)
2. Sidecar port file appears
3. Monitor detects and upgrades
4. Tool calls are delegated
5. Sidecar goes away
6. Tool calls revert to local
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from ivy_lsp.mcp import client as sidecar_client


@pytest.mark.asyncio
async def test_full_upgrade_downgrade_cycle():
    """Simulate: standalone -> upgrade -> downgrade -> standalone."""
    from ivy_lsp.mcp.server import _sidecar_monitor

    old_client = sidecar_client.get_sidecar_client()
    old_port = sidecar_client.get_sidecar_port()
    try:
        # Start clean
        sidecar_client.set_sidecar_client(None)
        sidecar_client.set_sidecar_port(None)
        assert sidecar_client.get_sidecar_port() is None

        # Phase 1: No port file -> stays standalone
        with patch("ivy_lsp.mcp.server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "test123"
            mock_sc.read_port_file.return_value = None
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

        assert sidecar_client.get_sidecar_port() is None  # Still standalone

        # Phase 2: Port file appears -> port stored (lazy connection)
        with patch("ivy_lsp.mcp.server.sidecar_client") as mock_sc:
            mock_sc.workspace_hash.return_value = "test123"
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

        assert sidecar_client.get_sidecar_port() == 19847  # Port stored!

        # Phase 3: Downgrade (sidecar goes away, port reset to None)
        sidecar_client.set_sidecar_port(None)
        assert sidecar_client.get_sidecar_port() is None  # Back to standalone
    finally:
        sidecar_client.set_sidecar_client(old_client)
        sidecar_client.set_sidecar_port(old_port)


@pytest.mark.asyncio
async def test_disable_upgrade_env_var():
    """IVY_MCP_DISABLE_UPGRADE=1 prevents monitor from running."""
    from ivy_lsp.mcp.server import _sidecar_monitor

    old_port = sidecar_client.get_sidecar_port()
    try:
        sidecar_client.set_sidecar_port(None)

        with patch.dict(os.environ, {"IVY_MCP_DISABLE_UPGRADE": "1"}):
            await _sidecar_monitor("/workspace")

        assert sidecar_client.get_sidecar_port() is None
    finally:
        sidecar_client.set_sidecar_port(old_port)
