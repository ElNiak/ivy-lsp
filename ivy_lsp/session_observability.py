"""Session-scoped observability logging for ivy-lsp.

Writes structured JSON events to per-session JSONL files using a path layout
compatible with panther-ivy-plugin hooks.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def get_session_id() -> str:
    """Return the active session identifier, or ``unknown`` when missing."""
    session_id = os.environ.get("IVY_SESSION_ID", "").strip()
    return session_id or "unknown"


def resolve_session_log_dir(
    session_id: str,
    *,
    observability_dir: str | None = None,
    workspace_root: str | None = None,
) -> Path:
    """Resolve per-session log directory with plugin-compatible priority.

    Priority:
      1. ``$IVY_OBSERVABILITY_DIR/sessions/<session_id>/``
      2. ``$IVY_WORKSPACE_ROOT/.observability/sessions/<session_id>/``
      3. ``/tmp/ivy-observability/sessions/<session_id>/``
    """
    explicit = (
        observability_dir or os.environ.get("IVY_OBSERVABILITY_DIR", "")
    ).strip()
    if explicit:
        return Path(explicit) / "sessions" / session_id

    workspace = (workspace_root or os.environ.get("IVY_WORKSPACE_ROOT", "")).strip()
    if workspace:
        return Path(workspace) / ".observability" / "sessions" / session_id

    return Path("/tmp/ivy-observability") / "sessions" / session_id


@dataclass(frozen=True)
class _LoggerKey:
    session_id: str
    enabled: bool
    observability_dir: str | None
    workspace_root: str | None


class SessionEventLogger:
    """Append structured events to ``events.jsonl`` for one session."""

    def __init__(  # noqa: D107
        self,
        *,
        session_id: str,
        enabled: bool,
        observability_dir: str | None,
        workspace_root: str | None,
    ) -> None:
        self._session_id = session_id
        self._enabled = enabled
        self._log_dir = resolve_session_log_dir(
            session_id,
            observability_dir=observability_dir,
            workspace_root=workspace_root,
        )

    @property
    def log_dir(self) -> Path:
        """Return the directory where session events are written."""
        return self._log_dir

    @property
    def events_file(self) -> Path:
        """Return the path to the JSONL events file."""
        return self._log_dir / "events.jsonl"

    def log_event(
        self,
        *,
        channel: str,
        event_type: str,
        name: str,
        status: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        call_id: str | None = None,
    ) -> Path | None:
        """Append one event to the session ``events.jsonl`` file.

        Never raises; returns ``None`` on failure or when disabled.
        """
        if not self._enabled:
            return None

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            event: dict[str, Any] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": self._session_id,
                "channel": channel,
                "event_type": event_type,
                "name": name,
                "status": status,
                "cwd": os.environ.get("PWD", os.getcwd()),
            }
            if duration_ms is not None:
                event["duration_ms"] = round(duration_ms, 2)
            if call_id:
                event["call_id"] = call_id
            if payload:
                event["payload"] = payload

            with open(self.events_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
            return self.events_file
        except (OSError, TypeError, ValueError):
            return None


_logger: SessionEventLogger | None = None
_logger_key: _LoggerKey | None = None
_logger_lock = threading.Lock()


def get_session_logger() -> SessionEventLogger:
    """Return a session logger keyed by current config/session state."""
    from ivy_lsp.config import get_config

    cfg = get_config()
    session_id = get_session_id()
    key = _LoggerKey(
        session_id=session_id,
        enabled=cfg.observability_enabled,
        observability_dir=cfg.observability_dir,
        workspace_root=cfg.workspace_root or cfg.workspace,
    )

    global _logger, _logger_key
    if _logger is not None and _logger_key == key:
        return _logger

    with _logger_lock:
        if _logger is not None and _logger_key == key:
            return _logger
        _logger = SessionEventLogger(
            session_id=key.session_id,
            enabled=key.enabled,
            observability_dir=key.observability_dir,
            workspace_root=key.workspace_root,
        )
        _logger_key = key
        return _logger


def reset_session_logger() -> None:
    """Reset singleton logger state (intended for tests)."""
    global _logger, _logger_key
    with _logger_lock:
        _logger = None
        _logger_key = None
