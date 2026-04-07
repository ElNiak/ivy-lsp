"""Mixin providing server initialization and setup pipeline.

Methods on this mixin operate on ``self`` attributes from
:class:`~ivy_lsp.lsp.server.IvyLanguageServer` via Python's MRO.
"""

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import (
    LogCategory,
    LogEvent,
    StructuredLogAdapter,
    timed_phase,
)
from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.lsp.lsp_log_handler import LspLogHandler

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer as _LS

    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.lsp._protocols import IvyServerHost

    class _SetupBase(IvyServerHost, _LS): ...  # type: ignore[misc]

else:
    _SetupBase = object

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


class ServerSetupMixin(_SetupBase):
    """Server initialization and setup pipeline for IvyLanguageServer."""

    def _install_lsp_log_handler(self) -> None:
        """Add LSP notification handler and demote stderr to WARNING-only."""
        root = logging.getLogger()
        handler = LspLogHandler(self)  # type: ignore[arg-type]
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
        with timed_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="setup",
            name="configure_activity_logging",
            channel="lsp",
        ):
            self._configure_activity_logging()

        ws_folders = self.workspace.folders
        if ws_folders:
            ws_root = uri_to_path(list(ws_folders.values())[0].uri)
        else:
            ws_root = os.getcwd()

        # Load offline index context (graceful fallback to empty context)
        try:
            from ivy_lsp.core.workspace.context import WorkspaceContext

            self._workspace_context = WorkspaceContext.load(ws_root)
            if self._workspace_context.has_index():
                protocols = self._workspace_context.list_protocols()
                slog.info(
                    "Loaded offline index for %d protocol(s): %s",
                    len(protocols),
                    ", ".join(protocols),
                    extra={
                        "event": LogEvent(
                            LogCategory.MILESTONE,
                            "offline_index",
                            {"protocols": protocols},
                        )
                    },
                )
            else:
                slog.info(
                    "No offline index found at %s; will use live indexing",
                    ws_root,
                    extra={"event": LogEvent(LogCategory.DIAGNOSTIC, "offline_index")},
                )
        except Exception:
            logger.warning(
                "WorkspaceContext loading failed; proceeding without offline index",
                exc_info=True,
            )
            from ivy_lsp.core.workspace.context import WorkspaceContext
            from ivy_lsp.core.workspace.detection import WorkspaceConfig

            self._workspace_context = WorkspaceContext(
                workspace_root=ws_root,
                project_type="fallback",
                workspace_config=WorkspaceConfig(
                    workspace_root=ws_root,
                    detected_by="fallback",
                ),
            )

        # Load active workspace state with RF-5 tiebreak logic.
        # workspace_config comes from the WorkspaceContext that was just loaded;
        # protocol_id is set when a .ivyworkspace marker declares one.
        try:
            _ws_cfg = getattr(self._workspace_context, "workspace_config", None)
            _detected_protocol_id = (
                getattr(_ws_cfg, "protocol_id", None) if _ws_cfg is not None else None
            )
            _state_file = os.path.join(
                self._workspace_context.workspace_root,
                ".ivy-workspace-state.json",
            )
            if hasattr(self._workspace_context, "load_active_workspace"):
                self._workspace_context.load_active_workspace(
                    _state_file,
                    detected_protocol_id=_detected_protocol_id,
                )
                logger.debug(
                    "Active workspace loaded: set=%s, group=%s, set_by=%s",
                    self._workspace_context.active_workspace.is_set(),
                    self._workspace_context.active_workspace.active_group,
                    self._workspace_context.active_workspace.set_by,
                )
        except Exception:
            logger.debug(
                "load_active_workspace failed; proceeding with cleared state",
                exc_info=True,
            )

        with timed_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="setup",
            name="create_resolver",
            channel="lsp",
        ):
            resolver, ws_root = self._create_resolver(ws_root)
        if resolver is None:
            return

        with timed_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="setup",
            name="create_parser",
            channel="lsp",
        ):
            self._create_parser(resolver)

        # Tier diagnostic: probe parsing tier availability at startup
        try:
            from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor

            tier_info = TieredExtractor().probe_tiers()
            best = tier_info.get("best_available", 0)
            if best == 1:
                logger.info("Parsing tier: 1 (ast/parser) — full accuracy")
            elif best == 2:
                logger.warning(
                    "Parsing tier: 2 (lexer) — Tier 1 unavailable: %s",
                    tier_info.get("tier_1_error", "unknown"),
                )
            else:
                logger.warning("Parsing tier: 3 (regex) — Tier 1/2 unavailable")
        except Exception:
            logger.debug("Tier probe failed", exc_info=True)

        with timed_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="setup",
            name="create_indexer",
            channel="lsp",
            payload={"workspace_root": ws_root},
        ):
            if not self._create_indexer(ws_root, resolver):
                return

        with timed_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="setup",
            name="setup_analysis_pipeline",
            channel="lsp",
        ):
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
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.workspace.detection import detect_ivy_workspace

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

        # Workspace marker (.ivyworkspace) takes precedence over env vars;
        # fall back to env-based config only when detection returned nothing.
        include_paths = ws_config.include_paths
        exclude_paths = ws_config.exclude_paths
        if not include_paths:
            include_paths = get_config().include_paths
        if not exclude_paths:
            exclude_paths = get_config().exclude_paths
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

        _stdlib_path = None
        if ws_config.standard_library:
            _stdlib_path = os.path.join(ws_root, ws_config.standard_library)

        try:
            resolver = IncludeResolver(
                ws_root,
                ivy_include_path=_stdlib_path,
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
        """Create the Ivy parser.  Z3 is mandatory.

        Sets self._parser and self._full_mode.
        Raises RuntimeError if Z3 / ivy.ivy_parser is not importable.
        """
        try:
            # Verify the full Z3-dependent import chain is available.
            # ivy.ivy_parser -> ivy_actions -> ivy_module -> ivy_solver -> z3_shim
            import ivy.ivy_parser  # noqa: F401

            from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

            self._parser = IvyParserWrapper(resolve_callback=resolver.resolve)
            self._full_mode = True
            slog.info(
                "Full parser available (z3 found)",
                extra={"event": LogEvent(LogCategory.MILESTONE, "startup")},
            )
        except Exception as e:
            slog.error(
                "Z3 is required but ivy.ivy_parser could not be imported: %s",
                e,
                extra={
                    "event": LogEvent(
                        LogCategory.DIAGNOSTIC,
                        "startup",
                        {"reason": str(e)},
                    )
                },
            )
            raise RuntimeError(
                f"Z3 is required but not available: {e}. "
                "Install via 'pip install z3-solver'."
            ) from e

    def _create_indexer(self, ws_root: str, resolver: "IncludeResolver") -> bool:
        """Create the workspace indexer and run initial indexing.

        If an offline ``.ivy-index/`` exists (loaded via ``WorkspaceContext``),
        the fast-scan phase is replaced by pre-populating the indexer from
        the cached artifacts.  Post-indexing wiring and background deep
        parse still run as normal.

        Returns True on success, False if the indexer could not be created.
        """
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer

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

            ws_ctx = getattr(self, "_workspace_context", None)
            if ws_ctx is not None and ws_ctx.has_index():
                self._prepopulate_from_offline_index(ws_ctx)
            else:
                self._indexer.index_workspace()

            index_duration = time.time() - index_start
            self.state_tracker.set_indexed(index_duration)
            n_files = len(self._indexer._cache._cache) if self._indexer._cache else 0  # type: ignore[attr-defined]
            if n_files == 0:
                # Offline index pre-population bypasses the file cache;
                # fall back to progress-tracked file count.
                n_files = len(self._indexer._deep_index_progress.file_statuses)
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

    # ------------------------------------------------------------------
    # Offline index pre-population
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_symbol_file_paths(sym, abs_path: str) -> None:
        """Recursively set ``file_path`` on *sym* and all descendants.

        Symbols serialized by the offline index builder retain the
        absolute paths from the build machine.  This helper normalizes
        them to the current workspace's absolute path so that all
        downstream consumers (go-to-definition, document symbols, etc.)
        see consistent paths.
        """
        stack = [sym]
        while stack:
            s = stack.pop()
            s.file_path = abs_path
            stack.extend(s.children)

    def _prepopulate_from_offline_index(self, ws_ctx) -> None:
        """Replace the fast-scan phase with pre-built index data.

        Iterates over every :class:`ProtocolIndex` in *ws_ctx*, converts
        relative paths to absolute, and populates the indexer's
        ``_symbol_table``, ``_include_graph``, and ``_file_export_imports``.

        After pre-population the standard post-indexing wiring steps run
        (requirement graph, coverage edges, test scopes) followed by
        the background deep-parse thread (unchanged).
        """
        import threading

        from ivy_lsp.core.indexer.workspace_indexer import FileIndexStatus
        from ivy_lsp.core.parsing.symbols import IvySymbol

        prepop_start = time.time()
        total_files = 0
        total_symbols = 0
        total_edges = 0

        for proto_name, proto_idx in ws_ctx.protocol_indexes.items():
            # protocol_dir = workspace_root / protocol-testing / <proto>
            protocol_dir = os.path.dirname(proto_idx.index_dir)

            # -- 1. Symbols ------------------------------------------------
            for rel_path, sym_dicts in proto_idx.symbols.items():
                abs_path = os.path.join(protocol_dir, rel_path)
                for sd in sym_dicts:
                    try:
                        sym = IvySymbol.from_dict(sd)
                        self._patch_symbol_file_paths(sym, abs_path)
                        self._indexer._symbol_table.add_symbol(sym)
                        total_symbols += 1
                    except Exception:
                        logger.debug(
                            "Skipping corrupt symbol in %s/%s",
                            proto_name,
                            rel_path,
                            exc_info=True,
                        )
                total_files += 1

                # Mark file as shallow-indexed in progress tracking
                with self._indexer._progress_lock:
                    self._indexer._deep_index_progress.file_statuses[abs_path] = (
                        FileIndexStatus(
                            filepath=abs_path,
                            shallow_indexed=True,
                            last_indexed_at=time.time(),
                        )
                    )

            # -- 2. Include graph ------------------------------------------
            edges = proto_idx.includes.to_edges()
            for from_rel, to_rels in edges.items():
                abs_from = os.path.join(protocol_dir, from_rel)
                for to_rel in to_rels:
                    abs_to = os.path.join(protocol_dir, to_rel)
                    self._indexer._include_graph.add_edge(abs_from, abs_to)
                    total_edges += 1

            # -- 3. Exports / imports --------------------------------------
            for rel_path, export_info in proto_idx.exports.items():
                abs_path = os.path.join(protocol_dir, rel_path)
                # ExportImportInfo stores the originating file; update to
                # absolute path so downstream consumers (test scope builder,
                # coverage tools) find it.
                patched = type(export_info)(
                    file=abs_path,
                    exports=list(export_info.exports),
                    imports=list(export_info.imports),
                    export_lines=dict(export_info.export_lines),
                    import_lines=dict(export_info.import_lines),
                )
                with self._indexer._exports_lock:
                    self._indexer._file_export_imports[abs_path] = patched

            # -- 4. Requirement graph (optional pickle) --------------------
            if proto_idx.requirement_graph is not None:
                proto_idx.requirement_graph.remap_paths(protocol_dir)
                self._indexer._requirement_graph = proto_idx.requirement_graph

        # -- 5. Semantic model (optional pickle) ----------------------------
        loaded_model_protocols = 0
        for proto_name, proto_idx in ws_ctx.protocol_indexes.items():
            if proto_idx.semantic_model is not None:
                if self._semantic_model is None:
                    from ivy_lsp.core.semantic.model import SemanticModel

                    self._semantic_model = SemanticModel()
                    logger.info(
                        "Lazy-initialized SemanticModel for offline cache merge"
                    )
                try:
                    self._semantic_model.merge_from(proto_idx.semantic_model)
                    loaded_model_protocols += 1
                except Exception:
                    logger.debug(
                        "Skipping incompatible semantic model for %s",
                        proto_name,
                        exc_info=True,
                    )

        self._semantic_model_from_cache = loaded_model_protocols > 0
        if loaded_model_protocols > 0:
            slog.info(
                "Loaded cached semantic model from %d protocol(s)",
                loaded_model_protocols,
                extra={
                    "event": LogEvent(
                        LogCategory.MILESTONE,
                        "offline_semantic_model",
                        {"protocols_loaded": loaded_model_protocols},
                    )
                },
            )

        slog.info(
            "Pre-populated from offline index: %d files, %d symbols, %d include edges",
            total_files,
            total_symbols,
            total_edges,
            extra={
                "event": LogEvent(
                    LogCategory.MILESTONE,
                    "offline_index_prepopulate",
                    {
                        "files": total_files,
                        "symbols": total_symbols,
                        "include_edges": total_edges,
                    },
                )
            },
        )

        # Run post-indexing wiring (same sequence as index_workspace)
        self._indexer._wire_requirement_graph()
        self._indexer._load_requirement_manifests()
        self._indexer._wire_coverage_edges()
        self._indexer._compute_test_scopes()
        self._indexer._last_index_duration = time.time() - prepop_start
        self._indexer._last_index_time = time.time()

        # Phase 2: background full-parse from test entry points
        from ivy_lsp.core.parsing.fallback_parser import FallbackOnlyParser

        has_full_parser = not isinstance(self._indexer._parser, FallbackOnlyParser)
        if has_full_parser:
            with self._indexer._progress_lock:
                self._indexer._deep_index_running = True
            t = threading.Thread(
                target=self._indexer._deep_index_from_tests,
                daemon=True,
                name="ivy-deep-index",
            )
            t.start()

    def _setup_analysis_pipeline(self) -> None:
        """Set up semantic model, adapters, compiler manager, and analysis pipeline."""
        try:
            from ivy_lsp.core.adapters.null_adapter import (
                NullAstEnrichmentAdapter,
                NullCompilerAdapter,
            )
            from ivy_lsp.core.semantic.analysis_pipeline import AnalysisPipeline
            from ivy_lsp.core.semantic.model import SemanticModel

            if self._semantic_model is None:
                self._semantic_model = SemanticModel()

            if self._full_mode:
                try:
                    from ivy_lsp.core.adapters.ast_enrichment_adapter import (
                        AstEnrichmentAdapter,
                    )
                    from ivy_lsp.core.adapters.compiler_adapter import CompilerAdapter

                    enrichment = AstEnrichmentAdapter()

                    # Create CompilerManager for subprocess-based compilation
                    compiler_staging_dir = None
                    try:
                        from ivy_lsp.core.compilation.compiler_manager import (
                            CompilerManager,
                        )

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
                                    else False
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
                from ivy_lsp.lsp.commands import _find_enclosing_test

                return _find_enclosing_test(self, filepath)

            requirement_graph = getattr(self._indexer, "requirement_graph", None)
            assert self._parser is not None
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
            # Wire scope provider for scope-aware bulk T1/T2 filtering
            if requirement_graph is not None:
                self._analysis_pipeline.set_scope_provider(requirement_graph)
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
