"""Tests for LSP 3.17 Pull-Model Diagnostics.

Covers DiagnosticCache, textDocument/diagnostic handler,
workspace/diagnostic handler, handler registration options,
push-pull interaction, and edge cases.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.features.diagnostics import DiagnosticCache, register

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_diag(message: str, line: int = 0) -> lsp.Diagnostic:
    """Create a minimal LSP Diagnostic."""
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=line, character=0),
            end=lsp.Position(line=line, character=10),
        ),
        message=message,
        severity=lsp.DiagnosticSeverity.Error,
        source="test",
    )


def _make_server_mock(cache=None):
    """Return a MagicMock server with all required attributes for diagnostics."""
    server = MagicMock()
    server.parser = None
    server.indexer = None
    server.semantic_model = None
    server.analysis_pipeline = None
    server.diagnostic_cache = cache or DiagnosticCache()

    doc = MagicMock()
    doc.source = "#lang ivy1.7\ntype cid\n"
    doc.version = 1
    server.workspace.get_text_document.return_value = doc
    server.workspace.text_documents = {}
    return server


def _register_handlers(server):
    """Call register(server) and return (handlers, options) dicts."""
    handlers = {}
    options_map = {}

    def fake_feature(method, options=None):
        def decorator(fn):
            handlers[method] = fn
            if options is not None:
                options_map[method] = options
            return fn

        return decorator

    server.feature = fake_feature
    register(server)
    return handlers, options_map


# ===========================================================================
# 1. DiagnosticCache
# ===========================================================================


class TestDiagnosticCache:
    """Unit tests for DiagnosticCache in isolation."""

    # -- _hash ---------------------------------------------------------------

    def test_hash_deterministic(self):
        assert DiagnosticCache._hash("hello") == DiagnosticCache._hash("hello")

    def test_hash_different_for_different_sources(self):
        assert DiagnosticCache._hash("a") != DiagnosticCache._hash("b")

    def test_hash_is_16_chars(self):
        h = DiagnosticCache._hash("test")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    # -- _result_id ----------------------------------------------------------

    def test_result_id_fast_suffix(self):
        assert DiagnosticCache._result_id("abc", False) == "abc-fast"

    def test_result_id_deep_suffix(self):
        assert DiagnosticCache._result_id("abc", True) == "abc-deep"

    # -- update_fast ---------------------------------------------------------

    def test_update_fast_returns_result_id(self):
        cache = DiagnosticCache()
        rid = cache.update_fast("file:///a.ivy", "src", [_make_diag("x")])
        assert isinstance(rid, str)
        assert rid.endswith("-fast")

    def test_update_fast_stores_entry(self):
        cache = DiagnosticCache()
        diags = [_make_diag("x")]
        cache.update_fast("file:///a.ivy", "src", diags)
        entry = cache.get("file:///a.ivy")
        assert entry is not None
        assert entry.diagnostics == diags
        assert entry.deep_diagnostics is None

    def test_update_fast_preserves_deep_when_source_unchanged(self):
        cache = DiagnosticCache()
        source = "#lang ivy1.7\n"
        cache.update_fast("file:///a.ivy", source, [_make_diag("fast")])
        cache.update_deep("file:///a.ivy", [_make_diag("deep")])
        # Re-update fast with same source
        rid = cache.update_fast("file:///a.ivy", source, [_make_diag("fast2")])
        entry = cache.get("file:///a.ivy")
        assert entry.deep_diagnostics is not None
        assert rid.endswith("-deep")

    def test_update_fast_clears_deep_when_source_changed(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "v1", [_make_diag("fast")])
        cache.update_deep("file:///a.ivy", [_make_diag("deep")])
        # Update fast with different source
        rid = cache.update_fast("file:///a.ivy", "v2", [_make_diag("fast2")])
        entry = cache.get("file:///a.ivy")
        assert entry.deep_diagnostics is None
        assert rid.endswith("-fast")

    # -- update_deep ---------------------------------------------------------

    def test_update_deep_returns_result_id(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [])
        rid = cache.update_deep("file:///a.ivy", [_make_diag("deep")])
        assert isinstance(rid, str)
        assert rid.endswith("-deep")

    def test_update_deep_returns_none_for_missing_uri(self):
        cache = DiagnosticCache()
        assert cache.update_deep("file:///missing", []) is None

    def test_update_deep_overlays_deep_diagnostics(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [])
        deep = [_make_diag("deep")]
        cache.update_deep("file:///a.ivy", deep)
        entry = cache.get("file:///a.ivy")
        assert entry.deep_diagnostics == deep

    # -- get -----------------------------------------------------------------

    def test_get_returns_none_for_unknown_uri(self):
        cache = DiagnosticCache()
        assert cache.get("file:///unknown") is None

    # -- get_merged ----------------------------------------------------------

    def test_get_merged_returns_none_for_unknown_uri(self):
        cache = DiagnosticCache()
        assert cache.get_merged("file:///unknown") is None

    def test_get_merged_returns_fast_only_when_no_deep(self):
        cache = DiagnosticCache()
        diags = [_make_diag("a"), _make_diag("b")]
        cache.update_fast("file:///a.ivy", "src", diags)
        result = cache.get_merged("file:///a.ivy")
        assert result is not None
        rid, merged = result
        assert len(merged) == 2

    def test_get_merged_combines_fast_and_deep(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [_make_diag("f1"), _make_diag("f2")])
        cache.update_deep("file:///a.ivy", [_make_diag("d1")])
        result = cache.get_merged("file:///a.ivy")
        assert result is not None
        rid, merged = result
        assert len(merged) == 3
        assert rid.endswith("-deep")

    # -- invalidate ----------------------------------------------------------

    def test_invalidate_removes_entry(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [])
        cache.invalidate("file:///a.ivy")
        assert cache.get("file:///a.ivy") is None

    def test_invalidate_noop_for_unknown_uri(self):
        cache = DiagnosticCache()
        cache.invalidate("file:///unknown")  # should not raise

    # -- all_uris ------------------------------------------------------------

    def test_all_uris_empty_cache(self):
        cache = DiagnosticCache()
        assert cache.all_uris() == []

    def test_all_uris_returns_all_cached(self):
        cache = DiagnosticCache()
        for uri in ["file:///a", "file:///b", "file:///c"]:
            cache.update_fast(uri, "src", [])
        assert set(cache.all_uris()) == {"file:///a", "file:///b", "file:///c"}

    # -- thread safety -------------------------------------------------------

    def test_thread_safety_concurrent_updates(self):
        cache = DiagnosticCache()
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    uri = f"file:///thread{n}_{i}.ivy"
                    cache.update_fast(uri, f"src{n}_{i}", [_make_diag(f"d{n}_{i}")])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(cache.all_uris()) == 200  # 4 threads * 50 URIs


# ===========================================================================
# 2. textDocument/diagnostic handler
# ===========================================================================


class TestTextDocumentDiagnosticHandler:
    """Tests for the textDocument/diagnostic pull handler."""

    def test_handler_registered_with_correct_method(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        assert lsp.TEXT_DOCUMENT_DIAGNOSTIC in handlers

    def test_handler_is_async(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]
        assert asyncio.iscoroutinefunction(handler)

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_previous_result_id_matches(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        uri = "file:///test.ivy"
        rid = cache.update_fast(uri, "#lang ivy1.7\n", [_make_diag("x")])

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            previous_result_id=rid,
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedUnchangedDocumentDiagnosticReport)
        assert result.result_id == rid

    @pytest.mark.asyncio
    async def test_returns_full_when_previous_result_id_mismatches(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        uri = "file:///test.ivy"
        cache.update_fast(uri, "#lang ivy1.7\n", [])

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            previous_result_id="stale-id",
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)

    @pytest.mark.asyncio
    async def test_returns_full_when_no_previous_result_id(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.ivy"),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)

    @pytest.mark.asyncio
    async def test_full_report_includes_result_id(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.ivy"),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)
        assert result.result_id is not None
        assert len(result.result_id) > 0

    @pytest.mark.asyncio
    async def test_merges_deep_diagnostics_if_available(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        uri = "file:///test.ivy"
        source = "#lang ivy1.7\ntype cid\n"
        server.workspace.get_text_document.return_value.source = source
        # Pre-populate cache with deep diagnostics
        cache.update_fast(uri, source, [])
        cache.update_deep(uri, [_make_diag("deep-error")])

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)
        messages = [d.message for d in result.items]
        assert "deep-error" in messages
        assert result.result_id.endswith("-deep")

    @pytest.mark.asyncio
    async def test_falls_back_to_disk_when_document_not_open(self, tmp_path):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        # Write a real file
        ivy_file = tmp_path / "disk.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype cid\n")
        uri = f"file://{ivy_file}"

        server.workspace.get_text_document.side_effect = KeyError("not open")

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)

    @pytest.mark.asyncio
    async def test_returns_empty_report_when_disk_read_fails(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        server.workspace.get_text_document.side_effect = KeyError("not open")
        uri = "file:///nonexistent/path/test.ivy"

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)
        assert result.items == []
        assert result.result_id == "empty"

    @pytest.mark.asyncio
    async def test_updates_cache_on_fresh_computation(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        uri = "file:///fresh.ivy"
        assert cache.get(uri) is None

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
        )
        await handler(params)
        assert cache.get(uri) is not None


# ===========================================================================
# 3. workspace/diagnostic handler
# ===========================================================================


class TestWorkspaceDiagnosticHandler:
    """Tests for the workspace/diagnostic pull handler."""

    def test_handler_registered_with_correct_method(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        assert lsp.WORKSPACE_DIAGNOSTIC in handlers

    def test_handler_is_async(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]
        assert asyncio.iscoroutinefunction(handler)

    @pytest.mark.asyncio
    async def test_returns_workspace_diagnostic_report(self):
        server = _make_server_mock()
        server.workspace.text_documents = {}
        server.indexer = None
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert isinstance(result, lsp.WorkspaceDiagnosticReport)

    @pytest.mark.asyncio
    async def test_unchanged_report_for_matching_previous_result_id(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        uri = "file:///a.ivy"
        rid = cache.update_fast(uri, "#lang ivy1.7\n", [_make_diag("x")])
        server.workspace.text_documents = {uri: MagicMock()}
        server.indexer = None

        params = lsp.WorkspaceDiagnosticParams(
            previous_result_ids=[
                lsp.PreviousResultId(uri=uri, value=rid),
            ],
        )
        result = await handler(params)
        assert len(result.items) == 1
        item = result.items[0]
        assert isinstance(item, lsp.WorkspaceUnchangedDocumentDiagnosticReport)
        assert item.result_id == rid

    @pytest.mark.asyncio
    async def test_full_report_for_mismatched_previous_result_id(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        uri = "file:///a.ivy"
        cache.update_fast(uri, "#lang ivy1.7\n", [_make_diag("x")])
        server.workspace.text_documents = {uri: MagicMock()}
        server.indexer = None

        params = lsp.WorkspaceDiagnosticParams(
            previous_result_ids=[
                lsp.PreviousResultId(uri=uri, value="wrong-id"),
            ],
        )
        result = await handler(params)
        assert len(result.items) == 1
        item = result.items[0]
        assert isinstance(item, lsp.WorkspaceFullDocumentDiagnosticReport)

    @pytest.mark.asyncio
    async def test_returns_cached_merged_when_available(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        uri = "file:///a.ivy"
        cache.update_fast(uri, "#lang ivy1.7\n", [_make_diag("fast")])
        cache.update_deep(uri, [_make_diag("deep")])
        server.workspace.text_documents = {uri: MagicMock()}
        server.indexer = None

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert len(result.items) == 1
        item = result.items[0]
        assert isinstance(item, lsp.WorkspaceFullDocumentDiagnosticReport)
        assert len(item.items) == 2  # fast + deep

    @pytest.mark.asyncio
    async def test_computes_fresh_for_uncached_files(self, tmp_path):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        ivy_file = tmp_path / "fresh.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")
        uri = f"file://{ivy_file}"

        server.workspace.text_documents = {}
        server.workspace.get_text_document.side_effect = KeyError("not open")
        indexer = MagicMock()
        indexer.get_all_ivy_file_paths.return_value = [str(ivy_file)]
        server.indexer = indexer

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert len(result.items) == 1
        item = result.items[0]
        assert isinstance(item, lsp.WorkspaceFullDocumentDiagnosticReport)
        # Cache should now have the entry
        assert cache.get(uri) is not None

    @pytest.mark.asyncio
    async def test_collects_uris_from_open_docs_and_indexer(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        uri_open = "file:///open.ivy"
        uri_indexed = "file:///indexed.ivy"
        cache.update_fast(uri_open, "src1", [])
        cache.update_fast(uri_indexed, "src2", [])

        server.workspace.text_documents = {uri_open: MagicMock()}
        indexer = MagicMock()
        indexer.get_all_ivy_file_paths.return_value = ["/indexed.ivy"]
        server.indexer = indexer

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert len(result.items) == 2

    @pytest.mark.asyncio
    async def test_skips_files_with_oserror(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        server.workspace.text_documents = {}
        server.workspace.get_text_document.side_effect = KeyError("not open")
        indexer = MagicMock()
        indexer.get_all_ivy_file_paths.return_value = ["/nonexistent/path.ivy"]
        server.indexer = indexer

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_no_indexer_only_open_docs(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        uri = "file:///a.ivy"
        cache.update_fast(uri, "src", [_make_diag("x")])
        server.workspace.text_documents = {uri: MagicMock()}
        server.indexer = None

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_empty_workspace_returns_empty_items(self):
        server = _make_server_mock()
        server.workspace.text_documents = {}
        server.indexer = None
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.WORKSPACE_DIAGNOSTIC]

        params = lsp.WorkspaceDiagnosticParams(previous_result_ids=[])
        result = await handler(params)
        assert result.items == []


# ===========================================================================
# 4. Handler registration options
# ===========================================================================


class TestHandlerRegistrationOptions:
    """Verify registration options match LSP 3.17 spec."""

    def test_text_document_diagnostic_options(self):
        server = _make_server_mock()
        _, options_map = _register_handlers(server)
        opts = options_map.get(lsp.TEXT_DOCUMENT_DIAGNOSTIC)
        assert opts is not None
        assert isinstance(opts, lsp.DiagnosticOptions)
        assert opts.identifier == "ivy"
        assert opts.inter_file_dependencies is True
        assert opts.workspace_diagnostics is True

    def test_workspace_diagnostic_registered(self):
        server = _make_server_mock()
        handlers, _ = _register_handlers(server)
        assert lsp.WORKSPACE_DIAGNOSTIC in handlers


# ===========================================================================
# 5. Push-pull interaction
# ===========================================================================


class TestPushPullInteraction:
    """Verify push handlers populate the cache that pull handlers read."""

    @pytest.mark.asyncio
    async def test_did_open_populates_cache(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)
        did_open = handlers[lsp.TEXT_DOCUMENT_DID_OPEN]

        uri = "file:///test.ivy"
        params = MagicMock()
        params.text_document.uri = uri
        await did_open(params)

        assert cache.get(uri) is not None

    @pytest.mark.asyncio
    async def test_did_open_then_pull_returns_unchanged(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)

        uri = "file:///test.ivy"
        # Trigger did_open to populate cache
        open_params = MagicMock()
        open_params.text_document.uri = uri
        await handlers[lsp.TEXT_DOCUMENT_DID_OPEN](open_params)

        # Get result_id from cache
        entry = cache.get(uri)
        assert entry is not None

        # Pull with matching result_id
        pull_params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            previous_result_id=entry.result_id,
        )
        result = await handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC](pull_params)
        assert isinstance(result, lsp.RelatedUnchangedDocumentDiagnosticReport)

    def test_did_close_invalidates_cache(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)

        uri = "file:///test.ivy"
        cache.update_fast(uri, "src", [])
        assert cache.get(uri) is not None

        params = MagicMock()
        params.text_document.uri = uri
        handlers[lsp.TEXT_DOCUMENT_DID_CLOSE](params)

        assert cache.get(uri) is None

    @pytest.mark.asyncio
    async def test_did_close_then_pull_returns_fresh(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)

        uri = "file:///test.ivy"
        old_rid = cache.update_fast(uri, "src", [])

        # Close invalidates cache
        close_params = MagicMock()
        close_params.text_document.uri = uri
        handlers[lsp.TEXT_DOCUMENT_DID_CLOSE](close_params)

        # Pull with old result_id should get fresh (not unchanged)
        pull_params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            previous_result_id=old_rid,
        )
        result = await handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC](pull_params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)

    def test_cache_result_id_changes_after_source_update(self):
        cache = DiagnosticCache()
        rid1 = cache.update_fast("file:///a.ivy", "v1", [])
        rid2 = cache.update_fast("file:///a.ivy", "v2", [])
        assert rid1 != rid2

    def test_deep_update_changes_result_id(self):
        cache = DiagnosticCache()
        rid1 = cache.update_fast("file:///a.ivy", "src", [])
        assert rid1.endswith("-fast")
        rid2 = cache.update_deep("file:///a.ivy", [_make_diag("deep")])
        assert rid2.endswith("-deep")
        assert rid1 != rid2

    @pytest.mark.asyncio
    async def test_pull_after_deep_update_returns_full(self):
        cache = DiagnosticCache()
        server = _make_server_mock(cache=cache)
        handlers, _ = _register_handlers(server)

        uri = "file:///test.ivy"
        source = "#lang ivy1.7\ntype cid\n"
        server.workspace.get_text_document.return_value.source = source
        fast_rid = cache.update_fast(uri, source, [])
        cache.update_deep(uri, [_make_diag("deep")])

        # Pull with old fast result_id should get full (result_id changed)
        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            previous_result_id=fast_rid,
        )
        result = await handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC](params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)


# ===========================================================================
# 6. Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge cases, error handling, and boundary conditions."""

    def test_empty_source_produces_result_id(self):
        cache = DiagnosticCache()
        rid = cache.update_fast("file:///empty.ivy", "", [])
        assert isinstance(rid, str)
        assert len(rid) > 0

    def test_unicode_source_hashes_correctly(self):
        h = DiagnosticCache._hash("type cid\n# \u00e9\u00e0\u00fc\n")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_multiple_uris_independent(self):
        cache = DiagnosticCache()
        diags_a = [_make_diag("a")]
        diags_b = [_make_diag("b")]
        cache.update_fast("file:///a.ivy", "src_a", diags_a)
        cache.update_fast("file:///b.ivy", "src_b", diags_b)
        assert cache.get("file:///a.ivy").diagnostics[0].message == "a"
        assert cache.get("file:///b.ivy").diagnostics[0].message == "b"

    def test_invalidate_one_uri_preserves_others(self):
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [])
        cache.update_fast("file:///b.ivy", "src", [])
        cache.invalidate("file:///a.ivy")
        assert cache.get("file:///a.ivy") is None
        assert cache.get("file:///b.ivy") is not None

    def test_update_deep_without_prior_fast_returns_none(self):
        cache = DiagnosticCache()
        assert cache.update_deep("file:///new.ivy", [_make_diag("d")]) is None

    def test_empty_deep_list_behavior(self):
        """Empty deep list is falsy, so get_merged won't extend merged list.

        The result_id still says '-deep' even though no deep diagnostics are present.
        """
        cache = DiagnosticCache()
        cache.update_fast("file:///a.ivy", "src", [_make_diag("fast")])
        cache.update_deep("file:///a.ivy", [])
        entry = cache.get("file:///a.ivy")
        assert entry.result_id.endswith("-deep")
        result = cache.get_merged("file:///a.ivy")
        _, merged = result
        assert len(merged) == 1  # Only fast, empty deep not extended

    def test_concurrent_update_fast_and_get_merged(self):
        cache = DiagnosticCache()
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.update_fast(
                        "file:///shared.ivy", f"src{i}", [_make_diag(f"d{i}")]
                    )
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    result = cache.get_merged("file:///shared.ivy")
                    if result is not None:
                        rid, diags = result
                        assert isinstance(diags, list)
            except Exception as e:
                errors.append(e)

        t_write = threading.Thread(target=writer)
        t_read = threading.Thread(target=reader)
        t_write.start()
        t_read.start()
        t_write.join()
        t_read.join()
        assert errors == []

    @pytest.mark.asyncio
    async def test_pull_handler_with_parser_none_and_indexer_none(self):
        server = _make_server_mock()
        server.parser = None
        server.indexer = None
        handlers, _ = _register_handlers(server)
        handler = handlers[lsp.TEXT_DOCUMENT_DIAGNOSTIC]

        params = lsp.DocumentDiagnosticParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.ivy"),
        )
        result = await handler(params)
        assert isinstance(result, lsp.RelatedFullDocumentDiagnosticReport)
        assert isinstance(result.items, list)
