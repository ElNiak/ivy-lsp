"""Tests for the MCP sidecar /health endpoint."""

import asyncio
import json


def test_health_response_includes_workspace_root():
    """The /health endpoint must return workspace_root for validation."""
    from ivy_lsp.mcp.sidecar import _health_middleware_factory

    class MockCtx:
        root = "/tmp/test-workspace"

        def get_model_status(self):
            return {"state": "ready"}

    middleware = _health_middleware_factory(MockCtx(), start_time=0.0)

    scope = {"type": "http", "path": "/health"}
    response_body = b""

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal response_body
        if message["type"] == "http.response.body":
            response_body = message.get("body", b"")

    asyncio.get_event_loop().run_until_complete(middleware(scope, receive, send))

    body = json.loads(response_body)
    assert body["status"] == "ok"
    assert body["workspace_root"] == "/tmp/test-workspace"
    assert "uptime_seconds" in body
