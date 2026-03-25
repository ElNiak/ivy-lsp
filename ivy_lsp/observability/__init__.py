"""Centralized observability package for the Ivy LSP server.

Re-exports the public API from submodules for convenient single-source imports:
``from ivy_lsp.observability import get_logger, timed_phase, ...``
"""

from ivy_lsp.observability.core import (
    IvyLogAdapter,
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
from ivy_lsp.observability.handlers import (
    DebugTracer,
    DedupFilter,
    LspLogHandler,
    ToolTraceContext,
    _LspLogHandler,
    get_tracer,
    init_tracer,
    trace_tool,
)
from ivy_lsp.observability.session import (
    SessionEventLogger,
    SessionJsonLogHandler,
    _read_session_file,
    _workspace_hash,
    get_error_count,
    get_session_id,
    get_session_logger,
    install_session_jsonl_handler,
    reset_session_cache,
    reset_session_logger,
    resolve_session_log_dir,
)

__all__ = [
    # core
    "IvyLogAdapter",
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
    "LspLogHandler",
    "ToolTraceContext",
    "_LspLogHandler",
    "get_tracer",
    "init_tracer",
    "trace_tool",
    # session
    "SessionEventLogger",
    "SessionJsonLogHandler",
    "_read_session_file",
    "_workspace_hash",
    "get_error_count",
    "get_session_id",
    "get_session_logger",
    "install_session_jsonl_handler",
    "reset_session_cache",
    "reset_session_logger",
    "resolve_session_log_dir",
]
