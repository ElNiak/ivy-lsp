"""Centralized configuration for the Ivy Language Server.

All ``IVY_LSP_*`` environment variables are read **once** at startup and
exposed as fields on a frozen :class:`ServerConfig` dataclass.  Individual
modules should import and use the singleton ``get_config()`` rather than
reading ``os.environ`` directly.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import List


def _bool_env(name: str, default: str = "1") -> bool:
    """Read an env var as a boolean (``"0"`` is False, anything else True)."""
    return os.environ.get(name, default) != "0"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float, floor: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, float(raw))
    except (ValueError, TypeError):
        return default


def _csv_env(name: str) -> List[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class ServerConfig:
    """Immutable configuration read from ``IVY_LSP_*`` environment variables.

    Constructed via :meth:`from_env` at startup; consumers should call
    :func:`get_config` to obtain the singleton instance.
    """

    # Logging / activity
    log_level: str = "INFO"
    activity_level: str = "phase"

    # Workspace
    workspace: str | None = None
    workspace_root: str | None = None
    workspace_hint: str | None = None
    include_paths: List[str] = field(default_factory=list)
    exclude_paths: List[str] = field(default_factory=list)

    # Bulk analysis flags
    bulk_analysis: bool = True
    bulk_analysis_t2: bool = True
    bulk_compile: bool = True

    # Worker / concurrency
    compile_workers: int = 1
    compile_timeout: float = 300.0
    compile_cache_ttl: float = 600.0
    max_concurrent_tools: int = 4
    fast_index_workers: int = 4
    parse_workers: int = 0
    bulk_workers: int = 4

    # Timeouts
    lock_timeout: float = 30.0
    verify_timeout: float = 120.0
    tool_compile_timeout: float = 300.0
    show_model_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Read all ``IVY_LSP_*`` environment variables and return a config."""
        return cls(
            log_level=os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper(),
            activity_level=os.environ.get("IVY_LSP_ACTIVITY_LEVEL", "phase"),
            workspace=os.environ.get("IVY_LSP_WORKSPACE"),
            workspace_root=os.environ.get("IVY_WORKSPACE_ROOT"),
            workspace_hint=os.environ.get("IVY_LSP_WORKSPACE_HINT"),
            include_paths=_csv_env("IVY_LSP_INCLUDE_PATHS"),
            exclude_paths=_csv_env("IVY_LSP_EXCLUDE_PATHS"),
            bulk_analysis=_bool_env("IVY_LSP_BULK_ANALYSIS"),
            bulk_analysis_t2=_bool_env("IVY_LSP_BULK_ANALYSIS_T2"),
            bulk_compile=_bool_env("IVY_LSP_BULK_COMPILE"),
            compile_workers=max(1, _int_env("IVY_LSP_COMPILE_WORKERS", 1)),
            compile_timeout=_float_env("IVY_LSP_COMPILE_TIMEOUT", 300.0),
            compile_cache_ttl=_float_env("IVY_LSP_COMPILE_CACHE_TTL", 600.0),
            max_concurrent_tools=max(1, _int_env("IVY_LSP_MAX_CONCURRENT_TOOLS", 4)),
            fast_index_workers=_int_env("IVY_LSP_FAST_INDEX_WORKERS", 4),
            parse_workers=_int_env("IVY_LSP_PARSE_WORKERS", 0),
            bulk_workers=_int_env("IVY_LSP_BULK_WORKERS", 4),
            lock_timeout=_float_env("IVY_LSP_LOCK_TIMEOUT", 30.0),
            verify_timeout=_float_env("IVY_LSP_VERIFY_TIMEOUT", 120.0, floor=5.0),
            tool_compile_timeout=_float_env(
                "IVY_LSP_TOOL_COMPILE_TIMEOUT", 300.0, floor=10.0
            ),
            show_model_timeout=_float_env(
                "IVY_LSP_SHOW_MODEL_TIMEOUT", 30.0, floor=5.0
            ),
        )


# Module-level singleton, lazily initialised.
_config: ServerConfig | None = None
_config_lock = threading.Lock()
_config_session_id: str | None = None


def get_config() -> ServerConfig:
    """Return the global :class:`ServerConfig` singleton.

    On first call, reads environment variables via :meth:`ServerConfig.from_env`.
    If ``IVY_SESSION_ID`` changes (e.g. a new Claude session starts), the config
    is automatically rebuilt so fresh environment variables are picked up.
    Thread-safe via a double-checked locking pattern.
    """
    global _config, _config_session_id
    current_session = os.environ.get("IVY_SESSION_ID")
    # Fast path: config exists and session unchanged
    if _config is not None and current_session == _config_session_id:
        return _config
    with _config_lock:
        # Double-check after acquiring lock
        if _config is not None and current_session == _config_session_id:
            return _config
        _config = ServerConfig.from_env()
        _config_session_id = current_session
        return _config


def reset_config() -> None:
    """Reset the singleton so the next :func:`get_config` re-reads env vars.

    Intended for testing only.
    """
    global _config, _config_session_id
    with _config_lock:
        _config = None
        _config_session_id = None
