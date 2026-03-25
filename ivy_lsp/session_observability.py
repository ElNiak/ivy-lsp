"""Session-scoped observability logging for ivy-lsp.

Writes structured JSON events to per-session JSONL files using a path layout
compatible with panther-ivy-plugin hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Session file cache (keyed by workspace root) ---
_session_cache: dict[str, tuple[float, str]] = (
    {}
)  # ws_root -> (monotonic_time, session_id)
_SESSION_CACHE_TTL = 5.0  # seconds


def _workspace_hash(workspace_root: str) -> str:
    """12-char SHA-256 hex hash matching the shell convention."""
    return hashlib.sha256(workspace_root.encode()).hexdigest()[:12]


def _read_session_file(
    workspace_root: str,
    *,
    session_dir: str = "/tmp",
) -> str | None:
    """Read session ID from the file written by the SessionStart hook.

    Returns None if the file is missing, empty, or unreadable.
    Results are cached per workspace root for _SESSION_CACHE_TTL seconds.
    """
    now = time.monotonic()
    cached = _session_cache.get(workspace_root)
    if cached and (now - cached[0]) < _SESSION_CACHE_TTL:
        return cached[1]

    ws_hash = _workspace_hash(workspace_root)
    path = Path(session_dir) / f"ivy-session-{ws_hash}.id"
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if value:
        _session_cache[workspace_root] = (now, value)
        return value
    return None


def reset_session_cache() -> None:
    """Clear the session file cache (for tests)."""
    _session_cache.clear()


def get_session_id(*, session_dir: str = "/tmp") -> str:
    """Return the active session identifier, or ``unknown`` when missing.

    Resolution order:
      1. ``IVY_SESSION_ID`` environment variable (explicit override)
      2. ``/tmp/ivy-session-<ws_hash>.id`` file (written by SessionStart hook)
      3. ``"unknown"`` fallback
    """
    from_env = os.environ.get("IVY_SESSION_ID", "").strip()
    if from_env:
        return from_env

    ws_root = os.environ.get("IVY_WORKSPACE_ROOT", "").strip() or os.getcwd()
    from_file = _read_session_file(ws_root, session_dir=session_dir)
    if from_file:
        return from_file

    return "unknown"


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
_jsonl_handler: logging.Handler | None = None


class SessionJsonLogHandler(logging.Handler):
    """Logging handler that mirrors Python log records into session JSONL."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            logger = get_session_logger()
            payload = {
                "logger": record.name,
                "level": record.levelname,
            }
            if hasattr(record, "module"):
                payload["module"] = record.module
            if hasattr(record, "funcName"):
                payload["function"] = record.funcName
            if hasattr(record, "lineno"):
                payload["line"] = record.lineno

            logger.log_event(
                channel="python-log",
                event_type="log",
                name=record.name,
                status=record.levelname.lower(),
                payload={
                    **payload,
                    "message": record.getMessage(),
                },
            )
        except Exception:
            # Never propagate logging failures.
            return


def install_session_jsonl_handler() -> None:
    """Install a singleton root logging handler for session JSONL mirroring."""
    global _jsonl_handler
    root = logging.getLogger()
    with _logger_lock:
        if _jsonl_handler is not None:
            return
        handler = SessionJsonLogHandler(level=logging.DEBUG)
        root.addHandler(handler)
        _jsonl_handler = handler


def get_session_logger(*, session_dir: str = "/tmp") -> SessionEventLogger:
    """Return a session logger keyed by current config/session state."""
    from ivy_lsp.config import get_config

    cfg = get_config()
    session_id = get_session_id(session_dir=session_dir)
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
        reset_session_cache()
