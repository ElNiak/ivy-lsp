"""Ivy Language Server implementation."""

import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import InvalidStateError

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from ivy_lsp import __version__
from ivy_lsp.features.status import ServerStateTracker
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.utils import uri_to_path

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


def _patch_pygls_cancelled_future() -> None:
    """Work around pygls 2.0.1 bug: responses to cancelled futures crash.

    pygls._handle_response() calls future.set_result() without checking
    future.cancelled(), so a late response to a timed-out or cancelled
    request raises InvalidStateError.  This wraps the method to suppress
    that harmless race.
    """
    from pygls.protocol.json_rpc import JsonRPCProtocol

    _original = JsonRPCProtocol._handle_response

    def _safe_handle_response(self, msg_id, result=None, error=None):
        try:
            _original(self, msg_id, result, error)
        except InvalidStateError:
            logger.debug(
                "Ignoring response to cancelled/completed request %s", msg_id
            )

    JsonRPCProtocol._handle_response = _safe_handle_response  # type: ignore[assignment]


def _patch_pygls_closed_pipe() -> None:
    """Convert ValueError from closed stdout to BrokenPipeError.

    pygls 2.0.1 catches BrokenPipeError in _send_data() but not
    ValueError("write to closed file"), which Python raises when
    writing to a closed BufferedWriter.  The generic except handler
    then cascades via logger.exception + _report_server_error.
    Converting to BrokenPipeError lets pygls's existing handler
    trigger a clean shutdown.
    """
    from pygls.protocol.json_rpc import JsonRPCProtocol

    _original = JsonRPCProtocol._send_data

    def _safe_send_data(self, data):
        try:
            _original(self, data)
        except ValueError as exc:
            if "closed" in str(exc).lower():
                raise BrokenPipeError(str(exc)) from exc
            raise

    JsonRPCProtocol._send_data = _safe_send_data  # type: ignore[assignment]


try:
    import pygls as _pygls_mod

    if getattr(_pygls_mod, "__version__", "").startswith("2.0."):
        _patch_pygls_cancelled_future()
        _patch_pygls_closed_pipe()
except ImportError:
    pass


class _LspLogHandler(logging.Handler):
    """Bridge Python logging -> LSP window/logMessage notifications.

    Rate-limited to prevent flooding the stdio pipe, which can cause
    write-side blocking and contribute to thread pool starvation.
    """

    _LEVEL_MAP = {
        logging.DEBUG: lsp.MessageType.Log,
        logging.INFO: lsp.MessageType.Info,
        logging.WARNING: lsp.MessageType.Warning,
        logging.ERROR: lsp.MessageType.Error,
        logging.CRITICAL: lsp.MessageType.Error,
    }

    _CAT_PRIORITY = {"MIL": 1, "DIA": 2, "PER": 3, "ACT": 4}
    _CAT_MIN_INTERVAL = {"MIL": 0.01, "DIA": 0.01, "PER": 0.1, "ACT": 0.1}
    _DEFAULT_MIN_INTERVAL = 0.05
    _MAX_MESSAGE_LEN = 8192  # 8 KB cap per log message

    _tls = threading.local()  # per-thread recursion guard

    def __init__(self, server: "IvyLanguageServer"):
        super().__init__()
        self._server = server
        self._lock = threading.Lock()  # non-reentrant; no I/O under lock
        self._last_emit = 0.0
        self._drop_counts: dict = {}
        self._pipe_dead = False

    @staticmethod
    def _extract_category(msg: str) -> str:
        if msg.startswith("[MIL"):
            return "MIL"
        if msg.startswith("[ACT"):
            return "ACT"
        if msg.startswith("[DIA"):
            return "DIA"
        if msg.startswith("[PER"):
            return "PER"
        return ""

    def emit(self, record: logging.LogRecord) -> None:
        if self._pipe_dead:
            return
        # Per-thread recursion guard: pygls logs inside _send_data(),
        # which would re-enter this handler. Skip to prevent infinite loop.
        if getattr(self._tls, "sending", False):
            return

        # --- fast path: all state under lock (no I/O) ---
        with self._lock:
            now = time.time()
            if record.levelno < logging.WARNING:
                msg = self.format(record)
                cat = self._extract_category(msg)
                min_interval = self._CAT_MIN_INTERVAL.get(
                    cat, self._DEFAULT_MIN_INTERVAL
                )
                if getattr(self._server, "_initializing", False):
                    min_interval = max(min_interval, 1.0)
                if (now - self._last_emit) < min_interval:
                    cat_key = cat or "_untagged"
                    self._drop_counts[cat_key] = (
                        self._drop_counts.get(cat_key, 0) + 1
                    )
                    return
            else:
                msg = self.format(record)

            if len(msg) > self._MAX_MESSAGE_LEN:
                msg = msg[: self._MAX_MESSAGE_LEN] + "... [truncated]"

            msg_type = self._LEVEL_MAP.get(record.levelno, lsp.MessageType.Log)
            if self._drop_counts:
                parts = []
                for k, v in sorted(self._drop_counts.items()):
                    label = k if k != "_untagged" else "other"
                    parts.append(f"{v} {label}")
                suppression = "[" + ", ".join(parts) + " messages suppressed]"
                msg = f"{msg} {suppression}"
                self._drop_counts = {}
            self._last_emit = now

        # --- slow path: send notification WITHOUT holding lock ---
        self._tls.sending = True
        try:
            self._server.window_log_message(
                lsp.LogMessageParams(type=msg_type, message=msg)
            )
        except Exception:
            self._pipe_dead = True
            try:
                sys.stderr.write(f"[ivy-lsp-fallback] {msg}\n")
                sys.stderr.flush()
            except Exception:
                pass
        finally:
            self._tls.sending = False


class IvyLanguageServer(LanguageServer):
    """Language server for Ivy formal specification files."""

    def __init__(self):
        super().__init__(
            name="ivy-language-server",
            version=__version__,
        )
        self._indexer = None
        self._parser = None
        self._full_mode = False
        self._semantic_model = None
        self._analysis_pipeline = None
        self._bulk_analysis_cancel = threading.Event()
        self._shutdown_event = threading.Event()
        self.state_tracker = ServerStateTracker()
        self._compiler_manager = None
        self._code_lens_enabled = True
        self._rfc_coverage_enabled = True
        self._initializing = True

        from ivy_lsp.features import (
            code_action,
            code_lens,
            commands,
            completion,
            definition,
            diagnostics,
            document_highlight,
            document_symbols,
            folding_range,
            hover,
            monitoring,
            references,
            rename,
            selection_range,
            signature_help,
            visualization,
            workspace_symbols,
        )

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

        @self.feature(lsp.INITIALIZE)
        def on_initialize(params: lsp.InitializeParams) -> None:
            opts = params.initialization_options or {}
            if isinstance(opts, dict):
                code_lens_opts = opts.get("codeLens", {})
                if isinstance(code_lens_opts, dict):
                    self._code_lens_enabled = code_lens_opts.get(
                        "enabled", True
                    )
                    self._rfc_coverage_enabled = code_lens_opts.get(
                        "rfcCoverage", True
                    )
            logger.info(
                "initializationOptions: codeLens.enabled=%s, "
                "codeLens.rfcCoverage=%s",
                self._code_lens_enabled,
                self._rfc_coverage_enabled,
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
                    extra={"event": LogEvent(
                        LogCategory.MILESTONE, "startup", {"mode": mode},
                    )},
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
                self._send_server_ready_notification(init_start)

        @self.feature(lsp.SHUTDOWN)
        def on_shutdown(params) -> None:
            self._shutdown_event.set()
            self._bulk_analysis_cancel.set()
            if self._indexer is not None:
                self._indexer.request_stop()
            if self._compiler_manager is not None:
                self._compiler_manager.shutdown()
            self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        """Clean up the staging directory on shutdown."""
        if self._indexer and hasattr(self._indexer, "_resolver"):
            try:
                self._indexer._resolver.cleanup_staging()
                slog.info(
                    "Staging directory cleaned up",
                    extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "staging")},
                )
            except Exception:
                logger.exception("Failed to clean up staging directory")

    def _send_model_ready_notification(self) -> None:
        """Send ``ivy/modelReady`` notification so the client can refresh immediately."""
        if self._shutdown_event.is_set():
            return
        try:
            graph = getattr(self._indexer, "_requirement_graph", None)
            action_count = len(graph.actions) if graph else 0
            req_count = len(graph.requirements) if graph else 0
            self.protocol.notify(
                "ivy/modelReady",
                {"actionCount": action_count, "requirementCount": req_count},
            )
            slog.info(
                "Sent ivy/modelReady: %d actions, %d requirements",
                action_count,
                req_count,
                extra={"event": LogEvent(
                    LogCategory.MILESTONE, "model_ready",
                    {"actions": action_count, "requirements": req_count},
                )},
            )
        except Exception:
            logger.warning(
                "Failed to send ivy/modelReady notification", exc_info=True
            )

    def _send_server_ready_notification(self, init_start: float) -> None:
        """Send ``ivy/serverReady`` notification after initialization completes."""
        if self._shutdown_event.is_set():
            return
        try:
            mode = "full" if self._full_mode else "light"
            init_duration = round(time.time() - init_start, 3)
            self.protocol.notify(
                "ivy/serverReady",
                {"mode": mode, "indexingDuration": init_duration},
            )
            slog.info(
                "Sent ivy/serverReady: mode=%s, duration=%.3fs",
                mode,
                init_duration,
                extra={"event": LogEvent(
                    LogCategory.MILESTONE, "server_ready",
                    {"mode": mode, "duration_s": init_duration},
                )},
            )
        except Exception:
            logger.warning(
                "Failed to send ivy/serverReady notification", exc_info=True
            )

    def _make_progress_callback(
        self,
        title: str,
        begin_msg: str,
        end_msg: str,
        throttle_seconds: float = 0.0,
    ):
        """Create a ``$/progress`` work-done notification callback.

        The callback is invoked from background threads.  pygls message
        sending is thread-safe (queued internally), so
        ``work_done_progress.begin/report/end`` can be called directly.

        Args:
            title: Progress bar title (e.g. "Ivy Deep Index").
            begin_msg: Begin message with ``{total}`` placeholder.
            end_msg: End message with ``{total}`` placeholder.
            throttle_seconds: Minimum interval between intermediate
                reports (0 = no throttle).

        Returns:
            A callable ``(completed, total, current_file) -> None``.
        """
        token = str(uuid.uuid4())
        state = {"begun": False, "disabled": False, "last_report": 0.0}
        state_lock = threading.Lock()
        server = self

        # Eagerly create the progress token (outside any callback)
        try:
            server.work_done_progress.create(token)
        except Exception:
            state["disabled"] = True
            logger.debug(
                "Client does not support work-done progress", exc_info=True
            )

        def _callback(completed: int, total: int, current_file):
            if server._shutdown_event.is_set():
                return
            with state_lock:
                if state["disabled"]:
                    return
                if not state["begun"]:
                    try:
                        server.work_done_progress.begin(
                            token,
                            lsp.WorkDoneProgressBegin(
                                title=title,
                                message=begin_msg.format(total=total),
                                cancellable=False,
                                percentage=0,
                            ),
                        )
                        state["begun"] = True
                    except Exception:
                        state["disabled"] = True
                        return

                if completed >= total:
                    try:
                        server.work_done_progress.end(
                            token,
                            lsp.WorkDoneProgressEnd(
                                message=end_msg.format(total=total),
                            ),
                        )
                    except Exception:
                        logger.debug("Failed to end progress", exc_info=True)
                    return

                if throttle_seconds > 0:
                    now = time.time()
                    if (now - state["last_report"]) < throttle_seconds:
                        return
                    state["last_report"] = now

            pct = int(100 * completed / total) if total > 0 else 0
            basename = (
                os.path.basename(current_file) if current_file else ""
            )
            try:
                server.work_done_progress.report(
                    token,
                    lsp.WorkDoneProgressReport(
                        message=f"({completed}/{total}) {basename}",
                        percentage=pct,
                    ),
                )
            except Exception:
                logger.debug("Failed to report progress", exc_info=True)

        return _callback

    def _start_bulk_analysis(self) -> None:
        """Kick off background T1+T2 analysis of all workspace files.

        Called as the ``done_callback`` from :class:`WorkspaceIndexer`
        after Phase 2 (deep indexing) completes.  This runs on a pygls
        thread-pool thread, so we schedule on the event loop via
        ``run_coroutine_threadsafe`` when possible, falling back to a
        plain daemon thread if no loop is available.
        """
        if os.environ.get("IVY_LSP_BULK_ANALYSIS", "1") == "0":
            slog.info(
                "Bulk analysis disabled via IVY_LSP_BULK_ANALYSIS=0",
                extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "bulk_t1t2")},
            )
            return
        if self._analysis_pipeline is None or self._indexer is None:
            return

        include_t2 = os.environ.get("IVY_LSP_BULK_ANALYSIS_T2", "1") != "0"
        all_files = self._indexer.get_all_ivy_file_paths()
        if not all_files:
            return

        progress_cb = self._make_progress_callback(
            "Ivy Background Analysis",
            "Analysing {total} files...",
            "Analysed {total} files",
        )

        def _run():
            try:
                result = self._analysis_pipeline.run_bulk_t1_t2(
                    filepaths=all_files,
                    progress_callback=progress_cb,
                    cancel_event=self._bulk_analysis_cancel,
                    include_t2=include_t2,
                )
                slog.info(
                    "Bulk analysis complete: %d T1, %d T2, %d errors, cancelled=%s",
                    result.t1_completed,
                    result.t2_completed,
                    len(result.errors),
                    result.cancelled,
                    extra={"event": LogEvent(
                        LogCategory.MILESTONE, "bulk_t1t2", {
                            "t1": result.t1_completed,
                            "t2": result.t2_completed,
                            "errors": len(result.errors),
                            "cancelled": result.cancelled,
                        },
                    )},
                )
                self._send_model_ready_notification()
                self._start_bulk_compilation_via_pipeline()
            except Exception:
                logger.exception("Bulk analysis failed")

        # Schedule on event loop if available; fall back to daemon thread
        try:
            loop = self.loop  # pygls LanguageServer exposes the event loop
            if loop is not None and loop.is_running():
                loop.run_in_executor(None, _run)
                return
        except AttributeError:
            pass

        thread = threading.Thread(
            target=_run, daemon=True, name="ivy-bulk-analysis",
        )
        thread.start()

    def _send_compilation_progress(
        self, completed: int, total: int, filepath: str, success: bool
    ) -> None:
        """Send ``ivy/compilationProgress`` push notification to the client."""
        if self._shutdown_event.is_set():
            return
        try:
            self.protocol.notify(
                "ivy/compilationProgress",
                {
                    "completed": completed,
                    "total": total,
                    "currentFile": os.path.basename(filepath),
                    "success": success,
                },
            )
        except Exception:
            logger.warning(
                "Failed to send compilationProgress notification",
                exc_info=True,
            )
        if completed >= total:
            self._send_model_ready_notification()

    def _start_bulk_compilation_via_pipeline(self) -> None:
        """Delegate bulk T3 compilation to the analysis pipeline.

        Called after bulk T1+T2 analysis completes.
        """
        if self._analysis_pipeline is None or self._indexer is None:
            return
        if self._compiler_manager is None:
            return
        if os.environ.get("IVY_LSP_BULK_COMPILE", "1") == "0":
            slog.info(
                "Bulk compilation disabled via IVY_LSP_BULK_COMPILE=0",
                extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "compile_bulk")},
            )
            return

        try:
            graph = self._indexer._requirement_graph
        except AttributeError:
            return

        from ivy_lsp.analysis.test_scope import ScopedRequirementModel

        if not isinstance(graph, ScopedRequirementModel):
            return

        test_files = list(graph._test_scopes.keys())
        if not test_files:
            logger.info("No test files found for bulk compilation")
            return

        slog.info(
            "Starting bulk compilation for %d test files",
            len(test_files),
            extra={"event": LogEvent(
                LogCategory.MILESTONE, "compile_bulk",
                {"test_files": len(test_files)},
            )},
        )

        progress_cb = self._make_progress_callback(
            "Ivy Compilation",
            "Compiling {total} test files...",
            "Compiled {total} test files",
            throttle_seconds=1.0,
        )

        self._analysis_pipeline.run_bulk_tier3(
            test_files,
            progress_callback=progress_cb,
            cancel_event=self._bulk_analysis_cancel,
        )

    def _install_lsp_log_handler(self) -> None:
        """Add LSP notification handler and demote stderr to WARNING-only."""
        root = logging.getLogger()
        handler = _LspLogHandler(self)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        # Raise stderr handler level to WARNING so critical errors remain
        # visible in raw output, but normal logs go through LSP only.
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
                h.setLevel(logging.WARNING)

    def _setup_indexer(self):
        """Create and populate the workspace indexer."""
        activity_level = os.environ.get("IVY_LSP_ACTIVITY_LEVEL", "phase")
        if activity_level == "file":
            logging.getLogger("ivy_lsp").setLevel(logging.DEBUG)
        elif activity_level == "phase":
            logging.getLogger("ivy_lsp").setLevel(logging.INFO)

        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer

        ws_folders = self.workspace.folders
        if ws_folders:
            root = uri_to_path(list(ws_folders.values())[0].uri)
        else:
            root = os.getcwd()

        # Read include/exclude paths from environment
        raw_includes = os.environ.get("IVY_LSP_INCLUDE_PATHS", "")
        include_paths = [p.strip() for p in raw_includes.split(",") if p.strip()]
        raw_excludes = os.environ.get("IVY_LSP_EXCLUDE_PATHS", "")
        exclude_paths = [p.strip() for p in raw_excludes.split(",") if p.strip()]
        if include_paths:
            slog.info(
                "Include paths: %s",
                include_paths,
                extra={"event": LogEvent(
                    LogCategory.DIAGNOSTIC, "startup",
                    {"include_paths": include_paths},
                )},
            )
        if exclude_paths:
            slog.info(
                "Exclude paths: %s",
                exclude_paths,
                extra={"event": LogEvent(
                    LogCategory.DIAGNOSTIC, "startup",
                    {"exclude_paths": exclude_paths},
                )},
            )

        try:
            resolver = IncludeResolver(
                root,
                exclude_paths=exclude_paths,
                include_paths=include_paths,
            )
        except Exception:
            logger.exception("IncludeResolver construction failed")
            self.window_show_message(
                lsp.ShowMessageParams(
                    type=lsp.MessageType.Warning,
                    message="Ivy include resolver failed to initialize. "
                    "Features depending on cross-file resolution will not work.",
                )
            )
            return

        # Create flat staging directory (mirrors ivyc's include/1.7/ model)
        try:
            staging_dir = resolver.create_staging_directory()
            slog.info(
                "Created staging directory: %s",
                staging_dir,
                extra={"event": LogEvent(
                    LogCategory.DIAGNOSTIC, "staging",
                    {"staging_dir": staging_dir},
                )},
            )
        except Exception:
            logger.exception("Failed to create staging, falling back to direct scan")
            self.window_show_message(
                lsp.ShowMessageParams(
                    type=lsp.MessageType.Warning,
                    message="Ivy staging directory creation failed. "
                    "Verify/compile may not resolve cross-directory includes.",
                )
            )

        # Try full parser (requires z3). Fall back to lexer-only mode.
        # Parser is created after resolver so it can use resolver.resolve
        # as a callback for cross-directory include resolution.
        try:
            from ivy_lsp.parsing.parser_session import IvyParserWrapper

            # Eagerly verify z3 is actually available — IvyParserWrapper
            # defers ivy imports to method bodies, so the import above
            # succeeds even without z3.
            import ivy.ivy_utils  # noqa: F401 — triggers z3_shim

            self._parser = IvyParserWrapper(resolve_callback=resolver.resolve)
            self._full_mode = True
            slog.info(
                "Full parser available (z3 found)",
                extra={"event": LogEvent(LogCategory.MILESTONE, "startup")},
            )
        except Exception as e:
            from ivy_lsp.parsing.fallback_parser import FallbackOnlyParser

            self._parser = FallbackOnlyParser()
            self._full_mode = False
            slog.info(
                "z3 not available (%s); running in light mode",
                e,
                extra={"event": LogEvent(
                    LogCategory.DIAGNOSTIC, "startup", {"reason": str(e)},
                )},
            )

        progress_cb = self._make_progress_callback(
            "Ivy Deep Index",
            "Parsing {total} test files...",
            "Indexed {total} test files",
        )
        try:
            self._indexer = WorkspaceIndexer(
                root, self._parser, resolver,
                progress_callback=progress_cb,
                done_callback=self._start_bulk_analysis,
            )
        except Exception:
            logger.exception("WorkspaceIndexer construction failed")
            self.window_show_message(
                lsp.ShowMessageParams(
                    type=lsp.MessageType.Warning,
                    message="Ivy workspace indexer failed to initialize. "
                    "Code intelligence features will not be available.",
                )
            )
            return
        self.state_tracker.set_indexing()
        try:
            index_start = time.time()
            self._indexer.index_workspace()
            index_duration = time.time() - index_start
            self.state_tracker.set_indexed(index_duration)
            n_files = len(self._indexer._cache._cache)
            n_symbols = sum(1 for _ in self._indexer._symbol_table.all_symbols())
            slog.info(
                "Indexed %d files, %d symbols",
                n_files,
                n_symbols,
                extra={"event": LogEvent(
                    LogCategory.MILESTONE, "indexing",
                    {"files": n_files, "symbols": n_symbols,
                     "duration_s": round(index_duration, 3)},
                )},
            )
            # Explicit notification bypasses the log-handler rate limiter
            # so clients always see this milestone in the Output channel.
            self.window_log_message(
                lsp.LogMessageParams(
                    type=lsp.MessageType.Info,
                    message=f"Indexed {n_files} files, {n_symbols} symbols",
                )
            )
            self._send_model_ready_notification()
        except Exception as exc:
            self.state_tracker.set_index_error(str(exc))
            logger.exception("Workspace indexing failed")
            self.window_show_message(
                lsp.ShowMessageParams(
                    type=lsp.MessageType.Warning,
                    message="Ivy workspace indexing failed. "
                    "Completion, go-to-definition, and other features may not work.",
                )
            )

        # Set up semantic model and analysis pipeline
        try:
            from ivy_lsp.adapters.null_adapter import (
                NullAstEnrichmentAdapter,
                NullCompilerAdapter,
            )
            from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline
            from ivy_lsp.semantic.model import SemanticModel

            self._semantic_model = SemanticModel()

            if self._full_mode:
                try:
                    from ivy_lsp.adapters.ast_enrichment_adapter import (
                        AstEnrichmentAdapter,
                    )
                    from ivy_lsp.adapters.compiler_adapter import CompilerAdapter

                    enrichment = AstEnrichmentAdapter()

                    # Create CompilerManager for subprocess-based compilation
                    compiler_staging_dir = None
                    try:
                        from ivy_lsp.compilation.compiler_manager import CompilerManager

                        # Re-read staging dir from the resolver (not the
                        # local var from create_staging_directory) because
                        # CompilerManager needs the persistent path.
                        if self._indexer and hasattr(self._indexer, "_resolver"):
                            compiler_staging_dir = getattr(
                                self._indexer._resolver, "_staging_dir", None
                            )
                        if compiler_staging_dir is None:
                            logger.warning(
                                "No staging directory available for CompilerManager. "
                                "Cross-directory includes will fail. "
                                "indexer=%s, has_resolver=%s",
                                self._indexer is not None,
                                hasattr(self._indexer, "_resolver")
                                if self._indexer
                                else False,
                            )
                        else:
                            logger.info(
                                "CompilerManager using staging dir: %s",
                                compiler_staging_dir,
                            )
                        max_concurrent = max(
                            1,
                            int(os.environ.get("IVY_LSP_COMPILE_WORKERS", "1")),
                        )
                        self._compiler_manager = CompilerManager(
                            staging_dir=compiler_staging_dir,
                            timeout=float(
                                os.environ.get("IVY_LSP_COMPILE_TIMEOUT", "300")
                            ),
                            cache_ttl=float(
                                os.environ.get("IVY_LSP_COMPILE_CACHE_TTL", "600")
                            ),
                            max_concurrent=max_concurrent,
                        )
                        compiler = CompilerAdapter(self._compiler_manager)
                    except Exception:
                        logger.warning(
                            "CompilerManager unavailable, using legacy adapter",
                            exc_info=True,
                        )
                        compiler = CompilerAdapter(
                            staging_dir=compiler_staging_dir,
                        )
                        self.window_show_message(
                            lsp.ShowMessageParams(
                                type=lsp.MessageType.Warning,
                                message=(
                                    "Ivy CompilerManager unavailable; "
                                    "using legacy compilation adapter. "
                                    "Compilation features may be degraded."
                                ),
                            )
                        )
                except ImportError:
                    enrichment = NullAstEnrichmentAdapter()
                    compiler = NullCompilerAdapter()
                    logger.warning(
                        "Full-mode adapters unavailable; falling back to null adapters. "
                        "Tier 2/3 analysis will be inactive."
                    )
                    self.window_show_message(
                        lsp.ShowMessageParams(
                            type=lsp.MessageType.Warning,
                            message="Ivy full-mode adapters unavailable (missing Z3 or ivy). "
                            "Type enrichment, compilation, and semantic diagnostics are disabled.",
                        )
                    )
            else:
                enrichment = NullAstEnrichmentAdapter()
                compiler = NullCompilerAdapter()

            def _resolve_test_file(filepath: str):
                """Resolve a module file to its enclosing test file for T3."""
                from ivy_lsp.features.commands import _find_enclosing_test

                return _find_enclosing_test(self, filepath)

            requirement_graph = getattr(
                self._indexer, "_requirement_graph", None
            )
            self._analysis_pipeline = AnalysisPipeline(
                self._semantic_model,
                self._parser,
                enrichment,
                compiler,
                compiler_manager=self._compiler_manager,
                test_file_resolver=_resolve_test_file,
                requirement_graph=requirement_graph,
                notification_callback=self._send_compilation_progress,
            )
            if self._indexer is not None:
                self._indexer.set_analysis_pipeline(self._analysis_pipeline)
            slog.info(
                "Semantic model and analysis pipeline initialized",
                extra={"event": LogEvent(LogCategory.MILESTONE, "semantic")},
            )
        except Exception:
            logger.exception("Semantic model setup failed")
            self.window_show_message(
                lsp.ShowMessageParams(
                    type=lsp.MessageType.Warning,
                    message="Semantic analysis initialization failed. "
                    "Hover enrichment, RFC code lenses, and semantic diagnostics unavailable.",
                )
            )
