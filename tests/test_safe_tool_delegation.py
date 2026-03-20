"""Tests for safe_tool sidecar delegation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ivy_lsp import sidecar_client


@pytest.mark.asyncio
async def test_delegation_calls_sidecar_when_client_set():
    """When _sidecar_client is set, get_sidecar_client returns it."""
    mock_client = AsyncMock()
    mock_result = MagicMock()
    mock_result.content = [MagicMock(text='{"delegated": true}')]
    mock_client.call_tool.return_value = mock_result

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(mock_client)
        client = sidecar_client.get_sidecar_client()
        assert client is mock_client

        result = await client.call_tool("ivy_capabilities", {})
        mock_client.call_tool.assert_called_once_with("ivy_capabilities", {})
    finally:
        sidecar_client.set_sidecar_client(old)


@pytest.mark.asyncio
async def test_delegation_resets_client_on_error():
    """On connection error, set_sidecar_client(None) reverts to local."""
    mock_client = AsyncMock()
    mock_client.call_tool.side_effect = ConnectionError("sidecar gone")

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(mock_client)

        try:
            await mock_client.call_tool("ivy_capabilities", {})
        except ConnectionError:
            sidecar_client.set_sidecar_client(None)

        assert sidecar_client.get_sidecar_client() is None
    finally:
        sidecar_client.set_sidecar_client(old)


def test_no_client_returns_none():
    """When no sidecar is set, get_sidecar_client returns None."""
    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(None)
        assert sidecar_client.get_sidecar_client() is None
    finally:
        sidecar_client.set_sidecar_client(old)
