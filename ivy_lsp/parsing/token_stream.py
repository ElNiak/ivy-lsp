"""Shared Ivy PLY lexer tokenization.

Single entry point for tokenizing Ivy source with the PLY lexer.
Used by both fallback_scanner (symbol extraction) and
lexer_requirement_extractor (requirement extraction) to avoid
double-tokenizing the same file.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TokenStream:
    """Result of tokenizing Ivy source with the PLY lexer."""

    tokens: list
    source: str
    filename: str
    lines: List[str] = field(default_factory=list)
    error_info: Optional[dict] = None

    def __post_init__(self):
        if not self.lines:
            self.lines = self.source.split("\n")


def tokenize_ivy(source: str, filename: str = "<string>") -> TokenStream:
    """Tokenize source using the Ivy PLY lexer.

    Returns a TokenStream with best-effort tokens (partial on error).
    Raises ImportError if ivy.ivy_lexer is not available.
    """
    from ivy.ivy_lexer import LexerVersion
    from ivy.ivy_lexer import lexer as ivy_lexer

    lex_copy = copy.copy(ivy_lexer)
    tokens: list = []
    error_info: Optional[dict] = None

    with LexerVersion([1, 7]):
        lex_copy.input(source)
        while True:
            try:
                tok = lex_copy.token()
            except Exception as exc:
                line = 1
                lineno = getattr(exc, "lineno", None)
                if lineno is not None:
                    if hasattr(lineno, "line") and isinstance(lineno.line, int):
                        line = lineno.line
                    elif isinstance(lineno, int):
                        line = lineno
                error_info = {"line": line, "message": str(exc)}
                logger.warning(
                    "Lexer error in %s after %d tokens; returning partial results",
                    filename,
                    len(tokens),
                    exc_info=True,
                )
                break
            if tok is None:
                break
            tokens.append(tok)

    return TokenStream(
        tokens=tokens,
        source=source,
        filename=filename,
        error_info=error_info,
    )
