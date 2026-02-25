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

        from ivy_lsp.features import (
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

    def initialized(self, params):
        """Handle the initialized notification."""
        logger.info("Ivy Language Server initialized")
        self._setup_indexer()
        mode = "full" if self._full_mode else "light"
        self.window_log_message(
            lsp.LogMessageParams(
                type=lsp.MessageType.Info,
                message=f"Ivy LSP running in {mode} mode",
            )
        )

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

        resolver = IncludeResolver(root)
        self._indexer = WorkspaceIndexer(root, self._parser, resolver)
        try:
            self._indexer.index_workspace()
            logger.info("Workspace indexing complete")
        except Exception:
            logger.exception("Workspace indexing failed")
