"""Ivy Language Server implementation."""

import logging
import os

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from ivy_lsp import __version__

logger = logging.getLogger(__name__)


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

        from ivy_lsp.features import (
            code_lens,
            commands,
            completion,
            definition,
            diagnostics,
            document_symbols,
            hover,
            references,
            workspace_symbols,
        )

        document_symbols.register(self)
        workspace_symbols.register(self)
        definition.register(self)
        references.register(self)
        hover.register(self)
        completion.register(self)
        diagnostics.register(self)
        code_lens.register(self)
        commands.register(self)

        @self.feature(lsp.INITIALIZED)
        def on_initialized(params: lsp.InitializedParams) -> None:
            logger.info("Ivy Language Server initialized")
            self._setup_indexer()
            mode = "full" if self._full_mode else "light"
            self.window_log_message(
                lsp.LogMessageParams(
                    type=lsp.MessageType.Info,
                    message=f"Ivy LSP running in {mode} mode",
                )
            )

        @self.feature(lsp.SHUTDOWN)
        def on_shutdown(params) -> None:
            self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        """Clean up the staging directory on shutdown."""
        if self._indexer and hasattr(self._indexer, "_resolver"):
            try:
                self._indexer._resolver.cleanup_staging()
                logger.info("Staging directory cleaned up")
            except Exception:
                logger.exception("Failed to clean up staging directory")

    def _setup_indexer(self):
        """Create and populate the workspace indexer."""
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer

        ws_folders = self.workspace.folders
        if ws_folders:
            root = list(ws_folders.values())[0].uri.replace("file://", "")
        else:
            root = os.getcwd()

        # Try full parser (requires z3). Fall back to lexer-only mode.
        try:
            from ivy_lsp.parsing.parser_session import IvyParserWrapper

            self._parser = IvyParserWrapper()
            self._full_mode = True
            logger.info("Full parser available (z3 found)")
        except (ImportError, ModuleNotFoundError) as e:
            from ivy_lsp.parsing.fallback_parser import FallbackOnlyParser

            self._parser = FallbackOnlyParser()
            self._full_mode = False
            logger.info("z3 not available (%s); running in light mode", e)

        # Read include/exclude paths from environment
        raw_includes = os.environ.get("IVY_LSP_INCLUDE_PATHS", "")
        include_paths = [p.strip() for p in raw_includes.split(",") if p.strip()]
        raw_excludes = os.environ.get("IVY_LSP_EXCLUDE_PATHS", "")
        exclude_paths = [p.strip() for p in raw_excludes.split(",") if p.strip()]
        if include_paths:
            logger.info("Include paths: %s", include_paths)
        if exclude_paths:
            logger.info("Exclude paths: %s", exclude_paths)

        resolver = IncludeResolver(
            root,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )

        # Create flat staging directory (mirrors ivyc's include/1.7/ model)
        try:
            staging_dir = resolver.create_staging_directory()
            logger.info("Created staging directory: %s", staging_dir)
        except Exception:
            logger.exception("Failed to create staging, falling back to direct scan")

        self._indexer = WorkspaceIndexer(root, self._parser, resolver)
        try:
            self._indexer.index_workspace()
            n_files = len(self._indexer._cache._cache)
            n_symbols = sum(1 for _ in self._indexer._symbol_table.all_symbols())
            logger.info("Indexed %d files, %d symbols", n_files, n_symbols)
        except Exception:
            logger.exception("Workspace indexing failed")

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
                    compiler = CompilerAdapter()
                except ImportError:
                    enrichment = NullAstEnrichmentAdapter()
                    compiler = NullCompilerAdapter()
            else:
                enrichment = NullAstEnrichmentAdapter()
                compiler = NullCompilerAdapter()

            self._analysis_pipeline = AnalysisPipeline(
                self._semantic_model, self._parser, enrichment, compiler
            )
            logger.info("Semantic model and analysis pipeline initialized")
        except Exception:
            logger.exception("Semantic model setup failed")
