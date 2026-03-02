"""Tests for the compiler adapter and snapshots."""

from ivy_lsp.adapters.compiler_adapter import CompilerAdapter, CompilerSession
from ivy_lsp.adapters.protocols import CompileResult
from ivy_lsp.semantic.snapshots import (
    ModuleSnapshot,
    SignatureSnapshot,
    SortInfo,
    SymbolInfo,
)


class TestSnapshots:
    def test_sort_info_defaults(self):
        s = SortInfo(name="bool")
        assert s.arity == 0
        assert s.params == []
        assert s.is_uninterpreted is False

    def test_symbol_info_defaults(self):
        s = SymbolInfo(name="foo")
        assert s.sort is None
        assert s.is_action is False

    def test_signature_snapshot_defaults(self):
        ss = SignatureSnapshot()
        assert ss.sorts == {}
        assert ss.symbols == {}

    def test_module_snapshot_defaults(self):
        ms = ModuleSnapshot()
        assert ms.signature is None
        assert ms.axioms == []


class TestCompilerAdapter:
    """Tests that don't require ivy/z3 to be installed."""

    def test_compile_without_ivy_returns_failure(self):
        """When ivy not installed, compile returns graceful failure."""
        adapter = CompilerAdapter()
        # This will fail with ImportError for ivy.ivy_compiler
        # which is expected - we verify it doesn't crash
        result = adapter.compile("#lang ivy1.7\ntype t", "test.ivy")
        assert isinstance(result, CompileResult)
        # Either succeeds (ivy available) or fails gracefully
        if not result.success:
            assert len(result.errors) > 0


class TestBackgroundCallbackGuarantee:
    """C7+H7: callback must fire even when compile() raises."""

    def test_callback_fires_on_compile_exception(self):
        import threading

        adapter = CompilerAdapter()
        results = []
        event = threading.Event()

        def capture(r):
            results.append(r)
            event.set()

        # Monkey-patch compile to raise
        def exploding_compile(source, filename):
            raise RuntimeError("compile crash")

        adapter.compile = exploding_compile

        adapter.compile_background("#lang ivy1.7", "test.ivy", callback=capture)
        event.wait(timeout=5)

        assert len(results) == 1, "callback must be invoked exactly once"
        assert not results[0].success
        assert any("compile crash" in e.message for e in results[0].errors)

    def test_callback_fires_once_on_success(self):
        import threading

        adapter = CompilerAdapter()
        results = []
        event = threading.Event()

        def capture(r):
            results.append(r)
            event.set()

        # Monkey-patch compile to succeed
        adapter.compile = lambda src, fn: CompileResult(success=True)

        adapter.compile_background("#lang ivy1.7", "test.ivy", callback=capture)
        event.wait(timeout=5)

        assert len(results) == 1, "callback must be invoked exactly once"
        assert results[0].success


class TestCompilerSession:
    def test_session_context_manager(self):
        """CompilerSession can enter/exit without crashing regardless of ivy availability."""
        try:
            with CompilerSession():
                pass
        except ImportError:
            # ivy not available - that's fine, session should still work
            pass
