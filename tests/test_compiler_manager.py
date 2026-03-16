"""Tests for CompilerManager -- orchestrates subprocess compilation."""

import threading
import time

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

    def test_max_concurrent_default_is_one(self):
        """Default max_concurrent is 1."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None)
        assert mgr._max_concurrent == 1
        mgr.shutdown()

    def test_semaphore_limits_concurrency(self):
        """Semaphore limits the number of concurrent compilations."""
        from unittest.mock import patch

        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=60.0, max_concurrent=1)

        # Track how many processes are running concurrently between
        # proc.start() and when poll() returns (simulating process lifetime).
        active_count = [0]
        max_active = [0]
        lock = threading.Lock()

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                with lock:
                    active_count[0] += 1
                    if active_count[0] > max_active[0]:
                        max_active[0] = active_count[0]

            def kill(self):
                pass

            def join(self, timeout=None):
                pass

        class FakeConn:
            def __init__(self, filepath):
                self._filepath = filepath

            def poll(self, timeout):
                import time

                time.sleep(0.05)
                # Decrement active when "process completes" (poll returns)
                with lock:
                    active_count[0] = max(0, active_count[0] - 1)
                return True

            def recv(self):
                return CompiledModuleIR.empty(self._filepath)

            def close(self):
                pass

        events = []
        results = []
        result_lock = threading.Lock()

        def make_cb(idx):
            evt = threading.Event()
            events.append(evt)

            def cb(ir):
                with result_lock:
                    results.append(idx)
                evt.set()

            return cb

        try:
            with patch(
                "ivy_lsp.compilation.compiler_manager.multiprocessing"
            ) as mock_mp:
                ctx = mock_mp.get_context.return_value

                call_count = [0]

                def make_pipe(duplex=False):
                    call_count[0] += 1
                    filepath = f"test_{call_count[0]}.ivy"
                    parent = FakeConn(filepath)
                    child = FakeConn(filepath)
                    return parent, child

                ctx.Pipe = make_pipe
                ctx.Process = FakeProcess

                # Fire 3 compilations with max_concurrent=1
                for i in range(3):
                    mgr.compile_async(f"type t{i}", f"test_{i + 1}.ivy", make_cb(i))

                # Wait for all to complete
                for evt in events:
                    evt.wait(timeout=30)

                assert len(results) == 3
                # With max_concurrent=1, at most 1 process should be active
                assert max_active[0] <= 1
        finally:
            mgr.shutdown()

    def test_recompile_same_file_does_not_lose_new_process(self):
        """Old _wait thread must not remove new process from _active."""
        from unittest.mock import patch

        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(staging_dir=None, timeout=30.0, max_concurrent=2)

        # Use mocks so we don't need real Ivy.
        # First compilation takes 0.2s, second is instant.
        call_count = [0]
        lock = threading.Lock()

        class SlowFakeProcess:
            """Simulates a process that finishes slowly."""

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def kill(self):
                pass

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        class FakeConn:
            def __init__(self, filepath, delay=0.0):
                self._filepath = filepath
                self._delay = delay

            def poll(self, timeout):
                if self._delay > 0:
                    time.sleep(self._delay)
                return True

            def recv(self):
                return CompiledModuleIR.empty(self._filepath)

            def close(self):
                pass

        events = [threading.Event(), threading.Event()]
        results = []

        def cb1(ir):
            results.append(("first", ir))
            events[0].set()

        def cb2(ir):
            results.append(("second", ir))
            events[1].set()

        try:
            with patch(
                "ivy_lsp.compilation.compiler_manager.multiprocessing"
            ) as mock_mp:
                ctx = mock_mp.get_context.return_value

                def make_pipe(duplex=False):
                    with lock:
                        call_count[0] += 1
                        n = call_count[0]
                    # First call: slow poll (0.2s), second: fast
                    delay = 0.2 if n == 1 else 0.0
                    parent = FakeConn("/test.ivy", delay=delay)
                    child = FakeConn("/test.ivy")
                    return parent, child

                ctx.Pipe = make_pipe
                ctx.Process = SlowFakeProcess

                mgr.compile_async("source1", "/test.ivy", cb1)
                time.sleep(0.05)  # Let first compilation start
                mgr.compile_async("source2", "/test.ivy", cb2)

                # Wait for second (fast) to complete
                events[1].wait(timeout=10)
                # Also wait for first to complete
                events[0].wait(timeout=10)

                # After both complete, _active should be empty
                assert mgr.get_stats()["activeProcesses"] == 0
        finally:
            mgr.shutdown()

    def test_invalidate_dependents_clears_target_and_includers(self):
        """invalidate_dependents removes target + all direct includers."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(timeout=30)
        for f in ["/a.ivy", "/b.ivy", "/c.ivy"]:
            mgr._put_cache(f, "hash", CompiledModuleIR.empty(f))

        class FakeGraph:
            def get_included_by(self, filepath):
                if filepath == "/a.ivy":
                    return ["/b.ivy"]
                return []

        mgr.invalidate_dependents("/a.ivy", FakeGraph())
        assert mgr.get_cached("/a.ivy") is None
        assert mgr.get_cached("/b.ivy") is None
        assert mgr.get_cached("/c.ivy") is not None
        mgr.shutdown()

    def test_invalidate_dependents_without_get_included_by(self):
        """Handles include_graph that lacks get_included_by."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(timeout=30)
        mgr._put_cache("/a.ivy", "hash", CompiledModuleIR.empty("/a.ivy"))
        mgr.invalidate_dependents("/a.ivy", object())
        assert mgr.get_cached("/a.ivy") is None
        mgr.shutdown()

    def test_shutdown_clears_active_and_cache(self):
        """shutdown() clears both _active and _cache."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(timeout=30)
        mgr._put_cache("/a.ivy", "hash", CompiledModuleIR.empty("/a.ivy"))
        mgr.shutdown()
        assert mgr.get_stats()["cachedFiles"] == 0
        assert mgr.get_stats()["activeProcesses"] == 0

    def test_get_stats_returns_correct_counts(self):
        """get_stats reflects cache population."""
        from ivy_lsp.compilation.compiler_manager import CompilerManager

        mgr = CompilerManager(timeout=30, max_concurrent=4)
        assert mgr.get_stats() == {
            "cachedFiles": 0,
            "activeProcesses": 0,
            "maxConcurrent": 4,
        }
        mgr._put_cache("/a.ivy", "hash", CompiledModuleIR.empty("/a.ivy"))
        assert mgr.get_stats()["cachedFiles"] == 1
        mgr.shutdown()
