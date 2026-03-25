"""Tests for the sidecar client — connection, validation, delegation."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest


def test_read_port_file_returns_port(tmp_path):
    from ivy_lsp.mcp.client import read_port_file

    port_file = tmp_path / "ivy-mcp-abc123.port"
    port_file.write_text("19847")
    assert read_port_file(str(tmp_path), "abc123") == 19847


def test_read_port_file_returns_none_when_missing(tmp_path):
    from ivy_lsp.mcp.client import read_port_file

    assert read_port_file(str(tmp_path), "abc123") is None


def test_read_port_file_returns_none_when_corrupt(tmp_path):
    from ivy_lsp.mcp.client import read_port_file

    port_file = tmp_path / "ivy-mcp-abc123.port"
    port_file.write_text("not-a-number")
    assert read_port_file(str(tmp_path), "abc123") is None


def test_workspace_hash_is_stable():
    from ivy_lsp.mcp.client import workspace_hash

    h1 = workspace_hash("/some/path")
    h2 = workspace_hash("/some/path")
    assert h1 == h2
    assert len(h1) == 12


def test_workspace_hash_differs_for_different_paths():
    from ivy_lsp.mcp.client import workspace_hash

    assert workspace_hash("/path/a") != workspace_hash("/path/b")


async def test_validate_workspace_rejects_mismatch():
    from ivy_lsp.mcp.client import validate_sidecar_workspace

    with patch("ivy_lsp.mcp.client._fetch_health") as mock_fetch:
        mock_fetch.return_value = {"workspace_root": "/other/workspace"}
        result = await validate_sidecar_workspace(19847, "/my/workspace")
        assert result is False


async def test_validate_workspace_accepts_match():
    from ivy_lsp.mcp.client import validate_sidecar_workspace

    with patch("ivy_lsp.mcp.client._fetch_health") as mock_fetch:
        mock_fetch.return_value = {"workspace_root": "/my/workspace"}
        result = await validate_sidecar_workspace(19847, "/my/workspace")
        assert result is True


async def test_validate_workspace_handles_unreachable():
    from ivy_lsp.mcp.client import validate_sidecar_workspace

    with patch("ivy_lsp.mcp.client._fetch_health") as mock_fetch:
        mock_fetch.return_value = None
        result = await validate_sidecar_workspace(19847, "/my/workspace")
        assert result is False


def test_get_set_sidecar_client():
    from ivy_lsp.mcp.client import get_sidecar_client, set_sidecar_client

    # Initially None
    set_sidecar_client(None)
    assert get_sidecar_client() is None

    # Set to a sentinel object
    sentinel = object()
    set_sidecar_client(sentinel)
    assert get_sidecar_client() is sentinel

    # Clean up
    set_sidecar_client(None)


def test_workspace_hash_matches_sidecar_algorithm():
    """Ensure workspace_hash matches _workspace_hash in mcp_sidecar.py."""
    import hashlib

    from ivy_lsp.mcp.client import workspace_hash

    path = "/some/workspace/root"
    expected = hashlib.sha256(path.encode()).hexdigest()[:12]
    assert workspace_hash(path) == expected


async def test_validate_workspace_handles_missing_workspace_root():
    from ivy_lsp.mcp.client import validate_sidecar_workspace

    with patch("ivy_lsp.mcp.client._fetch_health") as mock_fetch:
        mock_fetch.return_value = {"status": "ok"}  # no workspace_root key
        result = await validate_sidecar_workspace(19847, "/my/workspace")
        assert result is False


def test_read_port_file_returns_none_when_ws_hash_is_none():
    from ivy_lsp.mcp.client import read_port_file

    assert read_port_file("/tmp") is None
