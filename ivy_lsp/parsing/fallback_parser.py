"""Fallback parser for light mode (no z3 dependency).

When the full Ivy parser is unavailable (z3 not installed), this provides
a parser-compatible interface that always returns ``success=False``, causing
callers like :class:`WorkspaceIndexer` to fall through to the lexer-based
:func:`fallback_scan` for symbol extraction.
"""

from __future__ import annotations

from ivy_lsp.parsing.parser_session import ParseResult


class FallbackOnlyParser:
    """Parser substitute that signals callers to use the fallback scanner."""

    def parse(self, source: str, filename: str = "<string>") -> ParseResult:
        """Return a failing ParseResult so the indexer uses fallback_scan."""
        return ParseResult(ast=None, errors=[], success=False, filename=filename)
