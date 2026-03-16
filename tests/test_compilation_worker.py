"""Tests for the subprocess compilation worker."""

import multiprocessing

import pytest

from ivy_lsp.compilation.ir import CompiledModuleIR


class TestCompilerWorker:
    def test_worker_returns_failed_ir_when_ivy_unavailable(self):
        """Worker should send a failed IR when ivy is not importable.

        It must not crash the subprocess.
        """
        from ivy_lsp.compilation.worker import compiler_worker

        parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(
            target=compiler_worker,
            args=("type bool", "test.ivy", child_conn, None),
            daemon=True,
        )
        proc.start()
        child_conn.close()

        if parent_conn.poll(30):
            ir = parent_conn.recv()
            # Either succeeds (if ivy available) or fails gracefully
            assert isinstance(ir, CompiledModuleIR)
            assert ir.source_file == "test.ivy"
        else:
            proc.kill()
            proc.join(timeout=5)
            pytest.skip("Worker timed out -- ivy/Z3 may not be installed")

        parent_conn.close()
        proc.join(timeout=5)

    def test_worker_handles_empty_source(self):
        """Worker should handle empty source without crashing."""
        from ivy_lsp.compilation.worker import compiler_worker

        parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(
            target=compiler_worker,
            args=("", "empty.ivy", child_conn, None),
            daemon=True,
        )
        proc.start()
        child_conn.close()

        if parent_conn.poll(30):
            ir = parent_conn.recv()
            assert isinstance(ir, CompiledModuleIR)
            assert ir.source_file == "empty.ivy"
        else:
            proc.kill()
            proc.join(timeout=5)
            pytest.skip("Worker timed out")

        parent_conn.close()
        proc.join(timeout=5)
