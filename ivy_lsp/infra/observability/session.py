"""Session-scoped observability logging for ivy-lsp.

Writes structured JSON events to per-session JSONL files using a path layout
compatible with panther-ivy-plugin hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
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


def workspace_hash(workspace_root: str) -> str:
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

    ws_hash = workspace_hash(workspace_root)
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
      3. ``CLAUDE_SESSION_ID`` environment variable (inherited from Claude Code)
      4. ``"unknown"`` fallback
    """
    from_env = os.environ.get("IVY_SESSION_ID", "").strip()
    if from_env:
        return from_env

    ws_root = os.environ.get("IVY_WORKSPACE_ROOT", "").strip() or os.getcwd()
    from_file = _read_session_file(ws_root, session_dir=session_dir)
    if from_file:
        return from_file

    claude_sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if claude_sid:
        return claude_sid

    return "unknown"


def resolve_session_id(
    hook_payload: dict | None = None,
    *,
    session_dir: str = "/tmp",
) -> str:
    """Canonical session ID resolution matching detect-ivy-workspace.sh boot order.

    Priority:
      1. ``hook_payload["session_id"]`` (boot-time primary)
      2. ``CLAUDE_SESSION_ID``
      3. ``CLAUDE_CODE_SESSION_ID``
      4. ``IVY_SESSION_ID`` (already date-prefixed by boot hook)
      5. ``/tmp/ivy-session-<ws_hash>.id`` file
      6. ``"unknown"`` fallback
    """
    if hook_payload:
        sid = str(hook_payload.get("session_id", "")).strip()
        if sid:
            return sid
    for env_var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        sid = os.environ.get(env_var, "").strip()
        if sid:
            return sid
    return get_session_id(session_dir=session_dir)


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


# --- Error reporting helper ---

_error_count = 0
_last_error_report = 0.0
_ERROR_REPORT_INTERVAL = 60.0  # seconds


def _report_logging_error(exc: Exception, context: str) -> None:
    """Report a logging subsystem error without disrupting the caller.

    Rate-limited to at most one stderr message per 60 seconds.
    Always increments the error counter (queryable via get_error_count).
    """
    global _error_count, _last_error_report
    _error_count += 1
    now = time.monotonic()
    if (now - _last_error_report) >= _ERROR_REPORT_INTERVAL:
        _last_error_report = now
        try:
            sys.stderr.write(
                f"[ivy-lsp-logging] {context}: {type(exc).__name__}: {exc} "
                f"(total errors: {_error_count})\n"
            )
            sys.stderr.flush()
        except Exception:
            pass  # stderr itself is broken


def get_error_count() -> int:
    """Return the total number of silenced logging errors."""
    return _error_count


def _maybe_rotate(
    filepath: Path,
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB
    backup_count: int = 5,
) -> None:
    """Rotate filepath when it exceeds max_bytes (POSIX-atomic, lock-free).

    Safe with concurrent writers that use open/close per write (no cached
    file handles).  Worst-case race: one extra rotation cycle, zero data loss.
    """
    try:
        size = filepath.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        return
    # Shift existing backups: .5 -> delete, .4 -> .5, ..., .1 -> .2
    for i in range(backup_count, 0, -1):
        src = filepath.parent / f"{filepath.name}.{i}"
        if i == backup_count:
            try:
                src.unlink()
            except OSError:
                pass
        else:
            dst = filepath.parent / f"{filepath.name}.{i + 1}"
            try:
                src.rename(dst)
            except OSError:
                pass
    # Rename current file to .1 — next append creates a fresh file
    try:
        filepath.rename(filepath.parent / f"{filepath.name}.1")
    except OSError:
        pass


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
        self._drop_count: int = 0

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
            _maybe_rotate(self.events_file)
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
        except (OSError, TypeError, ValueError) as exc:
            self._drop_count += 1
            _report_logging_error(exc, "session JSONL write")
            return None


_logger: SessionEventLogger | None = None
_logger_key: _LoggerKey | None = None
_logger_lock = threading.Lock()
_jsonl_handler: logging.Handler | None = None


class SessionJsonLogHandler(logging.Handler):
    """Logging handler that mirrors Python log records into session JSONL.

    Uses a thread-local re-entrance guard to prevent recursion when
    instrumented functions trigger log records during ``timed_phase``.
    """

    _guard = threading.local()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        # Re-entrance guard: prevent recursion from instrumented call chains
        # (e.g., get_session_logger -> get_config -> timed_phase -> log -> emit)
        if getattr(self._guard, "emitting", False):
            return
        self._guard.emitting = True
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
        except Exception as exc:
            # Never propagate logging failures.
            _report_logging_error(exc, "session JSONL handler")
            return
        finally:
            self._guard.emitting = False


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
    from ivy_lsp.infra.config import get_config

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
