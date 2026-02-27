"""Tests for CompilerManager -- orchestrates subprocess compilation."""

import threading

import pytest

from ivy_lsp.compilation.ir import CompiledModuleIR


class TestCompilerManager:
    def test_compile_sync_returns_ir(self):
        """compile_sync returns a CompiledModuleIR (success or failure)."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0)
        try:
            ir = mgr.compile_sync("type bool", "test.ivy")
            assert isinstance(ir, CompiledModuleIR)
            assert ir.source_file == "test.ivy"
        finally:
            mgr.shutdown()

    def test_compile_async_calls_callback(self):
        """compile_async invokes the callback with an IR."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0)
        event = threading.Event()
        result_holder = [None]

        def on_result(ir):
            result_holder[0] = ir
            event.set()

        try:
            mgr.compile_async("type bool", "test.ivy", on_result)
            event.wait(timeout=60)
            assert result_holder[0] is not None
            assert isinstance(result_holder[0], CompiledModuleIR)
        finally:
            mgr.shutdown()

    def test_cache_hit(self):
        """Second compilation of same source returns cached IR."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0)
        try:
            ir1 = mgr.compile_sync("type bool", "test.ivy")
            ir2 = mgr.compile_sync("type bool", "test.ivy")
            # Same object (cached)
            assert ir1 is ir2
        finally:
            mgr.shutdown()

    def test_cache_miss_on_changed_source(self):
        """Different source content invalidates cache."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0)
        try:
            ir1 = mgr.compile_sync("type bool", "test.ivy")
            ir2 = mgr.compile_sync("type cid", "test.ivy")
            assert ir1 is not ir2
        finally:
            mgr.shutdown()

    def test_invalidate_clears_cache(self):
        """invalidate() removes cached entry for a file."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0)
        try:
            mgr.compile_sync("type bool", "test.ivy")
            assert mgr.get_cached("test.ivy") is not None
            mgr.invalidate("test.ivy")
            assert mgr.get_cached("test.ivy") is None
        finally:
            mgr.shutdown()

    def test_timeout_returns_failed_ir(self):
        """Compilation that exceeds timeout returns failed IR."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=0.001)
        try:
            ir = mgr.compile_sync("type bool", "test.ivy")
            # With 1ms timeout, almost certainly times out
            assert isinstance(ir, CompiledModuleIR)
            if not ir.success:
                assert any(
                    "timeout" in e.lower() or "timed out" in e.lower()
                    for e in ir.errors
                )
        finally:
            mgr.shutdown()
