"""Tests for safe_tool sidecar delegation and error handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from ivy_lsp import sidecar_client
from ivy_lsp.tools import _cancel_safe_wait_for, _error_result


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


# ---------------------------------------------------------------------------
# _error_result helper tests
# ---------------------------------------------------------------------------


def test_error_result_has_isError_true():
    """_error_result returns a CallToolResult with isError=True."""
    result = _error_result({"success": False, "message": "boom", "tool": "test"})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert "boom" in result.content[0].text


def test_error_result_timeout_message():
    """_error_result includes timeout-specific text when timeout flag is set."""
    result = _error_result(
        {
            "success": False,
            "message": "Tool timed out after 60s",
            "timeout": True,
            "tool": "ivy_include_graph",
        }
    )
    assert result.isError is True
    assert "timed out" in result.content[0].text
    assert "ivy_include_graph" in result.content[0].text


# ---------------------------------------------------------------------------
# _cancel_safe_wait_for tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_safe_wait_for_success():
    """Returns the result when coroutine completes within timeout."""

    async def fast():
        return 42

    result = await _cancel_safe_wait_for(fast(), timeout=5.0)
    assert result == 42


@pytest.mark.asyncio
async def test_cancel_safe_wait_for_timeout():
    """Raises asyncio.TimeoutError when coroutine exceeds timeout."""

    async def slow():
        await asyncio.sleep(999)

    with pytest.raises(asyncio.TimeoutError):
        await _cancel_safe_wait_for(slow(), timeout=0.05)


@pytest.mark.asyncio
async def test_cancel_safe_wait_for_no_cancel_leak():
    """CancelledError does not propagate to the caller after timeout."""
    cancel_seen = False

    async def slow():
        nonlocal cancel_seen
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancel_seen = True
            raise

    with pytest.raises(asyncio.TimeoutError):
        await _cancel_safe_wait_for(slow(), timeout=0.05)

    # Give the event loop a tick to process the cancellation
    await asyncio.sleep(0.01)
    assert cancel_seen, "Inner task should have been cancelled"


@pytest.mark.asyncio
async def test_cancel_safe_wait_for_propagates_exception():
    """If the coroutine raises, the exception propagates through."""

    async def broken():
        raise ValueError("test error")

    with pytest.raises(ValueError, match="test error"):
        await _cancel_safe_wait_for(broken(), timeout=5.0)


# ---------------------------------------------------------------------------
# Sidecar timeout integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidecar_timeout_disconnects_client():
    """After sidecar call_tool timeout, the client is set to None."""
    mock_client = AsyncMock()

    async def hang(*args, **kwargs):
        await asyncio.sleep(999)

    mock_client.call_tool.side_effect = hang

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(mock_client)
        assert sidecar_client.get_sidecar_client() is mock_client

        # Simulate what safe_tool does on timeout
        try:
            await _cancel_safe_wait_for(
                mock_client.call_tool("ivy_test", {}),
                timeout=0.05,
            )
        except asyncio.TimeoutError:
            sidecar_client.set_sidecar_client(None)

        assert sidecar_client.get_sidecar_client() is None
    finally:
        sidecar_client.set_sidecar_client(old)


@pytest.mark.asyncio
async def test_sidecar_connection_error_still_downgrades():
    """Non-timeout exceptions still trigger downgrade (client reset)."""
    mock_client = AsyncMock()
    mock_client.call_tool.side_effect = ConnectionError("sidecar crashed")

    old = sidecar_client.get_sidecar_client()
    try:
        sidecar_client.set_sidecar_client(mock_client)

        # Simulate what safe_tool does on general exception
        try:
            await mock_client.call_tool("ivy_test", {})
        except asyncio.TimeoutError:
            pass  # Not expected
        except Exception:
            sidecar_client.set_sidecar_client(None)

        assert sidecar_client.get_sidecar_client() is None
    finally:
        sidecar_client.set_sidecar_client(old)
