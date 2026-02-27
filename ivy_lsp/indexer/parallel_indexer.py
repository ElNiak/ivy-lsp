"""Multi-process parallel deep indexing for Ivy workspaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class WorkerResult:
    """Result from a single worker parse."""

    filepath: str
    success: bool
    symbols: List[dict]
    errors: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)


def worker_parse_file(filepath: str) -> WorkerResult:
    """Parse a single .ivy file in a worker process.

    Falls back to fallback_scan if full parsing is unavailable.
    Returns serialized symbols (dicts, not IvySymbol objects) for
    cross-process transfer.
    """
    try:
        with open(filepath) as f:
            source = f.read()
    except OSError as e:
        return WorkerResult(filepath=filepath, success=False, symbols=[], errors=[str(e)])

    # Try full parser first
    try:
        from ivy_lsp.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols

        parser = IvyParserWrapper()
        result = parser.parse(source, filepath)
        if result.success and result.ast is not None:
            syms = ast_to_symbols(result.ast, filepath, source)
            return WorkerResult(
                filepath=filepath,
                success=True,
                symbols=[s.to_dict() for s in syms],
                errors=[],
            )
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: lexer-based scan
    from ivy_lsp.parsing.fallback_scanner import fallback_scan

    symbols, _error_info = fallback_scan(source, filepath)
    return WorkerResult(
        filepath=filepath,
        success=False,
        symbols=[s.to_dict() for s in symbols],
        errors=[],
    )
