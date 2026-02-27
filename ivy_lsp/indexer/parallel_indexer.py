"""Multi-process parallel deep indexing for Ivy workspaces."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    """Result from a single worker parse."""

    filepath: str
    success: bool
    symbols: List[dict]
    errors: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)


def worker_parse_file(
    filepath: str,
    resolver_config: Optional[dict] = None,
) -> WorkerResult:
    """Parse a single .ivy file in a worker process.

    Falls back to fallback_scan if full parsing is unavailable.
    Returns serialized symbols (dicts, not IvySymbol objects) for
    cross-process transfer.

    Args:
        filepath: Absolute path to the .ivy file.
        resolver_config: Serialized IncludeResolver config (from
            ``IncludeResolver.to_config_dict()``).  When provided, the
            worker creates a local resolver so that ``include`` directives
            can search staging, workspace root, and stdlib — not just the
            same directory.
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

        resolve_callback = None
        if resolver_config:
            from ivy_lsp.indexer.include_resolver import IncludeResolver

            resolver = IncludeResolver.from_config(resolver_config)
            resolve_callback = resolver.resolve

        parser = IvyParserWrapper(resolve_callback=resolve_callback)
        result = parser.parse(source, filepath)
        if result.success and result.ast is not None:
            syms = ast_to_symbols(result.ast, filepath, source)
            return WorkerResult(
                filepath=filepath,
                success=True,
                symbols=[s.to_dict() for s in syms],
                errors=[],
            )
        # Parse ran but failed — record errors for diagnostics
        error_strs = []
        if hasattr(result, "errors") and result.errors:
            error_strs = [str(e) for e in result.errors]
        logger.debug(
            "Worker: parse returned success=False for %s: %s",
            filepath,
            "; ".join(error_strs) if error_strs else "(no error details)",
        )
    except ImportError as e:
        logger.warning("Worker: ivy import failed for %s: %s", filepath, e)
    except Exception as e:
        logger.warning("Worker: parse failed for %s: %s", filepath, e)

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

    def __init__(
        self,
        num_workers: int = 0,
        resolver_config: Optional[dict] = None,
    ) -> None:
        if num_workers <= 0:
            num_workers = max(1, (os.cpu_count() or 1) // 2)
        self._num_workers = num_workers
        self._resolver_config = resolver_config

    def parse_files(
        self, filepaths: List[str],
    ) -> Dict[str, WorkerResult]:
        cfg = self._resolver_config
        if len(filepaths) <= self.SERIAL_THRESHOLD:
            return {f: worker_parse_file(f, cfg) for f in filepaths}

        results: Dict[str, WorkerResult] = {}
        with ProcessPoolExecutor(max_workers=self._num_workers) as pool:
            futures = {
                pool.submit(worker_parse_file, f, cfg): f
                for f in filepaths
            }
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
