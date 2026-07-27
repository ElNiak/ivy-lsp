"""Shared LSP test helpers for Ivy LSP tests."""

from __future__ import annotations

from lsprotocol import types as lsp


def parse_to_symbols(source: str, filename: str = "test.ivy"):
    """Parse Ivy source and return a list of IvySymbol objects.

    Args:
        source: Ivy source code string.
        filename: Virtual filename for the parsed content.

    Returns:
        List of IvySymbol objects extracted from the AST.
    """
    from ivy_lsp.core.parsing.ast_to_symbols import ast_to_symbols
    from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

    wrapper = IvyParserWrapper()
    result = wrapper.parse(source, filename)
    if not result.success:
        # Fall back to fallback scanner
        from ivy_lsp.core.parsing.fallback_scanner import scan_symbols

        return scan_symbols(source, filename)
    return ast_to_symbols(result.ast, filename, source)


def position(line: int, character: int) -> lsp.Position:
    """Create an LSP Position (0-based)."""
    return lsp.Position(line=line, character=character)
