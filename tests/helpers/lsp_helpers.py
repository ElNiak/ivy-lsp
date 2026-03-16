"""Shared LSP test helpers for Ivy LSP tests."""

from __future__ import annotations

from pathlib import Path
from typing import List

from lsprotocol import types as lsp


def create_indexed_workspace(workspace_path: str):
    """Build a fully indexed WorkspaceIndexer from a directory of .ivy files.

    Args:
        workspace_path: Absolute path to directory containing .ivy files.

    Returns:
        A WorkspaceIndexer instance with all files indexed.
    """
    from ivy_lsp.indexer.include_resolver import IncludeResolver
    from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.parsing.parser_session import IvyParserWrapper

    parser = IvyParserWrapper()
    resolver = IncludeResolver(workspace_path)
    indexer = WorkspaceIndexer(workspace_path, parser, resolver)
    indexer.index_workspace()
    return indexer


def parse_to_symbols(source: str, filename: str = "test.ivy"):
    """Parse Ivy source and return a list of IvySymbol objects.

    Args:
        source: Ivy source code string.
        filename: Virtual filename for the parsed content.

    Returns:
        List of IvySymbol objects extracted from the AST.
    """
    from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
    from ivy_lsp.parsing.parser_session import IvyParserWrapper

    wrapper = IvyParserWrapper()
    result = wrapper.parse(source, filename)
    if not result.success:
        # Fall back to fallback scanner
        from ivy_lsp.parsing.fallback_scanner import scan_symbols

        return scan_symbols(source, filename)
    return ast_to_symbols(result.ast, filename, source)


def get_fixture_path(fixture_name: str) -> Path:
    """Get the absolute path to a test fixture file.

    Args:
        fixture_name: Relative path within the fixtures/ directory (e.g., "with_include/types.ivy").

    Returns:
        Absolute path to the fixture file.
    """
    return Path(__file__).parent / "fixtures" / fixture_name


def position(line: int, character: int) -> lsp.Position:
    """Create an LSP Position (0-based)."""
    return lsp.Position(line=line, character=character)
