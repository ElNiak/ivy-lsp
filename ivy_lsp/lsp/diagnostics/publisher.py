"""Diagnostics feature for Ivy LSP.

Cache layer, ``_convert_error_to_diagnostic``, and ``register()`` live
here.  Pure compute functions are in ``diagnostic_compute.py`` and
re-exported below so existing imports continue to work.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.lsp.diagnostics.compute import (
    _EXPORT_RE,
    check_structural_issues,
    compute_diagnostics,
    run_deep_diagnostics,
)

logger = logging.getLogger(__name__)

DEBOUNCE_DELAY = float(os.environ.get("IVY_LSP_DEBOUNCE_DELAY", "0.3"))


def _cancel_task(tasks: dict[str, asyncio.Task], key: str) -> None:
    """Cancel and remove an existing task if it's still running."""
    old = tasks.pop(key, None)
    if old and not old.done():
        old.cancel()


def _register_task(tasks: dict[str, asyncio.Task], key: str, coro) -> asyncio.Task:
    """Cancel any existing task for key, create new one with auto-cleanup."""
    _cancel_task(tasks, key)
    task = asyncio.get_running_loop().create_task(coro)
    task.add_done_callback(
        lambda t, k=key: tasks.pop(k, None) if tasks.get(k) is t else None
    )
    tasks[key] = task
    return task


@dataclass
class _CachedDiagnosticEntry:
    """Per-URI cached diagnostic result for pull diagnostics."""

    result_id: str
    source_hash: str
    diagnostics: List[lsp.Diagnostic]
    deep_diagnostics: Optional[List[lsp.Diagnostic]] = None


class DiagnosticCache:
    """Thread-safe per-URI diagnostic cache for LSP 3.17 pull diagnostics.

    Stores fast diagnostics and deep diagnostics (ivy_check) separately.
    ``result_id`` is derived from the source hash + a suffix indicating
    whether deep diagnostics are present, so pull clients will re-request
    when deep results arrive.
    """

    def __init__(self) -> None:
        """Initialize an empty diagnostic cache."""
        self._lock = Lock()
        self._entries: Dict[str, _CachedDiagnosticEntry] = {}

    @staticmethod
    def _hash(source: str) -> str:
        return hashlib.sha256(source.encode()).hexdigest()[:16]

    @staticmethod
    def _result_id(source_hash: str, has_deep: bool) -> str:
        return source_hash + ("-deep" if has_deep else "-fast")

    def update_fast(self, uri: str, source: str, diags: List[lsp.Diagnostic]) -> str:
        """Update fast diagnostics for *uri*.  Returns the new ``result_id``."""
        h = self._hash(source)
        with self._lock:
            existing = self._entries.get(uri)
            # Preserve deep diagnostics if source hasn't changed
            deep = (
                existing.deep_diagnostics
                if existing and existing.source_hash == h
                else None
            )
            rid = self._result_id(h, deep is not None)
            self._entries[uri] = _CachedDiagnosticEntry(rid, h, diags, deep)
            return rid

    def update_deep(self, uri: str, deep: List[lsp.Diagnostic]) -> Optional[str]:
        """Update deep diagnostics overlay.  Returns new ``result_id`` or ``None``."""
        with self._lock:
            e = self._entries.get(uri)
            if e is None:
                return None
            e.deep_diagnostics = deep
            e.result_id = self._result_id(e.source_hash, True)
            return e.result_id

    def get(self, uri: str) -> Optional[_CachedDiagnosticEntry]:
        """Return the cached entry for *uri*, or ``None``."""
        with self._lock:
            return self._entries.get(uri)

    def get_merged(self, uri: str) -> Optional[Tuple[str, List[lsp.Diagnostic]]]:
        """Return ``(result_id, fast + deep diagnostics)`` or ``None``."""
        with self._lock:
            e = self._entries.get(uri)
            if e is None:
                return None
            merged = list(e.diagnostics)
            if e.deep_diagnostics:
                merged.extend(e.deep_diagnostics)
            return (e.result_id, merged)

    def invalidate(self, uri: str) -> None:
        """Remove cached entry for *uri*."""
        with self._lock:
            self._entries.pop(uri, None)

    def bump_result_id(self, uri: str) -> Optional[str]:
        """Bump the result_id for *uri* without changing diagnostics.

        Used to signal pull-mode clients that they should re-request,
        e.g., after background semantic analysis completes.
        """
        import time as _t

        with self._lock:
            e = self._entries.get(uri)
            if e is None:
                return None
            e.result_id = f"{e.source_hash}-{int(_t.time() * 1000)}"
            return e.result_id

    def all_uris(self) -> List[str]:
        """Return all cached URIs."""
        with self._lock:
            return list(self._entries.keys())


def _convert_error_to_diagnostic(error: Any, source: str) -> lsp.Diagnostic:
    """Convert a single Ivy parse error to an LSP Diagnostic.

    Handles three error representations:
    - Error objects with ``.msg`` and ``.lineno`` attributes (IvyError)
    - Raw parser tuples ``(symbol_name, loc1, loc2, ...)``, formatted
      via ``format_ivy_error()`` with line extracted from first location
    - Generic fallback: ``str(error)``
    """
    from ivy_lsp.infra.utils.ivy_output import format_ivy_error

    line = 0
    message = str(error)

    if hasattr(error, "lineno"):
        lineno = error.lineno
        if hasattr(lineno, "line") and isinstance(lineno.line, int) and lineno.line > 0:
            line = lineno.line - 1

    if hasattr(error, "msg"):
        message = error.msg
    elif isinstance(error, tuple) and len(error) >= 1 and isinstance(error[0], str):
        # Raw parser tuples: (symbol_name, loc1, loc2, ...)
        message = format_ivy_error(error)
        # Use the first location with a valid line number
        for loc in error[1:]:
            if isinstance(loc, tuple) and len(loc) >= 2:
                if isinstance(loc[1], int) and loc[1] > 0:
                    line = loc[1] - 1
                    break

    lines = source.split("\n")
    line_len = len(lines[line]) if line < len(lines) else 0

    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=line, character=0),
            end=lsp.Position(line=line, character=line_len),
        ),
        message=message,
        severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )


def register(server) -> None:
    """Register diagnostic handlers for didOpen, didChange, didSave, and didClose."""
    _debounce_tasks: Dict[str, asyncio.Task] = {}
    _deep_tasks: Dict[str, asyncio.Task] = {}

    def _get_semantic_model():
        return getattr(server, "semantic_model", None)

    def _run_pipeline(source: str, filepath: str, trigger: str) -> Any:
        """Run the analysis pipeline. Returns the ParseResult (or None)."""
        pipeline = getattr(server, "analysis_pipeline", None)
        if pipeline:
            try:
                return pipeline.analyze(source, filepath, trigger)
            except Exception:
                logger.warning(
                    "Pipeline analysis failed for %s", filepath, exc_info=True
                )
        return None

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    async def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
        uri = params.text_document.uri
        server._last_active_uri = uri
        doc = server.workspace.get_text_document(uri)
        filepath = uri_to_path(uri)
        source = doc.source or ""

        # Notify session overlay of file open (fast Tier 2/3 indexing)
        ws_ctx = getattr(server, "_workspace_context", None)
        if ws_ctx is not None and hasattr(ws_ctx, "overlay") and source:
            try:
                ws_ctx.overlay.notify_file_change(filepath, source)
            except Exception:
                logger.debug("Overlay notification failed on open", exc_info=True)
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, _run_pipeline, source, filepath, "change"
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )
        server.diagnostic_cache.update_fast(uri, source, diags)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=uri, version=doc.version, diagnostics=diags
            )
        )

    @server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    async def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
        uri = params.text_document.uri
        server._last_active_uri = uri

        # Immediate structural diagnostics (no debounce) for fast feedback
        try:
            doc = server.workspace.get_text_document(uri)
            filepath = uri_to_path(uri)
            source = doc.source or ""

            # Notify session overlay of file change (fast Tier 2/3 indexing)
            ws_ctx = getattr(server, "_workspace_context", None)
            if ws_ctx is not None and hasattr(ws_ctx, "overlay"):
                try:
                    ws_ctx.overlay.notify_file_change(filepath, source)
                except Exception:
                    logger.debug("Overlay notification failed", exc_info=True)
            fast_diags = check_structural_issues(source, filepath, server.indexer)
            server.diagnostic_cache.update_fast(uri, source, fast_diags)
            server.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(
                    uri=uri, version=doc.version, diagnostics=fast_diags
                )
            )
        except Exception:
            logger.debug("Immediate T1 push failed for %s", uri, exc_info=True)

        async def _debounced():
            try:
                await asyncio.sleep(DEBOUNCE_DELAY)
                doc = server.workspace.get_text_document(uri)
                filepath = uri_to_path(uri)
                source = doc.source or ""
                loop = asyncio.get_running_loop()
                pipeline_result = await loop.run_in_executor(
                    None, _run_pipeline, source, filepath, "change"
                )
                diags = await loop.run_in_executor(
                    None,
                    compute_diagnostics,
                    server.parser,
                    source,
                    filepath,
                    server.indexer,
                    _get_semantic_model(),
                    pipeline_result,
                )
                server.diagnostic_cache.update_fast(uri, source, diags)
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(
                        uri=uri, version=doc.version, diagnostics=diags
                    )
                )
            except asyncio.CancelledError:
                logger.debug("Debounced diagnostics task cancelled for %s", uri)
            except Exception:
                logger.warning(
                    "Debounced diagnostics failed for %s", uri, exc_info=True
                )

        _register_task(_debounce_tasks, uri, _debounced())

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    async def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
        uri = params.text_document.uri
        filepath = uri_to_path(uri)
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, _run_pipeline, source, filepath, "save"
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )
        server.diagnostic_cache.update_fast(uri, source, diags)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=uri, version=doc.version, diagnostics=diags
            )
        )

        if filepath.endswith(".ivy") and server.indexer is not None:
            await loop.run_in_executor(
                None, server.indexer.reindex_file_with_dependents, filepath
            )

        doc_version = doc.version

        async def _deep():
            try:
                import os

                from ivy_lsp.lsp.commands import (
                    _find_enclosing_test,
                    _resolve_via_staging,
                )

                enclosing = _find_enclosing_test(server, filepath)
                if enclosing is not None:
                    deep_filepath = _resolve_via_staging(server, enclosing)
                elif not _EXPORT_RE.search(source):
                    # Module file without test scope — can't compile standalone.
                    # Still trigger a diagnostic refresh so pull clients re-request
                    # (previous fast diagnostics may have cached a stale result_id).
                    try:
                        server.workspace_diagnostic_refresh(None)
                    except Exception:
                        pass
                    return
                else:
                    deep_filepath = _resolve_via_staging(server, filepath)
                deep = await run_deep_diagnostics(
                    deep_filepath,
                    cwd=os.path.dirname(deep_filepath),
                )
                server.diagnostic_cache.update_deep(uri, deep)
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(
                        uri=uri, version=doc_version, diagnostics=diags + deep
                    )
                )
                # Notify pull-mode clients to re-request diagnostics
                try:
                    server.workspace_diagnostic_refresh(None)
                except Exception:
                    logger.debug("workspace/diagnostic/refresh not supported by client")
            except asyncio.CancelledError:
                logger.debug("Deep diagnostics task cancelled for %s", uri)
            except Exception:
                logger.warning(
                    "Deep diagnostics task failed for %s", uri, exc_info=True
                )

        _register_task(_deep_tasks, uri, _deep())

    @server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
        uri = params.text_document.uri
        _cancel_task(_debounce_tasks, uri)
        _cancel_task(_deep_tasks, uri)
        # Clear diagnostics for the closed document
        server.diagnostic_cache.invalidate(uri)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )

    # -- LSP 3.17 Pull Diagnostics -----------------------------------------

    @server.feature(
        lsp.TEXT_DOCUMENT_DIAGNOSTIC,
        lsp.DiagnosticOptions(
            identifier="ivy",
            inter_file_dependencies=True,
            workspace_diagnostics=True,
        ),
    )
    async def text_document_diagnostic(
        params: lsp.DocumentDiagnosticParams,
    ) -> (
        lsp.RelatedFullDocumentDiagnosticReport
        | lsp.RelatedUnchangedDocumentDiagnosticReport
    ):
        uri = params.text_document.uri
        cache = server.diagnostic_cache

        # Fast path: unchanged
        if params.previous_result_id is not None:
            entry = cache.get(uri)
            if entry is not None and entry.result_id == params.previous_result_id:
                return lsp.RelatedUnchangedDocumentDiagnosticReport(
                    result_id=entry.result_id,
                )

        # Get source (open doc or disk)
        try:
            doc = server.workspace.get_text_document(uri)
            source = doc.source or ""
        except KeyError:
            filepath = uri_to_path(uri)
            try:
                with open(filepath, "r") as f:
                    source = f.read()
            except OSError:
                return lsp.RelatedFullDocumentDiagnosticReport(
                    items=[],
                    result_id="empty",
                )

        filepath = uri_to_path(uri)
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            _run_pipeline,
            source,
            filepath,
            "pull",
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )

        result_id = cache.update_fast(uri, source, diags)
        # Merge cached deep diagnostics if available
        entry = cache.get(uri)
        if entry and entry.deep_diagnostics:
            diags = diags + entry.deep_diagnostics
            result_id = entry.result_id

        return lsp.RelatedFullDocumentDiagnosticReport(
            items=diags,
            result_id=result_id,
        )

    @server.feature(lsp.WORKSPACE_DIAGNOSTIC)
    async def workspace_diagnostic(
        params: lsp.WorkspaceDiagnosticParams,
    ) -> lsp.WorkspaceDiagnosticReport:
        cache = server.diagnostic_cache
        items: list = []

        # Build lookup of previous result IDs
        prev_ids: Dict[str, str] = {}
        for prev in params.previous_result_ids:
            prev_ids[prev.uri] = prev.value

        # Collect URIs: open docs + indexed workspace files
        uris: set = set()
        for doc_uri in server.workspace.text_documents:
            uris.add(doc_uri)
        if server.indexer is not None:
            graph = getattr(server.indexer, "requirement_graph", None)
            for fp in server.indexer.get_all_ivy_file_paths():
                # Skip files not in any test scope (orphans)
                if graph is not None and not graph.get_tests_for_file(fp):
                    continue
                uris.add(f"file://{fp}")

        for uri in sorted(uris):
            prev_rid = prev_ids.get(uri)

            # Unchanged check
            if prev_rid is not None:
                entry = cache.get(uri)
                if entry is not None and entry.result_id == prev_rid:
                    items.append(
                        lsp.WorkspaceUnchangedDocumentDiagnosticReport(
                            uri=uri,
                            version=None,
                            result_id=entry.result_id,
                        )
                    )
                    continue

            # Return cached if available
            cached = cache.get_merged(uri)
            if cached is not None:
                rid, merged = cached
                items.append(
                    lsp.WorkspaceFullDocumentDiagnosticReport(
                        uri=uri,
                        items=merged,
                        version=None,
                        result_id=rid,
                    )
                )
                continue

            # Compute fresh (fallback for uncached files)
            filepath = uri_to_path(uri)
            try:
                doc = server.workspace.get_text_document(uri)
                source = doc.source or ""
            except KeyError:
                try:
                    with open(filepath, "r") as f:
                        source = f.read()
                except OSError:
                    continue

            loop = asyncio.get_running_loop()
            diags = await loop.run_in_executor(
                None,
                compute_diagnostics,
                server.parser,
                source,
                filepath,
                server.indexer,
                _get_semantic_model(),
                None,
            )
            rid = cache.update_fast(uri, source, diags)
            items.append(
                lsp.WorkspaceFullDocumentDiagnosticReport(
                    uri=uri,
                    items=diags,
                    version=None,
                    result_id=rid,
                )
            )

        return lsp.WorkspaceDiagnosticReport(items=items)
