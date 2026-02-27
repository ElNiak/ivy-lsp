"""Orchestrates subprocess-based Ivy compilations with caching.

Thread-safe. Used by AnalysisPipeline for Tier 3 analysis and
by custom commands (ivy/compile, ivy/verify) for in-process module data.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ivy_lsp.compilation.ir import CompiledModuleIR

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    ir: CompiledModuleIR
    content_hash: str
    timestamp: float


class CompilerManager:
    """Manages subprocess-based Ivy compilations with caching and lifecycle."""

    def __init__(
        self,
        staging_dir: Optional[str] = None,
        timeout: float = 300.0,
        cache_ttl: float = 600.0,
    ) -> None:
        self._staging_dir = staging_dir
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._active: Dict[str, Any] = {}  # Process (spawn or fork)

    def compile_async(
        self,
        source: str,
        filepath: str,
        callback: Callable[[CompiledModuleIR], None],
    ) -> None:
        """Start compilation in a subprocess, call back with result."""
        content_hash = hashlib.sha256(source.encode()).hexdigest()

        cached = self._get_cached_by_hash(filepath, content_hash)
        if cached is not None:
            callback(cached)
            return

        self._cancel_if_running(filepath)

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_worker_entry,
            args=(source, filepath, child_conn, self._staging_dir),
            daemon=True,
        )

        def _wait():
            try:
                proc.start()
                child_conn.close()
                if parent_conn.poll(self._timeout):
                    ir = parent_conn.recv()
                    self._put_cache(filepath, content_hash, ir)
                    callback(ir)
                else:
                    proc.kill()
                    proc.join(timeout=5)
                    ir = CompiledModuleIR.empty(
                        filepath,
                        errors=[f"Compilation timed out after {self._timeout}s"],
                        duration=self._timeout,
                    )
                    callback(ir)
            except Exception as exc:
                callback(
                    CompiledModuleIR.empty(
                        filepath, errors=[str(exc)], duration=0.0
                    )
                )
            finally:
                try:
                    parent_conn.close()
                except Exception:
                    pass
                with self._lock:
                    self._active.pop(filepath, None)

        with self._lock:
            self._active[filepath] = proc

        t = threading.Thread(
            target=_wait,
            daemon=True,
            name=f"ivy-compile-{os.path.basename(filepath)}",
        )
        t.start()

    def compile_sync(
        self, source: str, filepath: str
    ) -> CompiledModuleIR:
        """Blocking compilation. For use in custom commands."""
        event = threading.Event()
        result_holder: list = [None]

        def _cb(ir: CompiledModuleIR) -> None:
            result_holder[0] = ir
            event.set()

        self.compile_async(source, filepath, _cb)
        event.wait(timeout=self._timeout + 10)
        return result_holder[0] or CompiledModuleIR.empty(
            filepath, errors=["Compilation did not complete"], duration=0.0
        )

    def get_cached(self, filepath: str) -> Optional[CompiledModuleIR]:
        """Return cached IR for *filepath* if present and not stale."""
        with self._lock:
            entry = self._cache.get(filepath)
            if entry is None:
                return None
            if time.time() - entry.timestamp > self._cache_ttl:
                del self._cache[filepath]
                return None
            return entry.ir

    def invalidate(self, filepath: str) -> None:
        """Remove cached compilation for *filepath*."""
        with self._lock:
            self._cache.pop(filepath, None)

    def invalidate_dependents(
        self, filepath: str, include_graph: Any
    ) -> None:
        """Invalidate *filepath* and all files that transitively include it."""
        self.invalidate(filepath)
        if hasattr(include_graph, "get_includers"):
            for includer in include_graph.get_includers(filepath):
                self.invalidate(includer)

    def shutdown(self) -> None:
        """Kill all active compilation subprocesses."""
        with self._lock:
            for proc in self._active.values():
                try:
                    proc.kill()
                    proc.join(timeout=2)
                except Exception:
                    pass
            self._active.clear()
            self._cache.clear()

    def _get_cached_by_hash(
        self, filepath: str, content_hash: str
    ) -> Optional[CompiledModuleIR]:
        with self._lock:
            entry = self._cache.get(filepath)
            if entry is None:
                return None
            if entry.content_hash != content_hash:
                return None
            if time.time() - entry.timestamp > self._cache_ttl:
                del self._cache[filepath]
                return None
            return entry.ir

    def _put_cache(
        self, filepath: str, content_hash: str, ir: CompiledModuleIR
    ) -> None:
        with self._lock:
            self._cache[filepath] = _CacheEntry(
                ir=ir, content_hash=content_hash, timestamp=time.time()
            )

    def _cancel_if_running(self, filepath: str) -> None:
        with self._lock:
            proc = self._active.pop(filepath, None)
        if proc is not None:
            try:
                proc.kill()
                proc.join(timeout=2)
            except Exception:
                pass


def _worker_entry(
    source: str,
    filename: str,
    result_conn: multiprocessing.connection.Connection,
    staging_dir: Optional[str],
) -> None:
    """Trampoline into the real worker (avoids pickling issues)."""
    from ivy_lsp.compilation.worker import compiler_worker

    compiler_worker(source, filename, result_conn, staging_dir)
