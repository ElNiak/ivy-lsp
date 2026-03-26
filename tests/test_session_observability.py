"""Tests for session-based observability logging."""

from __future__ import annotations

import hashlib
import json

import pytest

from ivy_lsp.infra.config import reset_config
from ivy_lsp.infra.observability import (
    _read_session_file,
    get_session_id,
    get_session_logger,
    init_tracer,
    reset_session_cache,
    reset_session_logger,
    resolve_session_log_dir,
    workspace_hash,
)


@pytest.fixture(autouse=True)
def _reset_observability_state():
    reset_config()
    reset_session_logger()
    reset_session_cache()
    yield
    reset_config()
    reset_session_logger()
    reset_session_cache()


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


# --- Session file resolution tests ---


def testworkspace_hash_matches_shell_convention():
    """Python hash must match: printf '%s' "$path" | shasum -a 256 | cut -c1-12."""
    path = "/Users/test/workspace"
    expected = hashlib.sha256(path.encode()).hexdigest()[:12]
    assert workspace_hash(path) == expected
    assert len(workspace_hash(path)) == 12


def test_read_session_file_returns_id(tmp_path):
    ws = str(tmp_path / "workspace")
    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    session_file = tmp_path / f"ivy-session-{ws_hash}.id"
    session_file.write_text("2026-03-25T1430-abc123\n")

    result = _read_session_file(ws, session_dir=str(tmp_path))
    assert result == "2026-03-25T1430-abc123"


def test_read_session_file_returns_none_when_missing(tmp_path):
    result = _read_session_file("/nonexistent/path", session_dir=str(tmp_path))
    assert result is None


def test_read_session_file_caches_within_ttl(tmp_path):
    ws = str(tmp_path / "workspace")
    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    session_file = tmp_path / f"ivy-session-{ws_hash}.id"
    session_file.write_text("2026-03-25T1430-first\n")

    result1 = _read_session_file(ws, session_dir=str(tmp_path))
    assert result1 == "2026-03-25T1430-first"

    session_file.write_text("2026-03-25T1430-second\n")
    result2 = _read_session_file(ws, session_dir=str(tmp_path))
    assert result2 == "2026-03-25T1430-first"  # cached


def test_read_session_file_expires_after_ttl(tmp_path):
    ws = str(tmp_path / "workspace")
    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    session_file = tmp_path / f"ivy-session-{ws_hash}.id"
    session_file.write_text("2026-03-25T1430-old\n")

    _read_session_file(ws, session_dir=str(tmp_path))
    session_file.write_text("2026-03-25T1431-new\n")

    # Backdate cache entry to simulate TTL expiry
    import ivy_lsp.infra.observability.session as obs_mod

    cached = obs_mod._session_cache.get(ws)
    assert cached is not None
    obs_mod._session_cache[ws] = (cached[0] - 10.0, cached[1])

    result = _read_session_file(ws, session_dir=str(tmp_path))
    assert result == "2026-03-25T1431-new"


def test_read_session_file_per_workspace_isolation(tmp_path):
    ws_a = str(tmp_path / "workspace-a")
    ws_b = str(tmp_path / "workspace-b")
    hash_a = hashlib.sha256(ws_a.encode()).hexdigest()[:12]
    hash_b = hashlib.sha256(ws_b.encode()).hexdigest()[:12]
    (tmp_path / f"ivy-session-{hash_a}.id").write_text("session-a\n")
    (tmp_path / f"ivy-session-{hash_b}.id").write_text("session-b\n")

    assert _read_session_file(ws_a, session_dir=str(tmp_path)) == "session-a"
    assert _read_session_file(ws_b, session_dir=str(tmp_path)) == "session-b"


def test_get_session_id_prefers_env_var(monkeypatch):
    monkeypatch.setenv("IVY_SESSION_ID", "from-env")
    assert get_session_id() == "from-env"


def test_get_session_id_falls_back_to_file(tmp_path, monkeypatch):
    monkeypatch.delenv("IVY_SESSION_ID", raising=False)
    ws = str(tmp_path / "workspace")
    monkeypatch.setenv("IVY_WORKSPACE_ROOT", ws)
    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    (tmp_path / f"ivy-session-{ws_hash}.id").write_text("2026-03-25T1430-file\n")

    assert get_session_id(session_dir=str(tmp_path)) == "2026-03-25T1430-file"


def test_get_session_id_returns_unknown_when_no_source(tmp_path, monkeypatch):
    monkeypatch.delenv("IVY_SESSION_ID", raising=False)
    monkeypatch.delenv("IVY_WORKSPACE_ROOT", raising=False)
    assert get_session_id(session_dir=str(tmp_path)) == "unknown"


def test_get_session_id_race_then_resolve(tmp_path, monkeypatch):
    """File absent -> unknown; file written -> resolves immediately (no TTL wait)."""
    monkeypatch.delenv("IVY_SESSION_ID", raising=False)
    ws = str(tmp_path / "workspace")
    monkeypatch.setenv("IVY_WORKSPACE_ROOT", ws)

    assert get_session_id(session_dir=str(tmp_path)) == "unknown"

    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    (tmp_path / f"ivy-session-{ws_hash}.id").write_text("2026-03-25T1430-real\n")

    assert get_session_id(session_dir=str(tmp_path)) == "2026-03-25T1430-real"


def test_session_logger_transitions_on_file_change(tmp_path, monkeypatch):
    """Logger auto-rebuilds when session ID changes (via _LoggerKey)."""
    monkeypatch.delenv("IVY_SESSION_ID", raising=False)
    monkeypatch.setenv("IVY_LSP_DEBUG_LOG", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_ENABLED", "1")
    monkeypatch.setenv("IVY_OBSERVABILITY_DIR", str(tmp_path / "obs"))

    ws = str(tmp_path / "workspace")
    monkeypatch.setenv("IVY_WORKSPACE_ROOT", ws)

    ws_hash = hashlib.sha256(ws.encode()).hexdigest()[:12]
    session_file = tmp_path / f"ivy-session-{ws_hash}.id"

    # Phase 1: no file -> logger gets "unknown"
    logger1 = get_session_logger(session_dir=str(tmp_path))
    logger1.log_event(channel="mcp", event_type="test", name="t1", status="ok")
    assert (tmp_path / "obs" / "sessions" / "unknown" / "events.jsonl").exists()

    # Phase 2: file written -> after reset, logger transitions
    session_file.write_text("2026-03-25T1430-real\n")
    reset_session_cache()
    reset_session_logger()

    logger2 = get_session_logger(session_dir=str(tmp_path))
    logger2.log_event(channel="mcp", event_type="test", name="t2", status="ok")
    assert (
        tmp_path / "obs" / "sessions" / "2026-03-25T1430-real" / "events.jsonl"
    ).exists()
