# tests/test_active_test_commands.py
"""Tests for ivy/setActiveTest, ivy/listTests, ivy/compileTest commands."""

import asyncio
import sys
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import RequirementGraph
from ivy_lsp.analysis.test_scope import ScopedRequirementModel, TestScope
from ivy_lsp.features.commands import register


# ---------------------------------------------------------------------------
# Helpers (duplicated from test_commands.py -- small, self-contained)
# ---------------------------------------------------------------------------


def _make_registered_handlers():
    """Register handlers on a mock server and return (server, handlers dict)."""
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


def _call(fn, *args, **kwargs):
    """Call a handler synchronously, running coroutines to completion."""
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _make_namedtuple_params(fields: dict):
    """Build a nested namedtuple mimicking pygls _dict_to_object() output."""
    def _convert(obj):
        if isinstance(obj, dict):
            converted = {k: _convert(v) for k, v in obj.items()}
            NT = namedtuple("Object", list(converted.keys()), rename=True)
            return NT(**converted)
        return obj
    return _convert(fields)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped_server():
    """Server with ScopedRequirementModel containing two test scopes."""
    server, registered = _make_registered_handlers()

    model = ScopedRequirementModel()
    scope_client = TestScope(
        test_file="/workspace/quic_client_test.ivy",
        include_closure=frozenset({
            "/workspace/quic_client_test.ivy",
            "/workspace/quic_stack.ivy",
            "/workspace/quic_types.ivy",
        }),
        exported_actions=frozenset({"quic.send_packet", "quic.send_frame"}),
        imported_actions=frozenset({"tls.handshake"}),
        tester_role="client",
    )
    scope_server = TestScope(
        test_file="/workspace/quic_server_test.ivy",
        include_closure=frozenset({
            "/workspace/quic_server_test.ivy",
            "/workspace/quic_stack.ivy",
        }),
        exported_actions=frozenset({"quic.recv_packet"}),
        imported_actions=frozenset(),
        tester_role="server",
    )
    model.register_test_scope(scope_client)
    model.register_test_scope(scope_server)

    server._indexer._requirement_graph = model
    return server, registered, model


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestNewCommandsRegistered:
    def test_set_active_test_registered(self):
        _server, registered = _make_registered_handlers()
        assert "ivy/setActiveTest" in registered

    def test_list_tests_registered(self):
        _server, registered = _make_registered_handlers()
        assert "ivy/listTests" in registered

    def test_compile_test_registered(self):
        _server, registered = _make_registered_handlers()
        assert "ivy/compileTest" in registered

    def test_existing_commands_still_registered(self):
        _server, registered = _make_registered_handlers()
        assert "ivy/verify" in registered
        assert "ivy/compile" in registered
        assert "ivy/showModel" in registered
        assert "ivy/capabilities" in registered


# ---------------------------------------------------------------------------
# ivy/setActiveTest
# ---------------------------------------------------------------------------


class TestSetActiveTestHandler:
    def test_set_known_test(self, scoped_server):
        server, registered, model = scoped_server
        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is True
        assert result["activeTest"] == "/workspace/quic_client_test.ivy"
        assert model.get_active_scope() is not None
        assert model.get_active_scope().tester_role == "client"

    def test_clear_active_test(self, scoped_server):
        server, registered, model = scoped_server
        # First set one
        model.set_active_test("/workspace/quic_client_test.ivy")
        # Then clear
        params = _make_namedtuple_params({"testFile": None})
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is True
        assert result["activeTest"] is None
        assert model.get_active_scope() is None

    def test_set_unknown_test_returns_error(self, scoped_server):
        server, registered, model = scoped_server
        params = _make_namedtuple_params({
            "testFile": "/workspace/nonexistent.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is False
        assert "error" in result

    def test_no_scoped_model_returns_error(self):
        server, registered = _make_registered_handlers()
        # Plain RequirementGraph, not ScopedRequirementModel
        server._indexer._requirement_graph = RequirementGraph()
        params = _make_namedtuple_params({
            "testFile": "/workspace/test.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is False
        assert "error" in result

    def test_switch_between_tests(self, scoped_server):
        server, registered, model = scoped_server
        params_a = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
        })
        params_b = _make_namedtuple_params({
            "testFile": "/workspace/quic_server_test.ivy",
        })
        _call(registered["ivy/setActiveTest"],params_a)
        assert model.get_active_scope().tester_role == "client"
        _call(registered["ivy/setActiveTest"],params_b)
        assert model.get_active_scope().tester_role == "server"

    def test_set_unknown_after_known_returns_error(self, scoped_server):
        """Setting unknown test after a known one must fail, not silently keep the old."""
        server, registered, model = scoped_server
        params_valid = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params_valid)
        assert result["success"] is True

        params_unknown = _make_namedtuple_params({
            "testFile": "/workspace/nonexistent.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params_unknown)
        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# ivy/listTests
# ---------------------------------------------------------------------------


class TestListTestsHandler:
    def test_list_with_scopes(self, scoped_server):
        server, registered, model = scoped_server
        result = _call(registered["ivy/listTests"],None)
        assert "tests" in result
        tests = result["tests"]
        assert len(tests) == 2
        # Sorted by testFile for determinism
        files = [t["testFile"] for t in tests]
        assert files == sorted(files)

    def test_list_entry_fields(self, scoped_server):
        server, registered, model = scoped_server
        result = _call(registered["ivy/listTests"],None)
        entry = next(
            t for t in result["tests"]
            if t["testFile"] == "/workspace/quic_client_test.ivy"
        )
        assert entry["testerRole"] == "client"
        assert entry["exportCount"] == 2
        assert entry["importCount"] == 1
        assert entry["includeCount"] == 3

    def test_list_empty_scopes(self):
        server, registered = _make_registered_handlers()
        server._indexer._requirement_graph = ScopedRequirementModel()
        result = _call(registered["ivy/listTests"],None)
        assert result["tests"] == []

    def test_list_no_scoped_model(self):
        server, registered = _make_registered_handlers()
        server._indexer._requirement_graph = RequirementGraph()
        result = _call(registered["ivy/listTests"],None)
        assert result["tests"] == []

    def test_list_includes_active_marker(self, scoped_server):
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")
        result = _call(registered["ivy/listTests"],None)
        assert result["activeTest"] == "/workspace/quic_client_test.ivy"

    def test_list_no_active_test(self, scoped_server):
        server, registered, model = scoped_server
        result = _call(registered["ivy/listTests"],None)
        assert result["activeTest"] is None


# ---------------------------------------------------------------------------
# ivy/compileTest
# ---------------------------------------------------------------------------


class TestCompileTestHandler:
    @pytest.mark.asyncio
    async def test_compile_success(self, scoped_server):
        server, registered, model = scoped_server
        server._indexer._resolver.get_staged_path.return_value = (
            "/tmp/staging/quic_client_test.ivy"
        )

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/compileTest"](params)

        assert result["success"] is True
        assert result["message"] == "OK"

    @pytest.mark.asyncio
    async def test_compile_uses_staging(self, scoped_server):
        server, registered, model = scoped_server
        server._indexer._resolver.get_staged_path.return_value = (
            "/tmp/staging/quic_client_test.ivy"
        )

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/compileTest"](params)

        call_args = mock_exec.call_args[0]
        assert "/tmp/staging/quic_client_test.ivy" in call_args
        assert "/workspace/quic_client_test.ivy" not in call_args

    @pytest.mark.asyncio
    async def test_compile_cmd_has_target_test(self, scoped_server):
        server, registered, model = scoped_server
        server._indexer._resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/compileTest"](params)

        call_args = mock_exec.call_args[0]
        assert "ivyc" in call_args
        assert "target=test" in call_args

    @pytest.mark.asyncio
    async def test_compile_stores_result(self, scoped_server):
        server, registered, model = scoped_server
        server._indexer._resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await registered["ivy/compileTest"](params)

        assert "/workspace/quic_client_test.ivy" in model._compilation_results
        assert model._compilation_results["/workspace/quic_client_test.ivy"]["success"]

    @pytest.mark.asyncio
    async def test_compile_no_test_file(self, scoped_server):
        server, registered, model = scoped_server
        params = _make_namedtuple_params({
            "testFile": None,
            "workDoneToken": None,
        })
        result = await registered["ivy/compileTest"](params)
        assert result["success"] is False
        assert "testFile" in result["message"].lower() or "test" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_compile_failure_exit_code(self, scoped_server):
        server, registered, model = scoped_server
        server._indexer._resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (
                b"", b"error: compilation failed\n"
            )
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            result = await registered["ivy/compileTest"](params)

        assert result["success"] is False
        assert "Exit code 1" in result["message"]

    @pytest.mark.asyncio
    async def test_compile_without_scoped_model_still_works(self):
        """Compile should work even with plain RequirementGraph."""
        server, registered = _make_registered_handlers()
        server._indexer._requirement_graph = RequirementGraph()
        server._indexer._resolver.get_staged_path.return_value = None

        params = _make_namedtuple_params({
            "testFile": "/workspace/test.ivy",
            "workDoneToken": None,
        })

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"ok\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await registered["ivy/compileTest"](params)

        assert result["success"] is True


# ---------------------------------------------------------------------------
# ivy/setActiveTest -- diagnostic refresh (Task 15)
# ---------------------------------------------------------------------------


class TestSetActiveTestDiagnosticRefresh:
    """Tests that ivy/setActiveTest refreshes diagnostics for open documents."""

    def test_successful_set_publishes_diagnostics(self, scoped_server):
        """Setting active test should trigger diagnostic publish for open docs."""
        server, registered, model = scoped_server

        mock_doc = MagicMock()
        mock_doc.uri = "file:///workspace/quic_stack.ivy"
        mock_doc.source = "action quic.send(x:t)\n"
        server.workspace.text_documents = {mock_doc.uri: mock_doc}

        with patch(
            "ivy_lsp.features.diagnostics.compute_diagnostics",
            return_value=[],
        ):
            params = _make_namedtuple_params({
                "testFile": "/workspace/quic_client_test.ivy",
            })
            result = _call(registered["ivy/setActiveTest"],params)

        assert result["success"] is True
        server.text_document_publish_diagnostics.assert_called_once()

    def test_clear_active_test_publishes_diagnostics(self, scoped_server):
        """Clearing active test should also refresh diagnostics."""
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")

        mock_doc = MagicMock()
        mock_doc.uri = "file:///workspace/quic_stack.ivy"
        mock_doc.source = "#lang ivy1.7\naction quic.send(x:t)\n"
        server.workspace.text_documents = {mock_doc.uri: mock_doc}

        with patch(
            "ivy_lsp.features.diagnostics.compute_diagnostics",
            return_value=[],
        ):
            params = _make_namedtuple_params({"testFile": None})
            result = _call(registered["ivy/setActiveTest"],params)

        assert result["success"] is True
        server.text_document_publish_diagnostics.assert_called_once()

    def test_failed_set_does_not_publish(self, scoped_server):
        """Setting unknown test should NOT trigger diagnostic publish."""
        server, registered, model = scoped_server

        params = _make_namedtuple_params({
            "testFile": "/workspace/nonexistent.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is False
        server.text_document_publish_diagnostics.assert_not_called()

    def test_refresh_skips_non_file_uris(self, scoped_server):
        """Diagnostic refresh should only process file:// URIs."""
        server, registered, model = scoped_server

        mock_ivy = MagicMock()
        mock_ivy.uri = "file:///workspace/quic_stack.ivy"
        mock_ivy.source = "action quic.send(x:t)\n"

        mock_untitled = MagicMock()
        mock_untitled.uri = "untitled:Untitled-1"
        mock_untitled.source = "some text"

        server.workspace.text_documents = {
            mock_ivy.uri: mock_ivy,
            mock_untitled.uri: mock_untitled,
        }

        with patch(
            "ivy_lsp.features.diagnostics.compute_diagnostics",
            return_value=[],
        ) as mock_compute:
            params = _make_namedtuple_params({
                "testFile": "/workspace/quic_client_test.ivy",
            })
            _call(registered["ivy/setActiveTest"],params)

        # compute_diagnostics called only for file:// URI, not untitled
        assert mock_compute.call_count == 1
        call_filepath = mock_compute.call_args[0][2]  # 3rd positional arg
        assert call_filepath == "/workspace/quic_stack.ivy"

    def test_no_workspace_text_documents_no_crash(self, scoped_server):
        """If workspace has no iterable text_documents, handler still succeeds."""
        server, registered, model = scoped_server
        # Don't set text_documents -- MagicMock auto-attr is not iterable,
        # so _refresh_open_diagnostics catches TypeError and returns.

        params = _make_namedtuple_params({
            "testFile": "/workspace/quic_client_test.ivy",
        })
        result = _call(registered["ivy/setActiveTest"],params)
        assert result["success"] is True

    def test_multiple_open_docs_all_refreshed(self, scoped_server):
        """All open file:// documents get refreshed, not just the first."""
        server, registered, model = scoped_server

        docs = {}
        for name in ("quic_stack.ivy", "quic_types.ivy", "quic_utils.ivy"):
            doc = MagicMock()
            doc.uri = f"file:///workspace/{name}"
            doc.source = f"# {name}\n"
            docs[doc.uri] = doc
        server.workspace.text_documents = docs

        with patch(
            "ivy_lsp.features.diagnostics.compute_diagnostics",
            return_value=[],
        ):
            params = _make_namedtuple_params({
                "testFile": "/workspace/quic_client_test.ivy",
            })
            _call(registered["ivy/setActiveTest"],params)

        assert server.text_document_publish_diagnostics.call_count == 3


# ---------------------------------------------------------------------------
# ivy/activeDocumentChanged (Task 16)
# ---------------------------------------------------------------------------


class TestActiveDocumentChangedRegistered:
    def test_handler_registered(self):
        _server, registered = _make_registered_handlers()
        assert "ivy/activeDocumentChanged" in registered


class TestActiveDocumentChanged:
    """Tests for ivy/activeDocumentChanged notification handler."""

    def test_opening_test_file_sets_active_test(self, scoped_server):
        """Opening a test file should auto-set it as active test."""
        server, registered, model = scoped_server
        assert model.get_active_scope() is None

        params = _make_namedtuple_params({
            "uri": "file:///workspace/quic_client_test.ivy",
        })
        _call(registered["ivy/activeDocumentChanged"],params)
        assert model.get_active_scope() is not None
        assert model.get_active_scope().test_file == "/workspace/quic_client_test.ivy"

    def test_opening_non_test_file_keeps_active_test(self, scoped_server):
        """Opening a non-test file should NOT clear the active test (sticky)."""
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")

        params = _make_namedtuple_params({
            "uri": "file:///workspace/quic_stack.ivy",
        })
        _call(registered["ivy/activeDocumentChanged"],params)
        # Active test should still be quic_client_test.ivy
        assert model.get_active_scope() is not None
        assert model.get_active_scope().test_file == "/workspace/quic_client_test.ivy"

    def test_switching_between_test_files(self, scoped_server):
        """Opening a different test file should switch the active test."""
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")

        params = _make_namedtuple_params({
            "uri": "file:///workspace/quic_server_test.ivy",
        })
        _call(registered["ivy/activeDocumentChanged"],params)
        assert model.get_active_scope().test_file == "/workspace/quic_server_test.ivy"
        assert model.get_active_scope().tester_role == "server"

    def test_no_scoped_model_no_crash(self):
        """Handler should not crash when graph is plain RequirementGraph."""
        server, registered = _make_registered_handlers()
        server._indexer._requirement_graph = RequirementGraph()

        params = _make_namedtuple_params({
            "uri": "file:///workspace/test.ivy",
        })
        # Should not raise
        _call(registered["ivy/activeDocumentChanged"],params)

    def test_no_indexer_no_crash(self):
        """Handler should not crash when indexer is missing."""
        server, registered = _make_registered_handlers()
        del server._indexer

        params = _make_namedtuple_params({
            "uri": "file:///workspace/test.ivy",
        })
        # Should not raise
        _call(registered["ivy/activeDocumentChanged"],params)

    def test_changed_test_triggers_diagnostic_refresh(self, scoped_server):
        """When active test changes, diagnostics should be refreshed."""
        server, registered, model = scoped_server
        mock_doc = MagicMock()
        mock_doc.uri = "file:///workspace/quic_stack.ivy"
        mock_doc.source = "action quic.send(x:t)\n"
        server.workspace.text_documents = {mock_doc.uri: mock_doc}

        with patch(
            "ivy_lsp.features.diagnostics.compute_diagnostics",
            return_value=[],
        ):
            params = _make_namedtuple_params({
                "uri": "file:///workspace/quic_client_test.ivy",
            })
            _call(registered["ivy/activeDocumentChanged"],params)

        server.text_document_publish_diagnostics.assert_called()

    def test_same_test_no_redundant_refresh(self, scoped_server):
        """Re-opening the same test file should NOT refresh diagnostics."""
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")

        params = _make_namedtuple_params({
            "uri": "file:///workspace/quic_client_test.ivy",
        })
        _call(registered["ivy/activeDocumentChanged"],params)
        server.text_document_publish_diagnostics.assert_not_called()

    def test_non_file_uri_ignored(self, scoped_server):
        """Non-file URIs (untitled, git) should be ignored."""
        server, registered, model = scoped_server
        model.set_active_test("/workspace/quic_client_test.ivy")

        params = _make_namedtuple_params({
            "uri": "untitled:Untitled-1",
        })
        _call(registered["ivy/activeDocumentChanged"],params)
        assert model.get_active_scope().test_file == "/workspace/quic_client_test.ivy"

    def test_none_params_no_crash(self):
        """Handler must not crash when pygls passes params=None."""
        server, registered = _make_registered_handlers()
        # Should not raise — getattr(None, "uri", None) returns None,
        # triggering the early return on the `not uri` guard.
        _call(registered["ivy/activeDocumentChanged"],None)


class TestNoneParamsGuards:
    """Verify all custom handlers survive params=None (pygls deserialization edge case)."""

    def test_set_active_test_none_params(self):
        server, registered = _make_registered_handlers()
        result = _call(registered["ivy/setActiveTest"],None)
        assert result["success"] is False
        assert "No params" in result["error"]

    @pytest.mark.asyncio
    async def test_compile_test_none_params(self):
        server, registered = _make_registered_handlers()
        result = await registered["ivy/compileTest"](None)
        assert result["success"] is False
        assert "No params" in result["message"]

    def test_active_document_changed_none_params(self):
        server, registered = _make_registered_handlers()
        # Should return without error (handler returns None)
        _call(registered["ivy/activeDocumentChanged"],None)


# ---------------------------------------------------------------------------
# pygls converter monkey-patch (_fixed_params_hook)
# ---------------------------------------------------------------------------


class TestFixedParamsHook:
    """Verify the pygls monkey-patch handles optional JSON-RPC params."""

    def test_missing_params_injects_none(self):
        """When 'params' key is absent, hook should set params=None."""
        from ivy_lsp.__main__ import _fixed_params_hook

        calls = []

        def fake_cls(**kwargs):
            calls.append(kwargs)
            return kwargs

        obj = {"jsonrpc": "2.0", "id": 1, "method": "ivy/listTests"}
        result = _fixed_params_hook(obj, fake_cls)
        assert result["params"] is None

    def test_present_params_dict_converted(self):
        """When 'params' key is present with a dict, hook should convert it."""
        from ivy_lsp.__main__ import _fixed_params_hook

        calls = []

        def fake_cls(**kwargs):
            calls.append(kwargs)
            return kwargs

        obj = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ivy/setActiveTest",
            "params": {"testFile": "/workspace/test.ivy"},
        }
        result = _fixed_params_hook(obj, fake_cls)
        # _dict_to_object converts dicts to namedtuples
        assert hasattr(result["params"], "testFile")
        assert result["params"].testFile == "/workspace/test.ivy"

    def test_null_params_stays_none(self):
        """When 'params' key is present but None, hook should keep it None."""
        from ivy_lsp.__main__ import _fixed_params_hook

        def fake_cls(**kwargs):
            return kwargs

        obj = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ivy/listTests",
            "params": None,
        }
        result = _fixed_params_hook(obj, fake_cls)
        assert result["params"] is None

    def test_patch_function_registers_hooks(self):
        """_patch_pygls_converter should register hooks on the converter."""
        from ivy_lsp.__main__ import _patch_pygls_converter

        mock_server = MagicMock()
        mock_converter = MagicMock()
        mock_server.protocol._converter = mock_converter

        _patch_pygls_converter(mock_server)

        assert mock_converter.register_structure_hook.call_count == 2
