"""Multi-process parallel deep indexing for Ivy workspaces."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List


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


class ParallelDeepIndexer:
    """Multi-process deep indexer using ProcessPoolExecutor."""

    # Below this count, serial is faster than fork overhead
    SERIAL_THRESHOLD = 3

    def __init__(self, num_workers: int = 0) -> None:
        if num_workers <= 0:
            num_workers = max(1, (os.cpu_count() or 1) // 2)
        self._num_workers = num_workers

    def parse_files(
        self, filepaths: List[str],
    ) -> Dict[str, WorkerResult]:
        if len(filepaths) <= self.SERIAL_THRESHOLD:
            return {f: worker_parse_file(f) for f in filepaths}

        results: Dict[str, WorkerResult] = {}
        with ProcessPoolExecutor(max_workers=self._num_workers) as pool:
            futures = {pool.submit(worker_parse_file, f): f for f in filepaths}
            for future in futures:
                filepath = futures[future]
                try:
                    results[filepath] = future.result(timeout=60)
                except Exception as e:
                    results[filepath] = WorkerResult(
                        filepath=filepath, success=False,
                        symbols=[], errors=[str(e)],
                    )
        return results
