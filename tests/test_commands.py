"""Tests for LSP custom command handlers (ivy/verify, ivy/compile, etc.)."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope
from ivy_lsp.lsp.commands import (
    _detect_isolate_at_position,
    _find_enclosing_test,
    _find_tool,
    _run_tool,
    _validate_ivy_param,
)
from ivy_lsp.lsp.diagnostics.publisher import parse_ivy_check_output

# ---------------------------------------------------------------------------
# parse_ivy_check_output
# ---------------------------------------------------------------------------


class TestParseIvyCheckOutput:
    def test_errors(self):
        output = "file.ivy:10: error: type mismatch"
        diags = parse_ivy_check_output(output)
        assert len(diags) == 1
        assert diags[0].severity == lsp.DiagnosticSeverity.Error
        assert diags[0].range.start.line == 9  # 0-indexed
        assert "type mismatch" in diags[0].message

    def test_warnings(self):
        output = "file.ivy:5: warning: unused variable"
        diags = parse_ivy_check_output(output)
        assert len(diags) == 1
        assert diags[0].severity == lsp.DiagnosticSeverity.Warning
        assert diags[0].range.start.line == 4

    def test_empty(self):
        assert parse_ivy_check_output("") == []

    def test_mixed_output(self):
        output = (
            "some info line\n"
            "file.ivy:1: error: first\n"
            "file.ivy:2: warning: second\n"
            "another info line\n"
        )
        diags = parse_ivy_check_output(output)
        assert len(diags) == 2
        assert diags[0].severity == lsp.DiagnosticSeverity.Error
        assert diags[1].severity == lsp.DiagnosticSeverity.Warning

    def test_line_zero_clamped(self):
        output = "file.ivy:0: error: at line zero"
        diags = parse_ivy_check_output(output)
        assert len(diags) == 1
        assert diags[0].range.start.line == 0  # max(0, 0-1) = 0


# ---------------------------------------------------------------------------
# _find_tool
# ---------------------------------------------------------------------------


class TestFindTool:
    def test_existing_tool(self):
        # python should always be on PATH in test envs
        assert _find_tool("python3") is not None or _find_tool("python") is not None

    def test_nonexistent_tool(self):
        assert _find_tool("definitely_nonexistent_tool_xyz") is None


# ---------------------------------------------------------------------------
# _detect_isolate_at_position
# ---------------------------------------------------------------------------


class TestDetectIsolateAtPosition:
    def _make_server(self, symbols):
        server = MagicMock()
        doc = MagicMock()
        doc.source = "#lang ivy1.7\nisolate test_iso = {\n  type t\n}\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()
        server.indexer = MagicMock()
        return server

    def test_none_position_returns_none(self):
        server = self._make_server([])
        result = _detect_isolate_at_position(server, "file:///a.ivy", None)
        assert result is None

    @patch("ivy_lsp.lsp.document_symbols.compute_document_symbols")
    def test_inside_isolate(self, mock_compute):
        ns_symbol = lsp.DocumentSymbol(
            name="test_iso",
            kind=lsp.SymbolKind.Namespace,
            range=lsp.Range(start=lsp.Position(1, 0), end=lsp.Position(3, 1)),
            selection_range=lsp.Range(start=lsp.Position(1, 0), end=lsp.Position(1, 8)),
            children=None,
        )
        mock_compute.return_value = [ns_symbol]
        server = self._make_server([ns_symbol])
        result = _detect_isolate_at_position(
            server, "file:///a.ivy", lsp.Position(line=2, character=0)
        )
        assert result == "test_iso"

    @patch("ivy_lsp.lsp.document_symbols.compute_document_symbols")
    def test_outside_isolate(self, mock_compute):
        ns_symbol = lsp.DocumentSymbol(
            name="test_iso",
            kind=lsp.SymbolKind.Namespace,
            range=lsp.Range(start=lsp.Position(1, 0), end=lsp.Position(3, 1)),
            selection_range=lsp.Range(start=lsp.Position(1, 0), end=lsp.Position(1, 8)),
            children=None,
        )
        mock_compute.return_value = [ns_symbol]
        server = self._make_server([ns_symbol])
        result = _detect_isolate_at_position(
            server, "file:///a.ivy", lsp.Position(line=0, character=0)
        )
        assert result is None


# ---------------------------------------------------------------------------
# _run_tool
# ---------------------------------------------------------------------------


class TestRunTool:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        server = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_tool(["ivy_check", "f.ivy"], 10.0, server)

        assert result["success"] is True
        assert result["message"] == "OK"
        assert "ok" in result["output"]

    @pytest.mark.asyncio
    async def test_failure_exit_code(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"file.ivy:1: error: bad\n")
        mock_proc.returncode = 1

        server = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_tool(["ivy_check", "f.ivy"], 10.0, server)

        assert result["success"] is False
        assert "Exit code 1" in result["message"]

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        server = MagicMock()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("not found"),
        ):
            result = await _run_tool(["nonexistent_tool", "f.ivy"], 10.0, server)

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_timeout(self):
        mock_proc = AsyncMock()

        async def slow_communicate():
            await asyncio.sleep(10)
            return b"", b""

        mock_proc.communicate = slow_communicate
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        server = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_tool(["ivy_check", "f.ivy"], 0.01, server)

        assert result["success"] is False
        assert "Timed out" in result["message"]

    @pytest.mark.asyncio
    async def test_with_progress_token(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        server = MagicMock()
        server.work_done_progress = MagicMock()
        server.work_done_progress.create_async = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_tool(
                ["ivy_check", "f.ivy"], 10.0, server, token="tok-1"
            )

        assert result["success"] is True
        server.work_done_progress.create_async.assert_called_once_with("tok-1")
        server.work_done_progress.begin.assert_called_once()
        server.work_done_progress.end.assert_called_once()


# ---------------------------------------------------------------------------
# Full handler registration (ivy/capabilities)
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_capabilities_response(self):
        from ivy_lsp.lsp.commands import register

        server = MagicMock()
        registered = {}

        def fake_feature(method, _options=None):
            def decorator(fn):
                registered[method] = fn
                return fn

            return decorator

        server.feature = fake_feature
        register(server)

        assert "ivy/capabilities" in registered
        result = asyncio.run(registered["ivy/capabilities"](None))
        assert "fullMode" in result
        assert "ivyCheckAvailable" in result
        assert "ivycAvailable" in result
        assert "ivyShowAvailable" in result


class TestVerifyHandler:
    def test_verify_registered(self):
        from ivy_lsp.lsp.commands import register

        server = MagicMock()
        registered = {}

        def fake_feature(method, _options=None):
            def decorator(fn):
                registered[method] = fn
                return fn

            return decorator

        server.feature = fake_feature
        register(server)

        assert "ivy/verify" in registered
        assert "ivy/compile" in registered
        assert "ivy/showModel" in registered
        assert "ivy/capabilities" in registered


# ---------------------------------------------------------------------------
# Namedtuple params tests (pygls 2.0.1 sends namedtuples, not dicts)
# ---------------------------------------------------------------------------


def _make_registered_handlers():
    """Register handlers on a mock server and return (server, handlers dict)."""
    from ivy_lsp.lsp.commands import register

    server = MagicMock()
    registered = {}

    def fake_feature(method, _options=None):
        def decorator(fn):
            registered[method] = fn
            return fn

        return decorator

    server.feature = fake_feature
    register(server)
    return server, registered


def _make_namedtuple_params(fields: dict):
    """Build a nested namedtuple mimicking pygls _dict_to_object() output."""
    from collections import namedtuple

    def _convert(obj):
        if isinstance(obj, dict):
            converted = {k: _convert(v) for k, v in obj.items()}
            NT = namedtuple("Object", list(converted.keys()), rename=True)
            return NT(**converted)
        return obj

    return _convert(fields)


class TestShowModelHandlerParams:
    """Verify handlers work with namedtuple params (as pygls actually sends)."""

    @pytest.mark.asyncio
    async def test_show_model_with_namedtuple_params(self):
        """Reproduce the exact bug: pygls sends namedtuple, not dict."""
        _server, registered = _make_registered_handlers()
        _server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {"textDocument": {"uri": "file:///test.ivy"}, "workDoneToken": None}
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/showModel"](params)

        assert result["success"] is True


class TestCompileHandlerParams:
    @pytest.mark.asyncio
    async def test_compile_with_namedtuple_params(self):
        _server, registered = _make_registered_handlers()
        _server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///test.ivy"},
                "workDoneToken": None,
                "target": "test",
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/compile"](params)

        assert result["success"] is True


class TestVerifyHandlerParams:
    @pytest.mark.asyncio
    async def test_verify_with_namedtuple_params(self):
        server, registered = _make_registered_handlers()

        # verify handler calls compute_diagnostics which needs doc.source as a string
        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()
        server.indexer = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {"textDocument": {"uri": "file:///test.ivy"}, "workDoneToken": None}
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/verify"](params)

        assert result["success"] is True
        assert "diagnosticCount" in result


# ---------------------------------------------------------------------------
# _resolve_via_staging
# ---------------------------------------------------------------------------


class TestResolveViaStaging:
    def test_staging_available(self):
        from ivy_lsp.lsp.commands import _resolve_via_staging

        server = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = "/tmp/staging/foo.ivy"
        assert (
            _resolve_via_staging(server, "/project/sub/foo.ivy")
            == "/tmp/staging/foo.ivy"
        )

    def test_staging_returns_none(self):
        from ivy_lsp.lsp.commands import _resolve_via_staging

        server = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = None
        assert (
            _resolve_via_staging(server, "/project/sub/foo.ivy")
            == "/project/sub/foo.ivy"
        )

    def test_no_indexer(self):
        from ivy_lsp.lsp.commands import _resolve_via_staging

        server = MagicMock(spec=[])
        assert _resolve_via_staging(server, "/project/foo.ivy") == "/project/foo.ivy"

    def test_indexer_is_none(self):
        from ivy_lsp.lsp.commands import _resolve_via_staging

        server = MagicMock()
        server.indexer = None
        assert _resolve_via_staging(server, "/project/foo.ivy") == "/project/foo.ivy"


class TestShowModelUsesStaging:
    @pytest.mark.asyncio
    async def test_show_model_passes_staged_path(self):
        server, registered = _make_registered_handlers()
        server.indexer = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = "/tmp/staging/test.ivy"

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///project/sub/test.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/showModel"](params)

        call_args = mock_exec.call_args[0]
        assert "/tmp/staging/test.ivy" in call_args
        assert "/project/sub/test.ivy" not in call_args


class TestVerifyUsesStaging:
    @pytest.mark.asyncio
    async def test_verify_passes_staged_path(self):
        server, registered = _make_registered_handlers()
        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()
        server.indexer = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = "/tmp/staging/test.ivy"

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///project/sub/test.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/verify"](params)

        call_args = mock_exec.call_args[0]
        assert "/tmp/staging/test.ivy" in call_args
        assert "/project/sub/test.ivy" not in call_args


class TestCompileUsesStaging:
    @pytest.mark.asyncio
    async def test_compile_passes_staged_path(self):
        server, registered = _make_registered_handlers()
        server.indexer = MagicMock()
        server.indexer.resolver.get_staged_path.return_value = "/tmp/staging/test.ivy"

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///project/sub/test.ivy"},
                "workDoneToken": None,
                "target": "test",
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/compile"](params)

        call_args = mock_exec.call_args[0]
        assert "test.ivy" in call_args
        assert "/project/sub/test.ivy" not in call_args
        assert mock_exec.call_args.kwargs.get("cwd") == "/tmp/staging"


# ---------------------------------------------------------------------------
# _validate_ivy_param
# ---------------------------------------------------------------------------


class TestValidateIvyParam:
    def test_rejects_flags(self):
        with pytest.raises(ValueError):
            _validate_ivy_param("--malicious-flag")

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError):
            _validate_ivy_param("foo;rm -rf /")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _validate_ivy_param("")

    def test_accepts_simple_name(self):
        assert _validate_ivy_param("quic_server") == "quic_server"

    def test_accepts_dotted_name(self):
        assert _validate_ivy_param("tcp.endpoint_1") == "tcp.endpoint_1"

    def test_accepts_underscore_prefix(self):
        assert _validate_ivy_param("_internal") == "_internal"

    def test_accepts_alphanumeric(self):
        assert _validate_ivy_param("frame_ack_v2") == "frame_ack_v2"


# ---------------------------------------------------------------------------
# _find_enclosing_test
# ---------------------------------------------------------------------------


def _make_scoped_server(
    test_scopes: dict,
    active_test: str | None = None,
) -> MagicMock:
    """Build a mock server with a ScopedRequirementModel containing the given test scopes.

    *test_scopes* maps test_file -> frozenset of included files.
    """
    graph = ScopedRequirementModel()
    for test_file, include_closure in test_scopes.items():
        scope = TestScope(
            test_file=test_file,
            include_closure=frozenset(include_closure) | {test_file},
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="unknown",
        )
        graph.register_test_scope(scope)
    if active_test is not None:
        graph.set_active_test(active_test)

    server = MagicMock()
    server.indexer.requirement_graph = graph
    return server


class TestFindEnclosingTest:
    def test_returns_none_for_test_file(self):
        server = _make_scoped_server(
            {
                "/tests/test_quic.ivy": {"/modules/quic_packet.ivy"},
            }
        )
        result = _find_enclosing_test(server, "/tests/test_quic.ivy")
        assert result is None

    def test_returns_active_test_when_covering(self):
        server = _make_scoped_server(
            {
                "/tests/test_a.ivy": {"/modules/mod.ivy"},
                "/tests/test_b.ivy": {"/modules/mod.ivy"},
            },
            active_test="/tests/test_a.ivy",
        )
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        assert result == "/tests/test_a.ivy"

    def test_returns_any_test_when_no_active(self):
        server = _make_scoped_server(
            {
                "/tests/test_b.ivy": {"/modules/mod.ivy"},
                "/tests/test_a.ivy": {"/modules/mod.ivy"},
            }
        )
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        # Deterministic: sorted picks test_a first
        assert result == "/tests/test_a.ivy"

    def test_returns_none_when_no_test_covers(self):
        server = _make_scoped_server(
            {
                "/tests/test_quic.ivy": {"/modules/quic_packet.ivy"},
            }
        )
        result = _find_enclosing_test(server, "/modules/other.ivy")
        assert result is None

    def test_returns_none_when_no_indexer(self):
        server = MagicMock(spec=[])
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        assert result is None

    def test_returns_none_when_no_scoped_model(self):
        server = MagicMock()
        server.indexer.requirement_graph = "not a ScopedRequirementModel"
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        assert result is None

    def test_prefers_active_over_other_tests(self):
        """When active test covers the file, prefer it over alphabetically earlier tests."""
        server = _make_scoped_server(
            {
                "/tests/test_a.ivy": {"/modules/mod.ivy"},
                "/tests/test_z.ivy": {"/modules/mod.ivy"},
            },
            active_test="/tests/test_z.ivy",
        )
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        assert result == "/tests/test_z.ivy"

    def test_falls_back_when_active_does_not_cover(self):
        """Active test exists but doesn't include the module."""
        server = _make_scoped_server(
            {
                "/tests/test_a.ivy": {"/modules/mod.ivy"},
                "/tests/test_z.ivy": {"/modules/other.ivy"},
            },
            active_test="/tests/test_z.ivy",
        )
        result = _find_enclosing_test(server, "/modules/mod.ivy")
        assert result == "/tests/test_a.ivy"


# ---------------------------------------------------------------------------
# Handler redirection integration tests
# ---------------------------------------------------------------------------


class TestShowModelRedirection:
    """Verify ivy/showModel redirects module files to their enclosing test."""

    @pytest.mark.asyncio
    async def test_show_model_redirects_module_to_test(self):
        server, registered = _make_registered_handlers()

        # Set up scoped model with a test that includes the module
        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None
        server.indexer.include_graph.get_transitive_includes.return_value = []
        server.indexer._cache = {}

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/showModel"](params)

        assert result["success"] is True
        # The command should have been called with the test file, not the module
        call_args = mock_exec.call_args[0]
        assert "/tests/test_quic.ivy" in call_args
        assert "/modules/quic_packet.ivy" not in call_args

    @pytest.mark.asyncio
    async def test_show_model_no_redirect_for_test_file(self):
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset({"/tests/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///tests/test_quic.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/showModel"](params)

        assert result["success"] is True
        call_args = mock_exec.call_args[0]
        assert "/tests/test_quic.ivy" in call_args

    @pytest.mark.asyncio
    async def test_show_model_adds_coi_false_when_redirected_no_isolate(self):
        """When redirected and no isolate resolved, coi=false must appear."""
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None
        server.indexer.include_graph.get_transitive_includes.return_value = []
        server.indexer._cache = {}

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/showModel"](params)

        call_args = mock_exec.call_args[0]
        assert "coi=false" in call_args

    @pytest.mark.asyncio
    async def test_show_model_no_coi_false_when_isolate_matched(self):
        """When redirected but an isolate was resolved, coi=false must NOT appear."""
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None
        server.indexer.include_graph.get_transitive_includes.return_value = []
        server.indexer._cache = {}

        # Return a single isolate "quic_packet" from document symbols.
        # The pre-redirect block finds exactly 1 isolate and auto-selects it.
        ns_symbol = lsp.DocumentSymbol(
            name="quic_packet",
            kind=lsp.SymbolKind.Namespace,
            range=lsp.Range(start=lsp.Position(1, 0), end=lsp.Position(10, 0)),
            selection_range=lsp.Range(
                start=lsp.Position(1, 0), end=lsp.Position(1, 12)
            ),
            children=None,
        )

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
            }
        )

        with (
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch(
                "ivy_lsp.lsp.document_symbols.compute_document_symbols",
                return_value=[ns_symbol],
            ),
        ):
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/showModel"](params)

        call_args = mock_exec.call_args[0]
        assert "isolate=quic_packet" in call_args
        assert "coi=false" not in call_args

    @pytest.mark.asyncio
    async def test_show_model_no_coi_false_when_not_redirected(self):
        """When file is a test (no redirection), coi=false must NOT appear."""
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset({"/tests/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///tests/test_quic.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/showModel"](params)

        call_args = mock_exec.call_args[0]
        assert "coi=false" not in call_args


class TestVerifyRedirection:
    @pytest.mark.asyncio
    async def test_verify_redirects_module_to_test(self):
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None
        server.indexer.include_graph.get_transitive_includes.return_value = []
        server.indexer._cache = {}

        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/verify"](params)

        assert result["success"] is True
        call_args = mock_exec.call_args[0]
        assert "/tests/test_quic.ivy" in call_args

    @pytest.mark.asyncio
    async def test_verify_adds_coi_false_when_redirected_no_isolate(self):
        """When redirected and no isolate resolved, coi=false must appear."""
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None
        server.indexer.include_graph.get_transitive_includes.return_value = []
        server.indexer._cache = {}

        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/verify"](params)

        call_args = mock_exec.call_args[0]
        assert "coi=false" in call_args

    @pytest.mark.asyncio
    async def test_verify_no_coi_false_when_not_redirected(self):
        """When file is a test (no redirection), coi=false must NOT appear."""
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset({"/tests/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None

        doc = MagicMock()
        doc.source = "#lang ivy1.7\n"
        doc.version = 1
        server.workspace.get_text_document.return_value = doc
        server.parser = MagicMock()

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///tests/test_quic.ivy"},
                "workDoneToken": None,
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/verify"](params)

        call_args = mock_exec.call_args[0]
        assert "coi=false" not in call_args


class TestCompileRedirection:
    @pytest.mark.asyncio
    async def test_compile_redirects_module_to_test(self):
        server, registered = _make_registered_handlers()

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/tests/test_quic.ivy",
            include_closure=frozenset(
                {
                    "/tests/test_quic.ivy",
                    "/modules/quic_packet.ivy",
                }
            ),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        server.indexer.requirement_graph = graph
        server.indexer.resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params(
            {
                "textDocument": {"uri": "file:///modules/quic_packet.ivy"},
                "workDoneToken": None,
                "target": "test",
            }
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/compile"](params)

        assert result["success"] is True
        call_args = mock_exec.call_args[0]
        assert "test_quic.ivy" in call_args
        assert "/modules/quic_packet.ivy" not in call_args
        assert mock_exec.call_args.kwargs.get("cwd") == "/tests"
