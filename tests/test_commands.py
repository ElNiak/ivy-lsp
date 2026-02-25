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

from ivy_lsp.features.commands import (
    _detect_isolate_at_position,
    _find_tool,
    _run_tool,
)
from ivy_lsp.features.diagnostics import parse_ivy_check_output


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
        server.workspace.get_text_document.return_value = doc
        server._parser = MagicMock()
        server._indexer = MagicMock()
        return server

    def test_none_position_returns_none(self):
        server = self._make_server([])
        result = _detect_isolate_at_position(server, "file:///a.ivy", None)
        assert result is None

    @patch("ivy_lsp.features.document_symbols.compute_document_symbols")
    def test_inside_isolate(self, mock_compute):
        ns_symbol = lsp.DocumentSymbol(
            name="test_iso",
            kind=lsp.SymbolKind.Namespace,
            range=lsp.Range(
                start=lsp.Position(1, 0), end=lsp.Position(3, 1)
            ),
            selection_range=lsp.Range(
                start=lsp.Position(1, 0), end=lsp.Position(1, 8)
            ),
            children=None,
        )
        mock_compute.return_value = [ns_symbol]
        server = self._make_server([ns_symbol])
        result = _detect_isolate_at_position(
            server, "file:///a.ivy", lsp.Position(line=2, character=0)
        )
        assert result == "test_iso"

    @patch("ivy_lsp.features.document_symbols.compute_document_symbols")
    def test_outside_isolate(self, mock_compute):
        ns_symbol = lsp.DocumentSymbol(
            name="test_iso",
            kind=lsp.SymbolKind.Namespace,
            range=lsp.Range(
                start=lsp.Position(1, 0), end=lsp.Position(3, 1)
            ),
            selection_range=lsp.Range(
                start=lsp.Position(1, 0), end=lsp.Position(1, 8)
            ),
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
            result = await _run_tool(
                ["nonexistent_tool", "f.ivy"], 10.0, server
            )

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
            result = await _run_tool(
                ["ivy_check", "f.ivy"], 0.01, server
            )

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
        from ivy_lsp.features.commands import register

        server = MagicMock()
        registered = {}

        def fake_feature(method):
            def decorator(fn):
                registered[method] = fn
                return fn
            return decorator

        server.feature = fake_feature
        register(server)

        assert "ivy/capabilities" in registered
        result = registered["ivy/capabilities"](None)
        assert "fullMode" in result
        assert "ivyCheckAvailable" in result
        assert "ivycAvailable" in result
        assert "ivyShowAvailable" in result


class TestVerifyHandler:
    def test_verify_registered(self):
        from ivy_lsp.features.commands import register

        server = MagicMock()
        registered = {}

        def fake_feature(method):
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
    from ivy_lsp.features.commands import register

    server = MagicMock()
    registered = {}

    def fake_feature(method):
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
        _server._indexer._resolver.get_staged_path.return_value = None

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
        _server._indexer._resolver.get_staged_path.return_value = None

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
        server.workspace.get_text_document.return_value = doc
        server._parser = MagicMock()
        server._indexer = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = None

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
        from ivy_lsp.features.commands import _resolve_via_staging

        server = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = "/tmp/staging/foo.ivy"
        assert _resolve_via_staging(server, "/project/sub/foo.ivy") == "/tmp/staging/foo.ivy"

    def test_staging_returns_none(self):
        from ivy_lsp.features.commands import _resolve_via_staging

        server = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = None
        assert _resolve_via_staging(server, "/project/sub/foo.ivy") == "/project/sub/foo.ivy"

    def test_no_indexer(self):
        from ivy_lsp.features.commands import _resolve_via_staging

        server = MagicMock(spec=[])
        assert _resolve_via_staging(server, "/project/foo.ivy") == "/project/foo.ivy"

    def test_indexer_is_none(self):
        from ivy_lsp.features.commands import _resolve_via_staging

        server = MagicMock()
        server._indexer = None
        assert _resolve_via_staging(server, "/project/foo.ivy") == "/project/foo.ivy"


class TestShowModelUsesStaging:
    @pytest.mark.asyncio
    async def test_show_model_passes_staged_path(self):
        server, registered = _make_registered_handlers()
        server._indexer = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = "/tmp/staging/test.ivy"

        params = _make_namedtuple_params(
            {"textDocument": {"uri": "file:///project/sub/test.ivy"}, "workDoneToken": None}
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
        server.workspace.get_text_document.return_value = doc
        server._parser = MagicMock()
        server._indexer = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = (
            "/tmp/staging/test.ivy"
        )

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
        server._indexer = MagicMock()
        server._indexer._resolver.get_staged_path.return_value = (
            "/tmp/staging/test.ivy"
        )

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
        assert "/tmp/staging/test.ivy" in call_args
        assert "/project/sub/test.ivy" not in call_args

