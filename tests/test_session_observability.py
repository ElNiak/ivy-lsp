"""Tests for session-based observability logging."""

from __future__ import annotations

import json

import pytest

from ivy_lsp.config import reset_config
from ivy_lsp.debug_trace import init_tracer
from ivy_lsp.session_observability import (
    get_session_logger,
    reset_session_logger,
    resolve_session_log_dir,
)


@pytest.fixture(autouse=True)
def _reset_observability_state():
    reset_config()
    reset_session_logger()
    yield
    reset_config()
    reset_session_logger()


def _read_events(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_resolve_log_dir_prefers_explicit_observability_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("IVY_OBSERVABILITY_DIR", str(tmp_path / "obs"))
    monkeypatch.setenv("IVY_WORKSPACE_ROOT", str(tmp_path / "workspace"))

    resolved = resolve_session_log_dir("s1")

    assert resolved == tmp_path / "obs" / "sessions" / "s1"


def test_resolve_log_dir_falls_back_to_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("IVY_OBSERVABILITY_DIR", raising=False)
    monkeypatch.setenv("IVY_WORKSPACE_ROOT", str(tmp_path / "workspace"))

    resolved = resolve_session_log_dir("s2")

    assert resolved == tmp_path / "workspace" / ".observability" / "sessions" / "s2"


def test_session_logger_writes_mcp_events(monkeypatch, tmp_path):
    monkeypatch.setenv("IVY_LSP_DEBUG_LOG", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_ENABLED", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_DIR", str(tmp_path / "obs"))
    monkeypatch.setenv("IVY_SESSION_ID", "session-123")

    logger = get_session_logger()
    logger.log_event(
        channel="mcp",
        event_type="call_start",
        name="ivy_test_tool",
        status="started",
        call_id="cid-1",
        payload={"args": ["alpha.ivy"], "kwargs": {"limit": 3}},
    )
    logger.log_event(
        channel="mcp",
        event_type="call_end",
        name="ivy_test_tool",
        status="ok",
        call_id="cid-1",
        duration_ms=10.5,
        payload={"result_type": "dict"},
    )

    events_file = tmp_path / "obs" / "sessions" / "session-123" / "events.jsonl"
    assert events_file.exists()

    events = _read_events(events_file)
    assert [event["event_type"] for event in events[-2:]] == [
        "call_start",
        "call_end",
    ]
    assert all(event["channel"] == "mcp" for event in events[-2:])
    assert events[-1]["status"] == "ok"
    assert events[-1]["name"] == "ivy_test_tool"


def test_lsp_trace_writes_session_event(monkeypatch, tmp_path):
    monkeypatch.setenv("IVY_LSP_DEBUG_LOG", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_ENABLED", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_DIR", str(tmp_path / "obs"))
    monkeypatch.setenv("IVY_SESSION_ID", "session-lsp")

    tracer = init_tracer(workspace_root=str(tmp_path / "workspace"))
    tracer.trace_lsp_request(
        method="textDocument/hover",
        filepath=str(tmp_path / "workspace" / "alpha.ivy"),
        position="10:4",
        word="send",
        source="semantic",
        result_summary="Hover content",
    )

    events_file = tmp_path / "obs" / "sessions" / "session-lsp" / "events.jsonl"
    events = _read_events(events_file)
    assert events[-1]["channel"] == "lsp"
    assert events[-1]["event_type"] == "request"
    assert events[-1]["name"] == "textDocument/hover"
    assert events[-1]["status"] == "ok"
    assert events[-1]["payload"]["source"] == "semantic"


def test_lsp_trace_persists_status_and_call_id(monkeypatch, tmp_path):
    monkeypatch.setenv("IVY_LSP_DEBUG_LOG", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_ENABLED", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_DIR", str(tmp_path / "obs"))
    monkeypatch.setenv("IVY_SESSION_ID", "session-lsp-status")

    tracer = init_tracer(workspace_root=str(tmp_path / "workspace"))
    tracer.trace_lsp_request(
        method="textDocument/documentSymbol",
        filepath=str(tmp_path / "workspace" / "alpha.ivy"),
        status="degraded",
        call_id="lsp-123",
        result_summary="1 symbols",
    )

    events_file = tmp_path / "obs" / "sessions" / "session-lsp-status" / "events.jsonl"
    events = _read_events(events_file)
    assert events[-1]["status"] == "degraded"
    assert events[-1]["call_id"] == "lsp-123"
