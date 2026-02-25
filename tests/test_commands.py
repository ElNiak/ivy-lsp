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
        server.progress = MagicMock()
        server.progress.create_async = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_tool(
                ["ivy_check", "f.ivy"], 10.0, server, token="tok-1"
            )

        assert result["success"] is True
        server.progress.create_async.assert_called_once_with("tok-1")
        server.progress.begin.assert_called_once()
        server.progress.end.assert_called_once()


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
