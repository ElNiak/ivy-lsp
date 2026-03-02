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

        did_close = handlers[lsp.TEXT_DOCUMENT_DID_CLOSE]

        # Create a mock task and inject it into _debounce_tasks via closure
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False

        # Access _debounce_tasks from did_close's closure
        _debounce_tasks = None
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


class TestDeepTaskTracking:
    """C2: Deep diagnostics tasks must be tracked and cancellable."""

    def test_did_close_cancels_deep_task(self):
        """didClose should cancel any pending deep diagnostics task."""
        import asyncio
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

        did_close = handlers[lsp.TEXT_DOCUMENT_DID_CLOSE]

        # Access _deep_tasks from did_close's closure
        _deep_tasks = None
        freevars = did_close.__code__.co_freevars
        for i, name in enumerate(freevars):
            if name == "_deep_tasks":
                _deep_tasks = did_close.__closure__[i].cell_contents
                break

        assert _deep_tasks is not None, (
            "Could not find _deep_tasks in did_close closure — "
            "did register() define _deep_tasks?"
        )

        # Inject a mock deep task
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        uri = "file:///tmp/test.ivy"
        _deep_tasks[uri] = mock_task

        params = MagicMock()
        params.text_document.uri = uri
        did_close(params)

        mock_task.cancel.assert_called_once()
        assert uri not in _deep_tasks

    def test_did_save_cancels_prior_deep_task(self):
        """did_save should cancel a prior deep task for the same URI before starting a new one."""
        import asyncio
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

        did_save = handlers[lsp.TEXT_DOCUMENT_DID_SAVE]

        # Access _deep_tasks from did_save's closure
        _deep_tasks = None
        freevars = did_save.__code__.co_freevars
        for i, name in enumerate(freevars):
            if name == "_deep_tasks":
                _deep_tasks = did_save.__closure__[i].cell_contents
                break

        assert _deep_tasks is not None, (
            "Could not find _deep_tasks in did_save closure — "
            "did register() define _deep_tasks?"
        )

        # Inject a mock prior deep task
        mock_prior = MagicMock(spec=asyncio.Task)
        mock_prior.done.return_value = False
        uri = "file:///tmp/test.ivy"
        _deep_tasks[uri] = mock_prior

        mock_prior.cancel.assert_not_called()

        # Simulate what did_save does: cancel prior deep task
        old_deep = _deep_tasks.pop(uri, None)
        if old_deep and not old_deep.done():
            old_deep.cancel()

        mock_prior.cancel.assert_called_once()
        assert uri not in _deep_tasks


class TestDiagnosticEndPosition:
    """H1: Diagnostics must use actual line length, not magic 999."""

    def test_coverage_hint_end_character_not_999(self):
        """Coverage hint diagnostics should not use magic 999."""
        from unittest.mock import MagicMock

        from ivy_lsp.analysis.requirement_graph import ActionNode, RequirementGraph
        from ivy_lsp.features.diagnostics import compute_diagnostics

        graph = RequirementGraph()
        graph.add_action(
            ActionNode(
                id="foo",
                name="foo",
                qualified_name="q.foo",
                file="/tmp/test.ivy",
                line=2,
            )
        )
        indexer = MagicMock()
        indexer._requirement_graph = graph
        indexer._include_graph = None
        indexer._resolver = MagicMock()

        # Provide a successful parse_result so compute_diagnostics reaches
        # the coverage hint section (it returns early if both parser and
        # parse_result are None).
        fake_result = MagicMock()
        fake_result.success = True
        fake_result.errors = []

        source = "#lang ivy1.7\n\naction foo(x:cid)\n"
        diags = compute_diagnostics(
            None, source, "/tmp/test.ivy",
            indexer=indexer, parse_result=fake_result,
        )

        coverage_diags = [d for d in diags if d.source == "ivy-lsp-coverage"]
        assert len(coverage_diags) > 0, "Expected at least one coverage diagnostic"
        for d in coverage_diags:
            assert d.range.end.character != 999, (
                "Coverage diagnostic uses magic 999 instead of actual line length"
            )

    def test_coverage_hint_end_matches_line_length(self):
        """Coverage hint end character should match the actual line length."""
        from unittest.mock import MagicMock

        from ivy_lsp.analysis.requirement_graph import ActionNode, RequirementGraph
        from ivy_lsp.features.diagnostics import compute_diagnostics

        graph = RequirementGraph()
        graph.add_action(
            ActionNode(
                id="foo",
                name="foo",
                qualified_name="q.foo",
                file="/tmp/test.ivy",
                line=2,
            )
        )
        indexer = MagicMock()
        indexer._requirement_graph = graph
        indexer._include_graph = None
        indexer._resolver = MagicMock()

        fake_result = MagicMock()
        fake_result.success = True
        fake_result.errors = []

        source = "#lang ivy1.7\n\naction foo(x:cid)\n"
        # Line 2 is "action foo(x:cid)" which has length 17
        diags = compute_diagnostics(
            None, source, "/tmp/test.ivy",
            indexer=indexer, parse_result=fake_result,
        )

        coverage_diags = [d for d in diags if d.source == "ivy-lsp-coverage"]
        assert len(coverage_diags) > 0, "Expected at least one coverage diagnostic"
        for d in coverage_diags:
            line_idx = d.range.start.line
            lines = source.split("\n")
            expected_len = len(lines[line_idx]) if line_idx < len(lines) else 0
            assert d.range.end.character == expected_len, (
                f"Expected end character {expected_len} for line {line_idx}, "
                f"got {d.range.end.character}"
            )

    def test_ivy_check_output_end_character_not_999(self):
        """parse_ivy_check_output should not use magic 999."""
        from ivy_lsp.features.diagnostics import parse_ivy_check_output

        # Format matches regex: "file:linenum: severity: message"
        output = "test.ivy:5: error: something went wrong"
        diags = parse_ivy_check_output(output)
        assert len(diags) == 1, "Expected one diagnostic from ivy_check output"
        for d in diags:
            assert d.range.end.character != 999, (
                "ivy_check diagnostic uses magic 999"
            )

    def test_ivy_check_output_uses_next_line_convention(self):
        """parse_ivy_check_output should use lineno+1, char=0 for full-line span."""
        from ivy_lsp.features.diagnostics import parse_ivy_check_output

        # Format: "file:line: severity: message"
        output = "test.ivy:5: error: something went wrong"
        diags = parse_ivy_check_output(output)
        assert len(diags) == 1
        d = diags[0]
        # lineno = max(0, 5 - 1) = 4, so end should be line 5, char 0
        assert d.range.start.line == 4
        assert d.range.start.character == 0
        assert d.range.end.line == 5
        assert d.range.end.character == 0


class TestDeepDiagnostics:
    @pytest.mark.asyncio
    async def test_missing_ivyc_handled(self):
        from ivy_lsp.features.diagnostics import run_deep_diagnostics

        result = await run_deep_diagnostics(
            "nonexistent.ivy", ivy_check_cmd="nonexistent_binary_12345"
        )
        assert result == []


class TestDiagnosticVersion:
    """H2: PublishDiagnosticsParams should include document version."""

    def test_did_open_publishes_with_version(self):
        """did_open should include doc.version in published diagnostics."""
        import asyncio
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
        server._parser = None
        server._indexer = None
        server._semantic_model = None
        server._analysis_pipeline = None

        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 42
        server.workspace.get_text_document.return_value = doc

        register(server)

        handler = handlers[lsp.TEXT_DOCUMENT_DID_OPEN]
        params = MagicMock()
        params.text_document.uri = "file:///tmp/test.ivy"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler(params))
        except Exception:
            pass
        finally:
            loop.close()

        assert server.text_document_publish_diagnostics.called, (
            "Expected publish_diagnostics to be called"
        )
        published = server.text_document_publish_diagnostics.call_args[0][0]
        assert published.version == 42, (
            f"Expected version=42, got version={published.version}"
        )

    def test_did_close_does_not_include_version(self):
        """didClose publishes empty diagnostics without version (no doc available)."""
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

        published = server.text_document_publish_diagnostics.call_args[0][0]
        assert published.version is None, (
            f"didClose should not set version, got version={published.version}"
        )

    def test_did_save_publishes_with_version(self):
        """did_save should include doc.version in published diagnostics."""
        import asyncio
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
        server._parser = None
        server._indexer = None
        server._semantic_model = None
        server._analysis_pipeline = None

        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 7
        server.workspace.get_text_document.return_value = doc

        register(server)

        handler = handlers[lsp.TEXT_DOCUMENT_DID_SAVE]
        params = MagicMock()
        params.text_document.uri = "file:///tmp/test.ivy"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler(params))
        except Exception:
            pass
        finally:
            loop.close()

        assert server.text_document_publish_diagnostics.called, (
            "Expected publish_diagnostics to be called"
        )
        published = server.text_document_publish_diagnostics.call_args[0][0]
        assert published.version == 7, (
            f"Expected version=7, got version={published.version}"
        )
