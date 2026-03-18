"""Mixin providing server initialization and setup pipeline.

Methods on this mixin operate on ``self`` attributes from
:class:`~ivy_lsp.server.IvyLanguageServer` via Python's MRO.
"""

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.config import get_config
from ivy_lsp.lsp_log_handler import LspLogHandler
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.utils import uri_to_path

if TYPE_CHECKING:
    from ivy_lsp.indexer.include_resolver import IncludeResolver

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


class ServerSetupMixin:
    """Server initialization and setup pipeline for IvyLanguageServer."""

    def _install_lsp_log_handler(self) -> None:
        """Add LSP notification handler and demote stderr to WARNING-only."""
        root = logging.getLogger()
        handler = LspLogHandler(self)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        # Raise stderr handler level to WARNING so critical errors remain
        # visible in raw output, but normal logs go through LSP only.
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
                h.setLevel(logging.WARNING)

    def _setup_indexer(self):
        """Create and populate the workspace indexer.

        Thin orchestrator that delegates to focused helpers:
        1. _configure_activity_logging --- env-based log level
        2. _create_resolver --- workspace detection + include resolver + staging
        3. _create_parser --- z3 detection + parser creation
        4. _create_indexer --- indexer construction + workspace scan
        5. _setup_analysis_pipeline --- semantic model + adapters + pipeline
        """
        self._configure_activity_logging()

        ws_folders = self.workspace.folders
        if ws_folders:
            ws_root = uri_to_path(list(ws_folders.values())[0].uri)
        else:
            ws_root = os.getcwd()

        resolver, ws_root = self._create_resolver(ws_root)
        if resolver is None:
            return

        self._create_parser(resolver)

        if not self._create_indexer(ws_root, resolver):
            return

        self._setup_analysis_pipeline()

    def _configure_activity_logging(self) -> None:
        """Configure ivy_lsp log level from IVY_LSP_ACTIVITY_LEVEL env var."""
        activity_level = get_config().activity_level
        if activity_level == "file":
            logging.getLogger("ivy_lsp").setLevel(logging.DEBUG)
        elif activity_level == "phase":
            logging.getLogger("ivy_lsp").setLevel(logging.INFO)

    def _create_resolver(
        self,
        ws_root: str,
    ) -> Tuple[Optional["IncludeResolver"], str]:
        """Create include resolver with workspace detection and staging.

        Performs workspace auto-detection, reads include/exclude paths from
        environment, constructs the IncludeResolver, and creates a flat
        staging directory for cross-directory includes.

        Returns:
            (resolver, refined_ws_root) on success, (None, ws_root) on failure.
        """
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.workspace_detection import detect_ivy_workspace

        ws_config = detect_ivy_workspace(start_dir=ws_root)
        ws_root = ws_config.workspace_root
        slog.info(
            "Workspace detection: root=%s, detected_by=%s, type=%s",
            ws_root,
            ws_config.detected_by,
            ws_config.project_type,
            extra={
                "event": LogEvent(
                    LogCategory.DIAGNOSTIC,
                    "workspace_detection",
                    {
                        "root": ws_root,
                        "detected_by": ws_config.detected_by,
                        "project_type": ws_config.project_type,
                    },
                )
            },
        )

        # Read include/exclude paths from environment, merging with detected
        cfg = get_config()
        include_paths = cfg.include_paths or ws_config.include_paths
        exclude_paths = cfg.exclude_paths or ws_config.exclude_paths
        if include_paths:
            slog.info(
                "Include paths: %s",
                include_paths,
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "startup",
                        {"include_paths": include_paths},
                    )
                },
            )
        if exclude_paths:
            slog.info(
                "Exclude paths: %s",
                exclude_paths,
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "startup",
                        {"exclude_paths": exclude_paths},
                    )
                },
            )

        try:
            resolver = IncludeResolver(
                ws_root,
                exclude_paths=exclude_paths,
                include_paths=include_paths,
                workspace_layers=ws_config.workspace_layers,
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
            return None, ws_root

        # Create flat staging directory (mirrors ivyc's include/1.7/ model)
        try:
            staging_dir = resolver.create_staging_directory()
            slog.info(
                "Created staging directory: %s",
                staging_dir,
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "staging",
                        {"staging_dir": staging_dir},
                    )
                },
            )
            # Activate per-layer staging if workspace layers are configured
            if ws_config.workspace_layers:
                resolver.build_layered_staging()
                slog.info(
                    "Built layered staging for %d layers",
                    len(ws_config.workspace_layers),
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

        return resolver, ws_root

    def _create_parser(self, resolver: "IncludeResolver") -> None:
        """Create the Ivy parser, falling back to lexer-only mode without z3.

        Sets self._parser and self._full_mode.
        """
        try:
            # Eagerly verify z3 is actually available --- IvyParserWrapper
            # defers ivy imports to method bodies, so the import above
            # succeeds even without z3.
            import ivy.ivy_utils  # noqa: F401 --- triggers z3_shim

            from ivy_lsp.parsing.parser_session import IvyParserWrapper

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
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "startup",
                        {"reason": str(e)},
                    )
                },
            )

    def _create_indexer(self, ws_root: str, resolver: "IncludeResolver") -> bool:
        """Create the workspace indexer and run initial indexing.

        Returns True on success, False if the indexer could not be created.
        """
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer

        progress_cb = self._make_progress_callback(
            "Ivy Deep Index",
            "Parsing {total} test files...",
            "Indexed {total} test files",
        )
        try:
            self._indexer = WorkspaceIndexer(
                ws_root,
                self._parser,
                resolver,
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
            return False
        self.state_tracker.set_indexing()
        try:
            index_start = time.time()
            self._indexer.index_workspace()
            index_duration = time.time() - index_start
            self.state_tracker.set_indexed(index_duration)
            n_files = len(self._indexer._cache._cache) if self._indexer._cache else 0
            n_symbols = sum(1 for _ in self._indexer.lookup_all_symbols())
            slog.info(
                "Indexed %d files, %d symbols",
                n_files,
                n_symbols,
                extra={
                    "event": LogEvent(
                        LogCategory.MILESTONE,
                        "indexing",
                        {
                            "files": n_files,
                            "symbols": n_symbols,
                            "duration_s": round(index_duration, 3),
                        },
                    )
                },
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
        return True

    def _setup_analysis_pipeline(self) -> None:
        """Set up semantic model, adapters, compiler manager, and analysis pipeline."""
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
                        if self._indexer:
                            compiler_staging_dir = getattr(
                                self._indexer.resolver, "_staging_dir", None
                            )
                        if compiler_staging_dir is None:
                            logger.warning(
                                "No staging directory available for CompilerManager. "
                                "Cross-directory includes will fail. "
                                "indexer=%s, has_resolver=%s",
                                self._indexer is not None,
                                (
                                    self._indexer.resolver is not None
                                    if self._indexer
                                    else False if self._indexer else False
                                ),
                            )
                        else:
                            logger.info(
                                "CompilerManager using staging dir: %s",
                                compiler_staging_dir,
                            )
                        _cfg = get_config()
                        self._compiler_manager = CompilerManager(
                            staging_dir=compiler_staging_dir,
                            timeout=_cfg.compile_timeout,
                            cache_ttl=_cfg.compile_cache_ttl,
                            max_concurrent=_cfg.compile_workers,
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

            requirement_graph = getattr(self._indexer, "requirement_graph", None)
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
