"""Tests for bulk compilation delegation in the server.

The actual compilation logic now lives in ``AnalysisPipeline.run_bulk_tier3``
(tested in ``test_analysis_pipeline.py``).  These tests verify the
server-level wrapper ``_start_bulk_compilation_via_pipeline`` that:

* validates preconditions (pipeline, indexer, compiler_manager)
* checks the IVY_LSP_BULK_COMPILE env var
* collects test files from the ScopedRequirementModel
* delegates to pipeline.run_bulk_tier3
* sends ``ivy/compilationProgress`` notifications via ``_send_compilation_progress``
"""
from __future__ import annotations

import os
import threading
import types
from unittest.mock import MagicMock, patch

from ivy_lsp.compilation.ir import ActionIR, CompiledModuleIR
from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline
from ivy_lsp.semantic.model import SemanticModel


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

    def get_stats(self):
        return {"cachedFiles": len(self._results), "activeProcesses": 0, "maxConcurrent": 1}

    def shutdown(self):
        pass


class FakeIndexer:
    """Minimal indexer stub."""

    def __init__(self, graph):
        self.requirement_graph = graph


class _NullAdapter:
    """No-op adapter for parser/enrichment/compiler."""

    def parse(self, source, filepath):
        return None

    def enrich(self, parse_result, source, filepath):
        return None

    def compile(self, source, filepath):
        from ivy_lsp.adapters.protocols import CompileResult
        return CompileResult(success=True, errors=[])


class TestBulkCompilationViaPipeline:
    """Test _start_bulk_compilation_via_pipeline on the server."""

    def _make_server(self, test_files=None, compile_results=None):
        """Build a minimal server-like object for testing."""
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        if test_files:
            for tf in test_files:
                scope = TestScope(
                    test_file=tf,
                    include_closure=frozenset([tf]),
                    exported_actions=frozenset(["ext:send"]),
                    imported_actions=frozenset(),
                    tester_role="client",
                )
                graph.register_test_scope(scope)

        fake_mgr = FakeCompilerManager(compile_results or {})

        adapter = _NullAdapter()
        model = SemanticModel()

        class Server:
            pass

        server = Server()
        server._compiler_manager = fake_mgr
        server._indexer = FakeIndexer(graph)
        server._semantic_model = model
        server._bulk_analysis_cancel = threading.Event()
        server._shutdown_event = threading.Event()

        # Mock protocol for notification tests
        server.protocol = MagicMock()
        server.work_done_progress = MagicMock()

        # Bind the actual server methods
        from ivy_lsp.server import IvyLanguageServer

        server._start_bulk_compilation_via_pipeline = types.MethodType(
            IvyLanguageServer._start_bulk_compilation_via_pipeline, server
        )
        server._send_compilation_progress = types.MethodType(
            IvyLanguageServer._send_compilation_progress, server
        )
        server._make_progress_callback = types.MethodType(
            IvyLanguageServer._make_progress_callback, server
        )
        server._send_model_ready_notification = lambda: None

        # Create pipeline with notification callback wired to server
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=adapter,
            enrichment_adapter=adapter,
            compiler_adapter=adapter,
            compiler_manager=fake_mgr,
            requirement_graph=graph,
            notification_callback=server._send_compilation_progress,
        )
        server._analysis_pipeline = pipeline

        return server

    def test_no_pipeline_is_noop(self):
        server = self._make_server()
        server._analysis_pipeline = None
        # Should not raise
        server._start_bulk_compilation_via_pipeline()

    def test_no_indexer_is_noop(self):
        server = self._make_server()
        server._indexer = None
        server._start_bulk_compilation_via_pipeline()

    def test_no_compiler_manager_is_noop(self):
        server = self._make_server()
        server._compiler_manager = None
        server._start_bulk_compilation_via_pipeline()

    def test_no_test_files_is_noop(self):
        server = self._make_server(test_files=[])
        server._start_bulk_compilation_via_pipeline()
        assert len(server._compiler_manager.compile_calls) == 0

    def test_compiles_each_test_file(self, tmp_path):
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
        server._start_bulk_compilation_via_pipeline()
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
        server._start_bulk_compilation_via_pipeline()
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
        server._start_bulk_compilation_via_pipeline()
        assert len(server._compiler_manager.compile_calls) == 0

    def test_missing_file_skipped(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist.ivy")
        server = self._make_server(
            test_files=[nonexistent],
            compile_results={},
        )
        server._start_bulk_compilation_via_pipeline()
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
        server._start_bulk_compilation_via_pipeline()

        # The notification_callback sends ivy/compilationProgress
        notify_calls = [
            c for c in server.protocol.notify.call_args_list
            if c[0][0] == "ivy/compilationProgress"
        ]
        assert len(notify_calls) >= 1  # At least the final notification

        # Check final notification payload
        last_payload = notify_calls[-1][0][1]
        assert last_payload["total"] == 2
        assert last_payload["completed"] == 2

    def test_bulk_compile_state_tracking(self, tmp_path):
        """Pipeline state tracking fields are updated during bulk compilation."""
        f1 = tmp_path / "test1.ivy"
        f1.write_text("#lang ivy1.8\ntype t\n")

        ir1 = CompiledModuleIR(success=True, source_file=str(f1))

        server = self._make_server(
            test_files=[str(f1)],
            compile_results={str(f1): ir1},
        )

        pipeline = server._analysis_pipeline
        state_before = pipeline.get_pipeline_state()
        assert state_before["bulkCompileRunning"] is False
        assert state_before["bulkCompileTotal"] == 0
        assert state_before["bulkCompileCompleted"] == 0

        server._start_bulk_compilation_via_pipeline()

        state_after = pipeline.get_pipeline_state()
        assert state_after["bulkCompileRunning"] is False
        assert state_after["bulkCompileTotal"] == 1
        assert state_after["bulkCompileCompleted"] == 1
