"""Centralized observability package for the Ivy LSP server.

Re-exports the public API from submodules for convenient single-source imports:
``from ivy_lsp.infra.observability import get_logger, timed_phase, ...``
"""

from ivy_lsp.infra.observability.core import (
    LogCategory,
    LogEvent,
    StructuredLogAdapter,
    _log_session_event,
    call_context,
    describe_logging_config,
    enable_package_instrumentation,
    ensure_call_id,
    get_call_id,
    get_logger,
    instrument_function,
    instrument_module_functions,
    log_phase,
    timed_phase,
)
from ivy_lsp.infra.observability.handlers import (
    DebugTracer,
    DedupFilter,
    ToolTraceContext,
    get_tracer,
    init_tracer,
    trace_tool,
)
from ivy_lsp.infra.observability.session import (
    SessionEventLogger,
    SessionJsonLogHandler,
    _read_session_file,
    get_error_count,
    get_session_id,
    get_session_logger,
    install_session_jsonl_handler,
    reset_session_cache,
    reset_session_logger,
    resolve_session_id,
    resolve_session_log_dir,
    workspace_hash,
)

__all__ = [
    # core
    "LogCategory",
    "LogEvent",
    "StructuredLogAdapter",
    "_log_session_event",
    "call_context",
    "describe_logging_config",
    "enable_package_instrumentation",
    "ensure_call_id",
    "get_call_id",
    "get_logger",
    "instrument_function",
    "instrument_module_functions",
    "log_phase",
    "timed_phase",
    # handlers
    "DebugTracer",
    "DedupFilter",
    "ToolTraceContext",
    "get_tracer",
    "init_tracer",
    "trace_tool",
    # session
    "SessionEventLogger",
    "SessionJsonLogHandler",
    "_read_session_file",
    "workspace_hash",
    "get_error_count",
    "get_session_id",
    "get_session_logger",
    "install_session_jsonl_handler",
    "reset_session_cache",
    "reset_session_logger",
    "resolve_session_id",
    "resolve_session_log_dir",
]
