"""Tests for safe_tool sidecar delegation and error handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from ivy_lsp.mcp import client as sidecar_client
from ivy_lsp.mcp.client import _list_tools_cache, call_sidecar_once
from ivy_lsp.mcp.tools import (
    _cancel_safe_wait_for,
    _error_result,
    _try_sidecar_delegation,
)

# ---------------------------------------------------------------------------
# call_sidecar_once tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_sidecar_once_success():
    """call_sidecar_once returns the CallToolResult on success."""
    mock_result = MagicMock(spec=CallToolResult)
    mock_result.content = [MagicMock(text='{"delegated": true}')]

    mock_session = AsyncMock()
    mock_session.call_tool.return_value = mock_result
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.initialize.return_value = None

    _list_tools_cache.clear()

    with patch("mcp.client.streamable_http.streamablehttp_client") as mock_transport:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_transport.return_value = mock_ctx

        with patch("mcp.ClientSession") as mock_session_cls:
            mock_session_ctx = AsyncMock()
            mock_session_ctx.__aenter__.return_value = mock_session
            mock_session_cls.return_value = mock_session_ctx

            result = await call_sidecar_once(19847, "ivy_capabilities", {}, 5.0)

    assert result is mock_result
    mock_session.call_tool.assert_called_once_with("ivy_capabilities", {})


@pytest.mark.asyncio
async def test_call_sidecar_once_connection_failure():
    """call_sidecar_once returns None when connection fails."""
    _list_tools_cache.clear()

    with patch(
        "mcp.client.streamable_http.streamablehttp_client",
        side_effect=ConnectionError("refused"),
    ):
        result = await call_sidecar_once(19847, "ivy_capabilities", {}, 5.0)

    assert result is None


@pytest.mark.asyncio
async def test_call_sidecar_once_timeout():
    """call_sidecar_once returns None on timeout (caught internally)."""

    async def hang(*args, **kwargs):
        await asyncio.sleep(999)

    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = hang
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.initialize.return_value = None

    _list_tools_cache.clear()

    with patch("mcp.client.streamable_http.streamablehttp_client") as mock_transport:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_transport.return_value = mock_ctx

        with patch("mcp.ClientSession") as mock_session_cls:
            mock_session_ctx = AsyncMock()
            mock_session_ctx.__aenter__.return_value = mock_session
            mock_session_cls.return_value = mock_session_ctx

            result = await call_sidecar_once(19847, "ivy_test", {}, 0.05)

    assert result is None


@pytest.mark.asyncio
async def test_list_tools_cache_hit():
    """Second call with same port skips list_tools()."""
    mock_session = AsyncMock()
    mock_session.call_tool.return_value = MagicMock(spec=CallToolResult)
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.initialize.return_value = None

    _list_tools_cache.clear()

    with patch("mcp.client.streamable_http.streamablehttp_client") as mock_transport:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_transport.return_value = mock_ctx

        with patch("mcp.ClientSession") as mock_session_cls:
            mock_session_ctx = AsyncMock()
            mock_session_ctx.__aenter__.return_value = mock_session
            mock_session_cls.return_value = mock_session_ctx

            # First call — list_tools should be called
            await call_sidecar_once(19847, "ivy_test", {}, 5.0)
            assert mock_session.list_tools.call_count == 1

            # Second call — list_tools should be skipped (cached)
            await call_sidecar_once(19847, "ivy_test", {}, 5.0)
            assert mock_session.list_tools.call_count == 1  # Still 1


@pytest.mark.asyncio
async def test_local_only_skips_sidecar_delegation():
    """Tools marked local_only bypass sidecar delegation even when a port exists."""
    old_port = sidecar_client.get_sidecar_port()
    try:
        sidecar_client.set_sidecar_port(19847)

        with patch(
            "ivy_lsp.mcp.tools.call_sidecar_once",
            new_callable=AsyncMock,
        ) as mock_call:
            result = await _try_sidecar_delegation(
                "ivy_workflow_state", {"action": "get"}
            )

        assert result is None
        mock_call.assert_not_called()
    finally:
        sidecar_client.set_sidecar_port(old_port)


@pytest.mark.asyncio
async def test_non_local_only_delegates_to_sidecar():
    """Tools without local_only still attempt sidecar delegation."""
    old_port = sidecar_client.get_sidecar_port()
    try:
        sidecar_client.set_sidecar_port(19847)
        mock_result = MagicMock(spec=CallToolResult)

        with patch(
            "ivy_lsp.mcp.tools.call_sidecar_once",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_call:
            result = await _try_sidecar_delegation("ivy_verify", {"test_file": "t.ivy"})

        assert result is mock_result
        mock_call.assert_called_once()
    finally:
        sidecar_client.set_sidecar_port(old_port)


def test_list_tools_cache_cleared_on_port_change():
    """set_sidecar_port with a different port clears _list_tools_cache."""
    old_port = sidecar_client.get_sidecar_port()
    try:
        _list_tools_cache.add(19847)
        assert 19847 in _list_tools_cache

        sidecar_client.set_sidecar_port(19848)
        assert len(_list_tools_cache) == 0
    finally:
        sidecar_client.set_sidecar_port(old_port)
        _list_tools_cache.clear()


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
# Fix 2A: _cleanup_sidecar tests (cancel scope shielding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_sidecar_catches_runtime_error():
    """RuntimeError('cancel scope') in disconnect does not propagate."""
    from ivy_lsp.mcp.tools import _cleanup_sidecar

    mock_client = AsyncMock()
    with patch(
        "ivy_lsp.mcp.client.disconnect_sidecar",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Attempted to exit a cancel scope that isn't current"),
    ):
        # Should not raise
        await _cleanup_sidecar(mock_client)


@pytest.mark.asyncio
async def test_cleanup_sidecar_catches_cancelled_error():
    """CancelledError in disconnect does not propagate."""
    from ivy_lsp.mcp.tools import _cleanup_sidecar

    mock_client = AsyncMock()
    with patch(
        "ivy_lsp.mcp.client.disconnect_sidecar",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError(),
    ):
        await _cleanup_sidecar(mock_client)


@pytest.mark.asyncio
async def test_cleanup_sidecar_timeout():
    """Cleanup returns within 3s+ even if disconnect hangs."""
    from ivy_lsp.mcp.tools import _cleanup_sidecar

    mock_client = AsyncMock()

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(999)

    with patch("ivy_lsp.mcp.client.disconnect_sidecar", new=hang):
        import time as _time

        start = _time.monotonic()
        await _cleanup_sidecar(mock_client)
        elapsed = _time.monotonic() - start
        # Should complete within ~3s timeout + small margin
        assert elapsed < 5.0
