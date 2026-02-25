"""Null (no-op) adapter implementations for graceful degradation.

Used when z3/ivy dependencies are unavailable.  Every method returns
empty or failure results so the pipeline can still run Tier 1 analysis.
"""

from __future__ import annotations

from typing import Any, List

from ivy_lsp.adapters.protocols import CompileResult, TypeAnnotation
from ivy_lsp.parsing.parser_session import ParseResult


class NullParserAdapter:
    """No-op parser that always returns a failed ParseResult."""

    def parse(self, source: str, filename: str) -> ParseResult:
        return ParseResult(ast=None, errors=[], success=False, filename=filename)


class NullAstEnrichmentAdapter:
    """No-op enrichment adapter that returns no type annotations."""

    def extract_type_info(
        self, ast: Any, filename: str, source: str
    ) -> List[TypeAnnotation]:
        return []


class NullCompilerAdapter:
    """No-op compiler adapter that returns a failed CompileResult."""

    def compile(self, source: str, filename: str) -> CompileResult:
        return CompileResult(success=False, errors=[])
