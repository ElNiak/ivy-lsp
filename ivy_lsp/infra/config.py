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


def _dict_env(name: str) -> dict[str, str]:
    """Parse ``KEY=VAL,KEY=VAL`` env var into a dict."""
    raw = os.environ.get(name, "")
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                result[k] = v
    return result


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
    compile_workers: int = 2
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

    # Model build
    model_build_timeout: float = 600.0
    model_retry_cooldown: float = 30.0
    req_graph_cooldown: float = 30.0
    prewarm_model: bool = True

    # Tool timeout
    tool_timeout_scale: float = 1.0
    max_raw_output_length: int = 2000
    max_result_chars: int = 8000

    # MCP bridge per-request timeout (seconds)
    bridge_timeout: float = 120.0

    # Sidecar delegation timeout (seconds).  When the standalone MCP server
    # delegates a tool call to the LSP sidecar, this is the maximum time to
    # wait for the sidecar response before falling back to local execution.
    sidecar_delegation_timeout: float = 8.0

    # Debug tracing
    debug_log: bool = False
    debug_log_path: str | None = None
    trace_all_functions: bool = False

    # Parsing tier override (0 = auto, 1/2/3 = force specific tier)
    force_tier: int = 0

    # Session observability
    observability_enabled: bool = False
    observability_dir: str | None = None

    # Per-subsystem log level overrides (e.g. {"parsing": "DEBUG", "mcp": "WARNING"})
    subsystem_levels: dict[str, str] = field(default_factory=dict)

    # RFC service
    rfc_cache_dir: str | None = None
    rfc_cache_ttl: int = 3600
    rfc_local_dir: str | None = None
    rfc_offline: bool = False

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Read all ``IVY_LSP_*`` environment variables and return a config."""
        debug_log = _bool_env("IVY_LSP_DEBUG_LOG", "0")
        obs_raw = os.environ.get("IVY_OBSERVABILITY_ENABLED")
        observability_enabled = debug_log if obs_raw is None else (obs_raw != "0")

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
            compile_workers=max(1, _int_env("IVY_LSP_COMPILE_WORKERS", 2)),
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
            model_build_timeout=_float_env(
                "IVY_LSP_MODEL_BUILD_TIMEOUT", 600.0, floor=30.0
            ),
            model_retry_cooldown=_float_env(
                "IVY_LSP_MODEL_RETRY_COOLDOWN", 30.0, floor=5.0
            ),
            req_graph_cooldown=_float_env(
                "IVY_LSP_REQ_GRAPH_COOLDOWN", 30.0, floor=5.0
            ),
            prewarm_model=_bool_env("IVY_LSP_PREWARM_MODEL"),
            tool_timeout_scale=_float_env("IVY_LSP_TOOL_TIMEOUT_SCALE", 1.0, floor=0.1),
            max_raw_output_length=_int_env("IVY_LSP_MAX_RAW_OUTPUT_LENGTH", 2000),
            max_result_chars=_int_env("IVY_LSP_MAX_RESULT_CHARS", 8000),
            debug_log=debug_log,
            debug_log_path=os.environ.get("IVY_LSP_DEBUG_LOG_PATH"),
            bridge_timeout=_float_env("IVY_LSP_BRIDGE_TIMEOUT", 120.0, floor=10.0),
            sidecar_delegation_timeout=_float_env(
                "IVY_LSP_SIDECAR_TIMEOUT", 8.0, floor=2.0
            ),
            trace_all_functions=_bool_env("IVY_LSP_TRACE_ALL_FUNCTIONS", "0"),
            force_tier=_int_env("IVY_LSP_FORCE_TIER", 0),
            observability_enabled=observability_enabled,
            observability_dir=os.environ.get("IVY_OBSERVABILITY_DIR"),
            subsystem_levels=_dict_env("IVY_LSP_SUBSYSTEM_LEVELS"),
            rfc_cache_dir=os.environ.get("IVY_LSP_RFC_CACHE_DIR"),
            rfc_cache_ttl=_int_env("IVY_LSP_RFC_CACHE_TTL", 3600),
            rfc_local_dir=os.environ.get("IVY_LSP_RFC_LOCAL_DIR"),
            rfc_offline=_bool_env("IVY_LSP_RFC_OFFLINE", "0"),
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
