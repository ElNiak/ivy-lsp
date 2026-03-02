"""Tests for Task 3.2: Diagnostics Feature."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import DiagnosticSeverity

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestDiagnosticsImport:
    def test_import(self):
        from ivy_lsp.features.diagnostics import (
            check_structural_issues,
            compute_diagnostics,
        )

        assert compute_diagnostics is not None
        assert check_structural_issues is not None


class TestParseDiagnostics:
    def test_valid_file_no_diagnostics(self, tmp_path):
        from ivy_lsp.features.diagnostics import compute_diagnostics
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = "#lang ivy1.7\n\ntype cid\n"
        diags = compute_diagnostics(parser, source, str(tmp_path / "a.ivy"))
        errors = [d for d in diags if d.severity == DiagnosticSeverity.Error]
        assert len(errors) == 0

    def test_syntax_error_produces_diagnostic(self):
        from ivy_lsp.features.diagnostics import compute_diagnostics
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = "#lang ivy1.7\n\ntype cid\nobject broken = {\n    this is not valid !!!\n}\n"
        diags = compute_diagnostics(parser, source, "broken.ivy")
        errors = [d for d in diags if d.severity == DiagnosticSeverity.Error]
        assert len(errors) > 0

    def test_diagnostic_has_message(self):
        from ivy_lsp.features.diagnostics import compute_diagnostics
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = "#lang ivy1.7\nobject x = { @@@ }\n"
        diags = compute_diagnostics(parser, source, "bad.ivy")
        errors = [d for d in diags if d.severity == DiagnosticSeverity.Error]
        if errors:
            assert errors[0].message  # non-empty message


class TestStructuralDiagnostics:
    def test_missing_lang_header(self):
        from ivy_lsp.features.diagnostics import check_structural_issues

        diags = check_structural_issues("type cid\n", "a.ivy")
        assert any(
            "lang" in d.message.lower() or "header" in d.message.lower() for d in diags
        )

    def test_valid_header_no_warning(self):
        from ivy_lsp.features.diagnostics import check_structural_issues

        diags = check_structural_issues("#lang ivy1.7\ntype cid\n", "a.ivy")
        lang_warnings = [
            d
            for d in diags
            if "lang" in d.message.lower() or "header" in d.message.lower()
        ]
        assert len(lang_warnings) == 0

    def test_unmatched_open_brace(self):
        from ivy_lsp.features.diagnostics import check_structural_issues

        diags = check_structural_issues("#lang ivy1.7\nobject x = {\ntype t\n", "a.ivy")
        brace_diags = [d for d in diags if "brace" in d.message.lower()]
        assert len(brace_diags) > 0

    def test_unmatched_close_brace(self):
        from ivy_lsp.features.diagnostics import check_structural_issues

        diags = check_structural_issues("#lang ivy1.7\n}\n", "a.ivy")
        brace_diags = [d for d in diags if "brace" in d.message.lower()]
        assert len(brace_diags) > 0

    def test_balanced_braces_no_diagnostic(self):
        from ivy_lsp.features.diagnostics import check_structural_issues

        diags = check_structural_issues(
            "#lang ivy1.7\nobject x = {\ntype t\n}\n", "a.ivy"
        )
        brace_diags = [d for d in diags if "brace" in d.message.lower()]
        assert len(brace_diags) == 0

    def test_unresolved_include(self, tmp_path):
        from ivy_lsp.features.diagnostics import check_structural_issues
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        (tmp_path / "a.ivy").write_text("#lang ivy1.7\ninclude nonexistent\n")
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        diags = check_structural_issues(
            "#lang ivy1.7\ninclude nonexistent\n",
            str(tmp_path / "a.ivy"),
            indexer=indexer,
        )
        include_diags = [d for d in diags if "include" in d.message.lower()]
        assert len(include_diags) > 0

    def test_resolved_include_no_diagnostic(self, tmp_path):
        from ivy_lsp.features.diagnostics import check_structural_issues
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype t\n")
        (tmp_path / "main.ivy").write_text("#lang ivy1.7\ninclude types\n")
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        diags = check_structural_issues(
            "#lang ivy1.7\ninclude types\n",
            str(tmp_path / "main.ivy"),
            indexer=indexer,
        )
        include_diags = [d for d in diags if "unresolved" in d.message.lower()]
        assert len(include_diags) == 0


class TestFallbackScannerDiagnostics:
    """Tests for surfacing fallback scanner lexer errors as diagnostics."""

    def test_lexer_error_produces_diagnostic(self):
        """A file with illegal characters should produce an error diagnostic
        when the parser fails and the fallback scanner encounters the error."""
        from ivy_lsp.features.diagnostics import compute_diagnostics
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        # Smart quote (U+2019) is an illegal character for the Ivy lexer
        source = "#lang ivy1.7\n\ntype cid\naction foo\u2019s_thing\n"
        diags = compute_diagnostics(parser, source, "smart_quote.ivy")
        lexer_errors = [
            d
            for d in diags
            if d.source == "ivy-lsp" and "Lexer error" in d.message
        ]
        assert len(lexer_errors) > 0, (
            "Expected a diagnostic for the illegal smart quote character"
        )
        assert lexer_errors[0].severity == DiagnosticSeverity.Error


class TestComputeDiagnostics:
    def test_full_pipeline_valid(self):
        from ivy_lsp.features.diagnostics import compute_diagnostics
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        diags = compute_diagnostics(parser, "#lang ivy1.7\ntype cid\n", "a.ivy")
        errors = [d for d in diags if d.severity == DiagnosticSeverity.Error]
        assert len(errors) == 0

    def test_no_parser_graceful(self):
        from ivy_lsp.features.diagnostics import compute_diagnostics

        diags = compute_diagnostics(None, "#lang ivy1.7\ntype cid\n", "a.ivy")
        # Should not crash; structural-only diagnostics
        assert isinstance(diags, list)


class TestDidCloseHandler:
    """C1: didClose must clear diagnostics and cancel pending debounce tasks."""

    def test_register_installs_did_close_handler(self):
        """The register function should install a TEXT_DOCUMENT_DID_CLOSE handler."""
        from unittest.mock import MagicMock

        from lsprotocol import types as lsp

        from ivy_lsp.features.diagnostics import register

        server = MagicMock()
        # Collect registered features
        registered = {}

        def fake_feature(method):
            def decorator(fn):
                registered[method] = fn
                return fn

            return decorator

        server.feature = fake_feature

        register(server)
        assert lsp.TEXT_DOCUMENT_DID_CLOSE in registered, (
            "register() must install a textDocument/didClose handler"
        )

    def test_did_close_publishes_empty_diagnostics(self):
        """didClose should publish an empty diagnostics list to clear stale entries."""
        from unittest.mock import MagicMock

        from lsprotocol import types as lsp

        from ivy_lsp.features.diagnostics import register

        server = MagicMock()
        handlers = {}

        def fake_feature(method):
            def decorator(fn):
                handlers[method] = fn
                return fn

            return decorator

        server.feature = fake_feature

        register(server)

        handler = handlers[lsp.TEXT_DOCUMENT_DID_CLOSE]
        params = MagicMock()
        params.text_document.uri = "file:///tmp/test.ivy"
        handler(params)

        server.text_document_publish_diagnostics.assert_called_once()
        published = server.text_document_publish_diagnostics.call_args[0][0]
        assert published.uri == "file:///tmp/test.ivy"
        assert published.diagnostics == []

    def test_did_close_cancels_debounce_task(self):
        """didClose should cancel any pending debounce task for the closed URI."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from lsprotocol import types as lsp

        from ivy_lsp.features.diagnostics import register

        server = MagicMock()
        handlers = {}

        def fake_feature(method):
            def decorator(fn):
                handlers[method] = fn
                return fn

            return decorator

        server.feature = fake_feature

        register(server)

        # Simulate a pending debounce task by calling did_change first
        # We need to access the closure's _debounce_tasks dict.
        # Instead, we inject a mock task directly via the did_change handler.
        did_change = handlers[lsp.TEXT_DOCUMENT_DID_CHANGE]
        did_close = handlers[lsp.TEXT_DOCUMENT_DID_CLOSE]

        # Create a mock task and inject it into _debounce_tasks via closure
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False

        # We can access _debounce_tasks indirectly: the did_close handler
        # pops from _debounce_tasks. We inject by using the fact that
        # did_change stores tasks there. Instead, let's monkey-patch:
        # Access the closure variables through the handler's __code__.
        # Simpler approach: just put a task in via the closure.
        # The did_close handler references _debounce_tasks from the enclosing
        # register() scope. We can access it via __closure__.

        # Find _debounce_tasks in the closure of did_close
        _debounce_tasks = None
        for cell in did_close.__code__.co_freevars:
            pass  # just checking names
        # The closure cells are ordered by co_freevars
        freevars = did_close.__code__.co_freevars
        for i, name in enumerate(freevars):
            if name == "_debounce_tasks":
                _debounce_tasks = did_close.__closure__[i].cell_contents
                break

        assert _debounce_tasks is not None, (
            "Could not find _debounce_tasks in did_close closure"
        )

        # Inject a mock pending task
        uri = "file:///tmp/test.ivy"
        _debounce_tasks[uri] = mock_task

        # Now call did_close
        params = MagicMock()
        params.text_document.uri = uri
        did_close(params)

        # The debounce task should have been cancelled
        mock_task.cancel.assert_called_once()
        # And removed from the dict
        assert uri not in _debounce_tasks


class TestDeepDiagnostics:
    @pytest.mark.asyncio
    async def test_missing_ivyc_handled(self):
        from ivy_lsp.features.diagnostics import run_deep_diagnostics

        result = await run_deep_diagnostics(
            "nonexistent.ivy", ivy_check_cmd="nonexistent_binary_12345"
        )
        assert result == []
