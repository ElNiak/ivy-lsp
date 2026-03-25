"""Shared observability primitives: correlation, structured logging, timing, and instrumentation.

Consolidates ivy_lsp.correlation, ivy_lsp.structured_logging, and the
original ivy_lsp.observability into a single module with a lightweight
logger factory for per-subsystem verbosity control.
"""

from __future__ import annotations

import contextvars
import importlib
import inspect
import json
import logging
import pkgutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    MutableMapping,
    Optional,
    Tuple,
    TypeVar,
    cast,
)

# ---------------------------------------------------------------------------
# 1. Correlation context
# ---------------------------------------------------------------------------

_call_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ivy_lsp_call_id",
    default=None,
)


def get_call_id() -> str | None:
    """Return the current correlation id, if one is set."""
    return _call_id_var.get()


def ensure_call_id(prefix: str = "call") -> str:
    """Return the active correlation id, creating one if missing."""
    current = _call_id_var.get()
    if current:
        return current
    created = f"{prefix}-{uuid.uuid4().hex[:12]}"
    _call_id_var.set(created)
    return created


@contextmanager
def call_context(
    call_id: str | None = None, *, prefix: str = "call"
) -> Generator[str, None, None]:
    """Temporarily set the current correlation id within a context."""
    resolved = call_id or f"{prefix}-{uuid.uuid4().hex[:12]}"
    token = _call_id_var.set(resolved)
    try:
        yield resolved
    finally:
        _call_id_var.reset(token)


# ---------------------------------------------------------------------------
# 2. Structured logging primitives
# ---------------------------------------------------------------------------


class LogCategory(str, Enum):
    """Categories used to classify structured log events."""

    MILESTONE = "MIL"
    ACTIVITY = "ACT"
    DIAGNOSTIC = "DIA"
    PERFORMANCE = "PER"


@dataclass
class LogEvent:
    """A structured log event carrying a category, phase, and data payload."""

    category: LogCategory
    phase: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


class StructuredLogAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Logging adapter that formats messages with category and phase prefixes."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> Tuple[str, MutableMapping[str, Any]]:
        """Prepend category/phase prefix and append JSON data to msg."""
        extra = kwargs.get("extra", {})
        event: Optional[LogEvent] = extra.pop("event", None)
        if event is None:
            return msg, kwargs

        if event.phase:
            prefix = f"[{event.category.value}:{event.phase}]"
        else:
            prefix = f"[{event.category.value}]"

        if event.data:
            try:
                data_str = json.dumps(event.data, default=str)
            except (TypeError, ValueError):
                data_str = str(event.data)
            formatted = f"{prefix} {msg} | {data_str}"
        else:
            formatted = f"{prefix} {msg}"

        return formatted, kwargs


# ---------------------------------------------------------------------------
# 3. Observability helpers (timing, instrumentation)
# ---------------------------------------------------------------------------

F = TypeVar("F", bound=Callable[..., Any])
_WRAPPED_ATTR = "__ivy_lsp_instrumented__"


def _log_session_event(
    *,
    channel: str,
    event_type: str,
    name: str,
    status: str,
    duration_ms: float | None = None,
    payload: dict[str, Any] | None = None,
    call_id: str | None = None,
) -> None:
    try:
        from ivy_lsp.infra.observability.session import get_session_logger

        get_session_logger().log_event(
            channel=channel,
            event_type=event_type,
            name=name,
            status=status,
            duration_ms=duration_ms,
            payload=payload,
            call_id=call_id,
        )
    except Exception:
        # Observability must not affect protocol behavior.
        pass


def log_phase(
    logger: logging.Logger,
    *,
    category: LogCategory,
    phase: str,
    message: str,
    data: dict[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one structured log event with current correlation context."""
    call_id = get_call_id()
    payload: dict[str, Any] = dict(data or {})
    if call_id and "call_id" not in payload:
        payload["call_id"] = call_id
    StructuredLogAdapter(logger, {}).log(
        level,
        message,
        extra={"event": LogEvent(category, phase, payload)},
    )


@contextmanager
def timed_phase(
    logger: logging.Logger,
    *,
    category: LogCategory,
    phase: str,
    name: str,
    channel: str = "core",
    payload: dict[str, Any] | None = None,
) -> Generator[str, None, None]:
    """Time a code block and emit start/success/failure events."""
    call_id = ensure_call_id("phase")
    start = time.perf_counter()
    base_payload = dict(payload or {})
    base_payload["call_id"] = call_id

    log_phase(
        logger,
        category=category,
        phase=phase,
        message=f"{name} started",
        data=base_payload,
        level=logging.DEBUG,
    )
    _log_session_event(
        channel=channel,
        event_type="phase",
        name=name,
        status="start",
        payload=base_payload,
        call_id=call_id,
    )

    try:
        yield call_id
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        err_payload = dict(base_payload)
        err_payload.update(
            {
                "duration_ms": round(elapsed, 2),
                "error": type(exc).__name__,
            }
        )
        log_phase(
            logger,
            category=category,
            phase=phase,
            message=f"{name} failed",
            data=err_payload,
            level=logging.WARNING,
        )
        _log_session_event(
            channel=channel,
            event_type="phase",
            name=name,
            status="error",
            duration_ms=elapsed,
            payload={"error": type(exc).__name__},
            call_id=call_id,
        )
        raise

    elapsed = (time.perf_counter() - start) * 1000
    done_payload = dict(base_payload)
    done_payload["duration_ms"] = round(elapsed, 2)
    log_phase(
        logger,
        category=category,
        phase=phase,
        message=f"{name} completed",
        data=done_payload,
        level=logging.INFO,
    )
    _log_session_event(
        channel=channel,
        event_type="phase",
        name=name,
        status="ok",
        duration_ms=elapsed,
        call_id=call_id,
    )


_instrument_tls = threading.local()


def instrument_function(
    *,
    category: LogCategory = LogCategory.ACTIVITY,
    phase: str = "function",
    channel: str = "core",
) -> Callable[[F], F]:
    """Decorator that emits timing + status for sync and async callables.

    Uses a thread-local depth counter to prevent cross-function recursion:
    only the outermost instrumented call emits ``timed_phase``; nested
    instrumented calls execute directly without timing/logging overhead.
    """

    def _decorator(func: F) -> F:
        module_logger = logging.getLogger(func.__module__)
        name = f"{func.__module__}.{func.__qualname__}"

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                depth = getattr(_instrument_tls, "depth", 0)
                if depth > 0:
                    return await cast(Callable[..., Any], func)(*args, **kwargs)
                _instrument_tls.depth = depth + 1
                try:
                    with timed_phase(
                        module_logger,
                        category=category,
                        phase=phase,
                        name=name,
                        channel=channel,
                    ):
                        return await cast(Callable[..., Any], func)(*args, **kwargs)
                finally:
                    _instrument_tls.depth = depth

            return cast(F, _async_wrapper)

        @wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            depth = getattr(_instrument_tls, "depth", 0)
            if depth > 0:
                return cast(Callable[..., Any], func)(*args, **kwargs)
            _instrument_tls.depth = depth + 1
            try:
                with timed_phase(
                    module_logger,
                    category=category,
                    phase=phase,
                    name=name,
                    channel=channel,
                ):
                    return cast(Callable[..., Any], func)(*args, **kwargs)
            finally:
                _instrument_tls.depth = depth

        return cast(F, _sync_wrapper)

    return _decorator


def _already_wrapped(obj: Any) -> bool:
    return bool(getattr(obj, _WRAPPED_ATTR, False))


def _mark_wrapped(obj: Any) -> None:
    try:
        setattr(obj, _WRAPPED_ATTR, True)
    except Exception:
        pass


def instrument_module_functions(
    module: Any,
    *,
    category: LogCategory = LogCategory.ACTIVITY,
    phase: str = "deep-trace",
    channel: str = "core",
) -> int:
    """Wrap module functions and class methods with deep instrumentation.

    Returns the number of wrapped callables.
    """
    wrapped = 0
    module_name = getattr(module, "__name__", "")
    module_dict = getattr(module, "__dict__", {})

    for name, obj in list(module_dict.items()):
        if name.startswith("__") and name.endswith("__"):
            continue

        if inspect.isfunction(obj) and getattr(obj, "__module__", "") == module_name:
            if _already_wrapped(obj):
                continue
            decorated = instrument_function(
                category=category,
                phase=phase,
                channel=channel,
            )(obj)
            _mark_wrapped(decorated)
            setattr(module, name, decorated)
            wrapped += 1
            continue

        if inspect.isclass(obj) and getattr(obj, "__module__", "") == module_name:
            for meth_name, meth in list(vars(obj).items()):
                if meth_name.startswith("__") and meth_name.endswith("__"):
                    continue
                if isinstance(meth, (staticmethod, classmethod)):
                    continue
                if not inspect.isfunction(meth):
                    continue
                if _already_wrapped(meth):
                    continue
                decorated_meth = instrument_function(
                    category=category,
                    phase=phase,
                    channel=channel,
                )(meth)
                _mark_wrapped(decorated_meth)
                setattr(obj, meth_name, decorated_meth)
                wrapped += 1

    return wrapped


def enable_package_instrumentation(
    package_name: str,
    *,
    category: LogCategory = LogCategory.ACTIVITY,
    phase: str = "deep-trace",
    channel: str = "core",
) -> dict[str, int]:
    """Import and instrument a package and all its submodules.

    Intended for opt-in debugging where broad function-level tracing is needed.
    """
    logger = logging.getLogger(__name__)
    package = importlib.import_module(package_name)
    modules_scanned = 0
    callables_wrapped = 0

    # Modules that form the instrumentation/logging infrastructure.
    # Wrapping these creates circular calls because the wrappers
    # themselves use timed_phase -> log_phase -> get_session_logger
    # -> get_config, so any module in that call chain must be excluded.
    _skip = frozenset(
        {
            f"{package_name}.observability",
            f"{package_name}.observability.core",
            f"{package_name}.observability.session",
            f"{package_name}.observability.handlers",
            f"{package_name}.config",
        }
    )

    if package_name not in _skip:
        callables_wrapped += instrument_module_functions(
            package,
            category=category,
            phase=phase,
            channel=channel,
        )
        modules_scanned += 1

    pkg_path = getattr(package, "__path__", None)
    if pkg_path is None:
        return {"modules": modules_scanned, "wrapped": callables_wrapped}

    prefix = f"{package_name}."
    for mod_info in pkgutil.walk_packages(pkg_path, prefix=prefix):
        mod_name = mod_info.name
        # Skip tests, caches, and generated pycache paths.
        if ".__pycache__" in mod_name or mod_name.endswith(".__main__"):
            continue
        if mod_name in _skip:
            logger.debug("Skipping self-instrumentation for %s", mod_name)
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            logger.debug("Skipping instrumentation import failure for %s", mod_name)
            continue
        modules_scanned += 1
        callables_wrapped += instrument_module_functions(
            mod,
            category=category,
            phase=phase,
            channel=channel,
        )

    return {"modules": modules_scanned, "wrapped": callables_wrapped}


# ---------------------------------------------------------------------------
# 4. Logger factory and diagnostics
# ---------------------------------------------------------------------------

# Subsystem detection map
SUBSYSTEM_MAP = {
    "parsing": "ivy_lsp.parsing",
    "indexer": "ivy_lsp.indexer",
    "compilation": "ivy_lsp.compilation",
    "semantic": "ivy_lsp.semantic",
    "features": "ivy_lsp.features",
    "tools": "ivy_lsp.tools",
    "mcp": "ivy_lsp.mcp",
    "analysis": "ivy_lsp.analysis",
    "rfc": "ivy_lsp.rfc",
    "adapters": "ivy_lsp.adapters",
}

# Reverse map for auto-detection
_PREFIX_TO_SUBSYSTEM: dict[str, str] = {}
for _sub, _prefix in SUBSYSTEM_MAP.items():
    _PREFIX_TO_SUBSYSTEM[_prefix] = _sub


def _detect_subsystem(name: str) -> str | None:
    """Auto-detect subsystem from a logger name like 'ivy_lsp.parsing.tiered_extractor'."""
    for prefix, subsystem in _PREFIX_TO_SUBSYSTEM.items():
        if name == prefix or name.startswith(prefix + "."):
            return subsystem
    return None


_logger_cache: dict[str, logging.Logger] = {}


def get_logger(name: str, *, subsystem: str | None = None) -> logging.Logger:
    """Return a configured logger for the ivy-lsp package.

    Uses Python's standard hierarchy (propagate=True) so root handlers
    (rotating file, LspLogHandler, SessionJsonLogHandler, DedupFilter)
    all see the events. Only sets the effective level per subsystem.
    """
    if name in _logger_cache:
        return _logger_cache[name]

    logger = logging.getLogger(name)
    resolved_subsystem = subsystem or _detect_subsystem(name)
    if resolved_subsystem:
        from ivy_lsp.infra.config import get_config

        try:
            level_str = get_config().subsystem_levels.get(resolved_subsystem)
            if level_str:
                logger.setLevel(getattr(logging, level_str.upper(), logging.NOTSET))
        except Exception:
            pass  # Config not yet initialized

    _logger_cache[name] = logger
    return logger


def describe_logging_config() -> dict[str, Any]:
    """Return a summary of all active logging configuration for diagnostics."""
    from ivy_lsp.infra.config import get_config
    from ivy_lsp.infra.observability.session import (
        get_session_id,
        resolve_session_log_dir,
    )

    cfg = get_config()
    session_id = get_session_id()
    return {
        "log_level": cfg.log_level,
        "activity_level": cfg.activity_level,
        "subsystem_levels": dict(cfg.subsystem_levels),
        "debug_log": cfg.debug_log,
        "debug_log_path": cfg.debug_log_path,
        "observability_enabled": cfg.observability_enabled,
        "observability_dir": cfg.observability_dir,
        "trace_all_functions": cfg.trace_all_functions,
        "handlers": [type(h).__name__ for h in logging.getLogger().handlers],
        "session_id": session_id,
        "session_log_dir": str(resolve_session_log_dir(session_id)),
    }
