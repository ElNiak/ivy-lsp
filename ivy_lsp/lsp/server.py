"""Ivy Language Server implementation."""

import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

# Apply pygls patches at import time (side-effect import).
import ivy_lsp.lsp.pygls_patches  # noqa: F401
from ivy_lsp import __version__
from ivy_lsp.infra.observability import (
    LogCategory,
    LogEvent,
    StructuredLogAdapter,
    call_context,
    get_call_id,
    get_session_logger,
)
from ivy_lsp.lsp.bulk_orchestrator import BulkOrchestrationMixin
from ivy_lsp.lsp.server_setup import ServerSetupMixin
from ivy_lsp.lsp.ui.status import ServerStateTracker

if TYPE_CHECKING:
    from ivy_lsp.core.compilation.compiler_manager import CompilerManager
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.semantic.analysis_pipeline import AnalysisPipeline
    from ivy_lsp.core.semantic.model import SemanticModel
    from ivy_lsp.lsp.diagnostics.publisher import DiagnosticCache

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


class IvyLanguageServer(BulkOrchestrationMixin, ServerSetupMixin, LanguageServer):
    """Language server for Ivy formal specification files."""

    def __init__(self) -> None:
        """Initialize the Ivy language server and register features."""
        super().__init__(
            name="ivy-language-server",
            version=__version__,
        )
        self._install_audit_logging()
        self._indexer: "Optional[WorkspaceIndexer]" = None
        self._parser: "Optional[Any]" = None
        self._full_mode: bool = False
        self._semantic_model: "Optional[SemanticModel]" = None
        self._analysis_pipeline: "Optional[AnalysisPipeline]" = None
        self._bulk_analysis_cancel: threading.Event = threading.Event()
        self._shutdown_event: threading.Event = threading.Event()
        self.state_tracker: ServerStateTracker = ServerStateTracker()
        self._compiler_manager: "Optional[CompilerManager]" = None
        self._last_active_uri: Optional[str] = None
        self._code_lens_enabled: bool = True
        self._rfc_coverage_enabled: bool = True
        self._client_supports_work_done_progress: bool = False
        self._initializing: bool = True
        self._ready_event: threading.Event = threading.Event()
        from ivy_lsp.lsp.diagnostics.publisher import DiagnosticCache

        self._diagnostic_cache: DiagnosticCache = DiagnosticCache()
        self._mcp_sidecar_thread: Optional[Any] = None
        self._mcp_sidecar_port: int = 0
        self.__init_features()

    def _install_audit_logging(self) -> None:
        """Wrap pygls dispatch to log every incoming LSP request/notification.

        Also wraps ``text_document_publish_diagnostics`` to track diagnostic
        pushes that Claude Code cannot consume (it only supports
        request/response LSP operations, not push notifications).
        """
        self._request_counts: dict = {}
        self._diagnostics_published_count: int = 0
        self._diagnostic_pull_count: int = 0

        protocol = self.protocol
        _original_handle_request = protocol._handle_request
        _original_handle_notification = protocol._handle_notification

        server_ref = self

        def _audited_handle_request(msg_id, method_name, params):
            base_call_id = (
                f"req-{method_name.replace('/', '_')}-{uuid.uuid4().hex[:10]}"
            )
            with call_context(base_call_id):
                server_ref._request_counts[method_name] = (
                    server_ref._request_counts.get(method_name, 0) + 1
                )
                t0 = time.time()
                try:
                    result = _original_handle_request(msg_id, method_name, params)
                    duration_ms = round((time.time() - t0) * 1000, 1)
                    get_session_logger().log_event(
                        channel="lsp",
                        event_type="request",
                        name=method_name,
                        status="ok",
                        duration_ms=duration_ms,
                        call_id=get_call_id(),
                    )
                    slog.info(
                        "LSP request",
                        extra={
                            "event": LogEvent(
                                LogCategory.ACTIVITY,
                                "audit",
                                {
                                    "call_id": get_call_id(),
                                    "method": method_name,
                                    "duration_ms": duration_ms,
                                },
                            )
                        },
                    )
                    if method_name == "textDocument/diagnostic":
                        server_ref._diagnostic_pull_count += 1
                    return result
                except Exception:
                    duration_ms = round((time.time() - t0) * 1000, 1)
                    get_session_logger().log_event(
                        channel="lsp",
                        event_type="request",
                        name=method_name,
                        status="error",
                        duration_ms=duration_ms,
                        call_id=get_call_id(),
                    )
                    slog.warning(
                        "LSP request failed",
                        extra={
                            "event": LogEvent(
                                LogCategory.ACTIVITY,
                                "audit",
                                {
                                    "call_id": get_call_id(),
                                    "method": method_name,
                                    "duration_ms": duration_ms,
                                    "error": True,
                                },
                            )
                        },
                    )
                    raise

        def _audited_handle_notification(method_name, params):
            call_id = f"notif-{method_name.replace('/', '_')}-{uuid.uuid4().hex[:10]}"
            with call_context(call_id):
                server_ref._request_counts[method_name] = (
                    server_ref._request_counts.get(method_name, 0) + 1
                )
                slog.debug(
                    "LSP notification",
                    extra={
                        "event": LogEvent(
                            LogCategory.ACTIVITY,
                            "audit",
                            {
                                "call_id": get_call_id(),
                                "method": method_name,
                            },
                        )
                    },
                )
                get_session_logger().log_event(
                    channel="lsp",
                    event_type="notification",
                    name=method_name,
                    status="ok",
                    call_id=get_call_id(),
                )
                return _original_handle_notification(method_name, params)

        protocol._handle_request = _audited_handle_request  # type: ignore[assignment]
        protocol._handle_notification = _audited_handle_notification  # type: ignore[assignment]

        # Wrap diagnostic publishing to track drops
        _original_publish = self.text_document_publish_diagnostics

        def _tracked_publish(params):
            server_ref._diagnostics_published_count += 1
            diag_count = len(params.diagnostics) if params.diagnostics else 0
            logger.debug(
                "publishDiagnostics -> uri=%s, count=%d", params.uri, diag_count
            )
            try:
                if diag_count > 0:
                    error_count = sum(
                        1
                        for d in params.diagnostics
                        if d.severity == lsp.DiagnosticSeverity.Error
                    )
                    slog.info(
                        "Diagnostics published",
                        extra={
                            "event": LogEvent(
                                LogCategory.DIAGNOSTIC,
                                "publish",
                                {
                                    "call_id": get_call_id(),
                                    "uri": params.uri,
                                    "count": diag_count,
                                    "errors": error_count,
                                },
                            )
                        },
                    )
            except Exception:
                logger.debug("Audit logging failed in _tracked_publish", exc_info=True)
            try:
                result = _original_publish(params)
                logger.debug("publishDiagnostics delivered successfully")
                return result
            except Exception:
                logger.warning(
                    "publishDiagnostics FAILED for %s", params.uri, exc_info=True
                )
                raise

        self.text_document_publish_diagnostics = _tracked_publish  # type: ignore[assignment]

    # -- Public property accessors ---

    @property
    def indexer(self) -> "Optional[WorkspaceIndexer]":
        """Public accessor for the workspace indexer."""
        return self._indexer

    @property
    def parser(self) -> "Optional[Any]":
        """Public accessor for the Ivy parser."""
        return self._parser

    @property
    def full_mode(self) -> bool:
        """Whether the server is running in full (Z3) mode."""
        return self._full_mode

    @property
    def semantic_model(self) -> "Optional[SemanticModel]":
        """Public accessor for the semantic model."""
        return self._semantic_model

    @property
    def analysis_pipeline(self) -> "Optional[AnalysisPipeline]":
        """Public accessor for the analysis pipeline."""
        return self._analysis_pipeline

    @property
    def compiler_manager(self) -> "Optional[CompilerManager]":
        """Public accessor for the compiler manager."""
        return self._compiler_manager

    @property
    def diagnostic_cache(self) -> "DiagnosticCache":
        """Public accessor for the pull-diagnostics cache."""
        return self._diagnostic_cache

    @property
    def initializing(self) -> bool:
        """Whether the server is currently initializing."""
        return self._initializing

    @property
    def bulk_analysis_cancel(self) -> threading.Event:
        """Public accessor for the bulk analysis cancel event."""
        return self._bulk_analysis_cancel

    def start_mcp_sidecar(self, port: int = 0) -> None:
        """Start the MCP HTTP sidecar in a daemon thread.

        Should be called before ``start_io()`` so the sidecar is ready
        when the LSP client connects.

        Args:
            port: Desired port (0 = default 19847). Auto-increments on conflict.
        """
        try:
            from ivy_lsp.mcp.sidecar import start_mcp_http_thread

            thread, actual_port = start_mcp_http_thread(self, port=port)
            self._mcp_sidecar_thread = thread
            self._mcp_sidecar_port = actual_port
            logger.info("MCP sidecar started on port %d", actual_port)
        except ImportError as exc:
            logger.info(
                "MCP sidecar not available (missing dependency: %s) — "
                "MCP tools will not be served via HTTP",
                exc,
            )
        except Exception:
            logger.warning("MCP sidecar failed to start", exc_info=True)

    def _stop_mcp_sidecar(self) -> None:
        """Clean up MCP sidecar resources on shutdown."""
        if self._mcp_sidecar_thread is None:
            return
        try:
            from ivy_lsp.mcp.sidecar import _remove_port_file

            # Get workspace root for port file cleanup
            ws_root = ""
            if self._indexer is not None:
                ws_root = self._indexer._workspace_root
            if not ws_root:
                ws_root = os.environ.get("IVY_WORKSPACE_ROOT", os.getcwd())
            _remove_port_file(ws_root)
        except Exception:
            logger.debug("MCP sidecar cleanup failed", exc_info=True)
        # Daemon thread will die when the process exits
        self._mcp_sidecar_thread = None
        self._mcp_sidecar_port = 0

    def __init_features(self):
        # -- Feature registration (below) ---
        from ivy_lsp.lsp import (
            code_action,
            commands,
            completion,
            document_highlight,
            document_symbols,
            rename,
            signature_help,
            visualization,
            workspace_symbols,
        )
        from ivy_lsp.lsp.diagnostics import publisher as diagnostics
        from ivy_lsp.lsp.navigation import (
            call_hierarchy,
            definition,
            hover,
            implementation,
            references,
        )
        from ivy_lsp.lsp.ui import code_lens, folding_range, monitoring, selection_range

        document_symbols.register(self)
        workspace_symbols.register(self)
        definition.register(self)
        document_highlight.register(self)
        references.register(self)
        rename.register(self)
        selection_range.register(self)
        signature_help.register(self)
        hover.register(self)
        completion.register(self)
        diagnostics.register(self)
        code_action.register(self)
        code_lens.register(self)
        commands.register(self)
        folding_range.register(self)
        monitoring.register(self)
        visualization.register(self)
        implementation.register(self)
        call_hierarchy.register(self)

        @self.feature(lsp.INITIALIZE)
        def on_initialize(params: lsp.InitializeParams) -> None:
            logger.info("initialize request received (process startup complete)")
            opts = params.initialization_options or {}
            if isinstance(opts, dict):
                code_lens_opts = opts.get("codeLens", {})
                if isinstance(code_lens_opts, dict):
                    self._code_lens_enabled = code_lens_opts.get("enabled", True)
                    self._rfc_coverage_enabled = code_lens_opts.get("rfcCoverage", True)

            # Check if the client supports work-done progress tokens.
            # If not, the server won't attempt to create progress tokens
            # (avoids JsonRpcMethodNotFound errors from clients like
            # Claude Code / MCP that don't handle
            # window/workDoneProgress/create).
            try:
                general = getattr(params.capabilities, "general", None)
                if general is not None:
                    # LSP 3.16+ client capabilities
                    wdp = getattr(general, "work_done_progress", None)
                    # Explicit check: if client has no work_done_progress
                    # capability, we need to also check window capability
                    if wdp is True:
                        self._client_supports_work_done_progress = True
                if not self._client_supports_work_done_progress:
                    window = getattr(params.capabilities, "window", None)
                    if window is not None:
                        wdp = getattr(window, "work_done_progress", None)
                        if wdp is True:
                            self._client_supports_work_done_progress = True
            except Exception:
                logger.debug(
                    "Failed to check client work-done progress capability",
                    exc_info=True,
                )

            logger.info(
                "initializationOptions: codeLens.enabled=%s, "
                "codeLens.rfcCoverage=%s, "
                "client.workDoneProgress=%s",
                self._code_lens_enabled,
                self._rfc_coverage_enabled,
                self._client_supports_work_done_progress,
            )

        @self.feature(lsp.INITIALIZED)
        @self.thread()
        def on_initialized(params: lsp.InitializedParams) -> None:
            init_start = time.time()
            try:
                slog.info(
                    "Server initialized",
                    extra={"event": LogEvent(LogCategory.MILESTONE, "startup")},
                )
                # Install the LSP log bridge early so that indexing
                # milestones (file count, symbol count, deep-index stats)
                # are forwarded as window/logMessage to the client.
                self._install_lsp_log_handler()
                self._setup_indexer()
                mode = "full" if self._full_mode else "light"
                self.window_log_message(
                    lsp.LogMessageParams(
                        type=lsp.MessageType.Info,
                        message=f"Ivy LSP running in {mode} mode",
                    )
                )
                slog.info(
                    "Running in %s mode",
                    mode,
                    extra={
                        "event": LogEvent(
                            LogCategory.MILESTONE,
                            "startup",
                            {"mode": mode},
                        )
                    },
                )
            except Exception:
                logger.exception("on_initialized failed")
                self.window_show_message(
                    lsp.ShowMessageParams(
                        type=lsp.MessageType.Error,
                        message="Ivy LSP: Initialization failed. "
                        "Code intelligence features may not be available. "
                        "Check the Ivy Language Server output for details.",
                    )
                )
            finally:
                self._initializing = False
                self._ready_event.set()
                self._send_server_ready_notification(init_start)

        @self.feature(lsp.SHUTDOWN)
        def on_shutdown(params) -> None:
            slog.info(
                "Session audit summary",
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "summary",
                        {
                            "request_counts": dict(self._request_counts),
                            "diagnostics_published": self._diagnostics_published_count,
                            "diagnostic_pull_requests": self._diagnostic_pull_count,
                            "diagnostic_gap": (
                                self._diagnostics_published_count
                                - self._diagnostic_pull_count
                            ),
                        },
                    )
                },
            )
            self._shutdown_event.set()
            self._bulk_analysis_cancel.set()
            if self._indexer is not None:
                self._indexer.request_stop()
            if self._compiler_manager is not None:
                self._compiler_manager.shutdown()
            self._stop_mcp_sidecar()
            self._cleanup_staging()
