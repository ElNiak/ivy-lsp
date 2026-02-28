"""Tests for bulk compilation trigger in the server."""
from __future__ import annotations

import os
import threading
import types
from unittest.mock import MagicMock

from ivy_lsp.compilation.ir import ActionIR, CompiledModuleIR


class FakeScopedModel:
    """Minimal ScopedRequirementModel stub."""

    def __init__(self):
        self._test_scopes = {}
        self.actions = {}

    def add_action(self, node):
        self.actions[node.id] = node


class FakeSemanticModel:
    """Minimal SemanticModel stub."""

    def __init__(self):
        self._files = {}

    def update_file(self, filepath, nodes, edges, tier):
        self._files[filepath] = {"nodes": nodes, "edges": edges, "tier": tier}

    def get_nodes_in_file(self, filepath):
        entry = self._files.get(filepath, {})
        return entry.get("nodes", [])

    def node_count(self):
        return sum(len(e["nodes"]) for e in self._files.values())

    def edge_count(self):
        return sum(len(e["edges"]) for e in self._files.values())

    def get_outgoing(self, node_id):
        return []


class FakeCompilerManager:
    """CompilerManager stub that calls back immediately."""

    def __init__(self, results=None):
        self._results = results or {}
        self.compile_calls = []

    def compile_async(self, source, filepath, callback):
        self.compile_calls.append(filepath)
        ir = self._results.get(filepath, CompiledModuleIR.empty(filepath))
        callback(ir)

    def get_cached(self, filepath):
        return self._results.get(filepath)

    def shutdown(self):
        pass


class FakeIndexer:
    """Minimal indexer stub."""

    def __init__(self, graph):
        self._requirement_graph = graph


class TestBulkCompilation:
    def _make_server(self, test_files=None, compile_results=None):
        """Build a minimal server-like object for testing _start_bulk_compilation."""
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        if test_files:
            for tf in test_files:
                # Register fake test scopes
                scope = TestScope(
                    test_file=tf,
                    include_closure=frozenset([tf]),
                    exported_actions=frozenset(["ext:send"]),
                    imported_actions=frozenset(),
                    tester_role="client",
                )
                graph.register_test_scope(scope)

        # Monkey-patch a server-like object
        class Server:
            pass

        server = Server()
        server._compiler_manager = FakeCompilerManager(compile_results or {})
        server._indexer = FakeIndexer(graph)
        server._semantic_model = FakeSemanticModel()
        server._bulk_analysis_cancel = threading.Event()
        server._bulk_compile_running = False
        server._bulk_compile_total = 0
        server._bulk_compile_completed = 0

        # Mock protocol and work_done_progress for notification tests
        server.protocol = MagicMock()
        server.work_done_progress = MagicMock()

        # Bind the actual methods
        from ivy_lsp.server import IvyLanguageServer

        server._start_bulk_compilation = types.MethodType(
            IvyLanguageServer._start_bulk_compilation, server
        )
        server._make_bulk_compile_progress_callback = types.MethodType(
            IvyLanguageServer._make_bulk_compile_progress_callback, server
        )
        server._send_model_ready_notification = lambda: None

        return server

    def test_no_compiler_manager_is_noop(self):
        server = self._make_server()
        server._compiler_manager = None
        # Should not raise
        server._start_bulk_compilation()

    def test_no_indexer_is_noop(self):
        server = self._make_server()
        server._indexer = None
        server._start_bulk_compilation()

    def test_no_test_files_is_noop(self):
        server = self._make_server(test_files=[])
        server._start_bulk_compilation()
        assert len(server._compiler_manager.compile_calls) == 0

    def test_compiles_each_test_file(self, tmp_path):
        # Create actual files so open() works
        f1 = tmp_path / "test1.ivy"
        f2 = tmp_path / "test2.ivy"
        f1.write_text("#lang ivy1.8\ntype t\n")
        f2.write_text("#lang ivy1.8\ntype s\n")

        ir1 = CompiledModuleIR(
            actions={"ext:send": ActionIR(name="ext:send", is_exported=True)},
            success=True,
            source_file=str(f1),
        )
        ir2 = CompiledModuleIR(
            actions={"ext:recv": ActionIR(name="ext:recv", is_imported=True)},
            success=True,
            source_file=str(f2),
        )

        server = self._make_server(
            test_files=[str(f1), str(f2)],
            compile_results={str(f1): ir1, str(f2): ir2},
        )
        server._start_bulk_compilation()
        assert len(server._compiler_manager.compile_calls) == 2
        assert str(f1) in server._compiler_manager.compile_calls
        assert str(f2) in server._compiler_manager.compile_calls

    def test_failed_ir_does_not_crash(self, tmp_path):
        f1 = tmp_path / "test1.ivy"
        f1.write_text("#lang ivy1.8\n")
        ir = CompiledModuleIR.empty(str(f1), errors=["parse error"])

        server = self._make_server(
            test_files=[str(f1)],
            compile_results={str(f1): ir},
        )
        # Should not raise even with failed IR
        server._start_bulk_compilation()
        assert len(server._compiler_manager.compile_calls) == 1

    def test_env_var_disables_bulk_compile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IVY_LSP_BULK_COMPILE", "0")
        f1 = tmp_path / "test1.ivy"
        f1.write_text("#lang ivy1.8\n")

        server = self._make_server(
            test_files=[str(f1)],
            compile_results={
                str(f1): CompiledModuleIR(
                    success=True, source_file=str(f1)
                )
            },
        )
        server._start_bulk_compilation()
        assert len(server._compiler_manager.compile_calls) == 0

    def test_missing_file_skipped(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist.ivy")
        server = self._make_server(
            test_files=[nonexistent],
            compile_results={},
        )
        # Should not raise
        server._start_bulk_compilation()
        # compile_async not called for unreadable file
        assert len(server._compiler_manager.compile_calls) == 0

    def test_progress_notification_sent(self, tmp_path):
        """ivy/compilationProgress notification is sent for each compiled file."""
        f1 = tmp_path / "test1.ivy"
        f2 = tmp_path / "test2.ivy"
        f1.write_text("#lang ivy1.8\ntype t\n")
        f2.write_text("#lang ivy1.8\ntype s\n")

        ir1 = CompiledModuleIR(
            actions={"ext:send": ActionIR(name="ext:send", is_exported=True)},
            success=True,
            source_file=str(f1),
        )
        ir2 = CompiledModuleIR.empty(str(f2), errors=["parse error"])

        server = self._make_server(
            test_files=[str(f1), str(f2)],
            compile_results={str(f1): ir1, str(f2): ir2},
        )
        server._start_bulk_compilation()

        # Verify ivy/compilationProgress notifications were sent
        notify_calls = [
            c for c in server.protocol.notify.call_args_list
            if c[0][0] == "ivy/compilationProgress"
        ]
        assert len(notify_calls) == 2

        # Check first notification payload
        payload0 = notify_calls[0][0][1]
        assert payload0["total"] == 2
        assert payload0["completed"] >= 1
        assert "success" in payload0
        assert "currentFile" in payload0

        # Check second notification payload
        payload1 = notify_calls[1][0][1]
        assert payload1["total"] == 2
        assert payload1["completed"] == 2

    def test_bulk_compile_state_tracking(self, tmp_path):
        """State tracking fields are updated during bulk compilation."""
        f1 = tmp_path / "test1.ivy"
        f1.write_text("#lang ivy1.8\ntype t\n")

        ir1 = CompiledModuleIR(success=True, source_file=str(f1))

        server = self._make_server(
            test_files=[str(f1)],
            compile_results={str(f1): ir1},
        )

        assert server._bulk_compile_running is False
        assert server._bulk_compile_total == 0
        assert server._bulk_compile_completed == 0

        server._start_bulk_compilation()

        # After synchronous FakeCompilerManager completes all callbacks:
        assert server._bulk_compile_running is False  # all done
        assert server._bulk_compile_total == 1
        assert server._bulk_compile_completed == 1

    def test_progress_callback_lifecycle(self, tmp_path):
        """$/progress begin/report/end lifecycle is followed."""
        f1 = tmp_path / "test1.ivy"
        f1.write_text("#lang ivy1.8\ntype t\n")

        ir1 = CompiledModuleIR(success=True, source_file=str(f1))

        server = self._make_server(
            test_files=[str(f1)],
            compile_results={str(f1): ir1},
        )
        server._start_bulk_compilation()

        wdp = server.work_done_progress
        # create -> begin -> end (1 file: completed==total triggers end)
        assert wdp.create.call_count == 1
        assert wdp.begin.call_count == 1
        assert wdp.end.call_count == 1
