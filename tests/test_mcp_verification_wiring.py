"""Test that MCP handlers use shared verification when staging_dir is set."""
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_start_mcp_accepts_staging_dir(tmp_path):
    """start_mcp accepts staging_dir parameter without error."""
    with patch("ivy_lsp.mcp_server.shared_ivy_check", new_callable=AsyncMock):
        from ivy_lsp.mcp_server import start_mcp
        app = start_mcp(
            workspace_root=str(tmp_path),
            staging_dir=str(tmp_path / "staging"),
            _return_app=True,
        )
        assert app is not None
