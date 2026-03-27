# tests/test_tool_executor.py
"""Tests for dedicated tool thread pool executor."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading

import pytest

from ivy_lsp.mcp.context import ToolContext


def test_tool_context_accepts_executor():
    """ToolContext should accept a tool_executor field."""
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="test"
    )
    try:
        ctx = ToolContext(
            root="/tmp/test",
            staging_dir=None,
            executor=None,
            base_path=None,
            tool_executor=pool,
        )
        assert ctx.tool_executor is pool
    finally:
        pool.shutdown(wait=False)


def test_tool_context_executor_defaults_to_none():
    """ToolContext.tool_executor should default to None."""
    ctx = ToolContext(
        root="/tmp/test",
        staging_dir=None,
        executor=None,
        base_path=None,
    )
    assert ctx.tool_executor is None


@pytest.fixture
def mock_env(monkeypatch):
    """Set minimal env for McpServerState."""
    monkeypatch.setenv("IVY_LSP_PREWARM_MODEL", "0")
    monkeypatch.setenv("IVY_LSP_PREWARM_GRAPH", "0")


def test_mcp_server_state_creates_tool_executor(tmp_path, mock_env):
    """McpServerState should create a _tool_executor ThreadPoolExecutor."""
    from ivy_lsp.mcp.server import McpServerState

    state = McpServerState(root=str(tmp_path))
    assert state._tool_executor is not None
    assert isinstance(state._tool_executor, concurrent.futures.ThreadPoolExecutor)
    state._tool_executor.shutdown(wait=False)


def test_build_tool_context_wires_executor(tmp_path, mock_env):
    """build_tool_context should set tool_executor on ToolContext."""
    from ivy_lsp.mcp.server import McpServerState

    state = McpServerState(root=str(tmp_path))
    ctx = state.build_tool_context()
    assert ctx.tool_executor is state._tool_executor
    state._tool_executor.shutdown(wait=False)
