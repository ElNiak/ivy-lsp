# tests/test_tool_executor.py
"""Tests for dedicated tool thread pool executor."""

from __future__ import annotations

import concurrent.futures

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
