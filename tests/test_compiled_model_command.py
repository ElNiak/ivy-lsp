"""Tests for the ivy/compiledModel custom command."""

from __future__ import annotations

import asyncio
import functools

from ivy_lsp.core.compilation.ir import (
    ActionIR,
    CompiledModuleIR,
    IsolateIR,
    MixinIR,
    SortIR,
    SymbolIR,
)


class FakeParams:
    """Minimal params object for testing."""

    def __init__(self, uri=None, text_document=None):
        self.uri = uri
        self.textDocument = text_document


class FakeTextDocument:
    def __init__(self, uri):
        self.uri = uri


class FakeCompilerManager:
    """Minimal CompilerManager stub with get_cached."""

    def __init__(self):
        self._cache = {}

    def get_cached(self, filepath):
        return self._cache.get(filepath)


class FakeServer:
    """Minimal server stub for testing commands.register."""

    def __init__(self):
        self.full_mode = False
        self.compiler_manager: object = None
        self._handlers = {}

    def feature(self, name, _options=None):
        def decorator(fn):
            if asyncio.iscoroutinefunction(fn):

                @functools.wraps(fn)
                def _sync(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))

                self._handlers[name] = _sync
            else:
                self._handlers[name] = fn
            return fn

        return decorator

    def command(self, name):
        def decorator(fn):
            self._handlers[name] = fn
            return fn

        return decorator


class TestCompiledModelCommand:
    def _setup_server(self):
        """Register commands and return server + handler."""
        server = FakeServer()
        from ivy_lsp.features.commands import register

        register(server)
        handler = server._handlers.get("ivy/compiledModel")
        assert handler is not None, "ivy/compiledModel not registered"
        return server, handler

    def test_no_params_returns_error(self):
        _, handler = self._setup_server()
        result = handler(None)
        assert result["success"] is False
        assert "No params" in result["error"]

    def test_no_uri_returns_error(self):
        _, handler = self._setup_server()
        result = handler(FakeParams())
        assert result["success"] is False
        assert "No file URI" in result["error"]

    def test_no_compiler_manager_returns_error(self):
        _, handler = self._setup_server()
        result = handler(FakeParams(uri="file:///test.ivy"))
        assert result["success"] is False
        assert "CompilerManager not available" in result["error"]

    def test_no_cached_ir_returns_error(self):
        server, handler = self._setup_server()
        server.compiler_manager = FakeCompilerManager()
        result = handler(FakeParams(uri="file:///test.ivy"))
        assert result["success"] is False
        assert "No cached compilation" in result["error"]
        assert "hint" in result

    def test_returns_cached_ir_as_json(self):
        server, handler = self._setup_server()
        mgr = FakeCompilerManager()
        ir = CompiledModuleIR(
            sorts={
                "pkt_type": SortIR(
                    name="pkt_type",
                    is_enumerated=True,
                    constructors=["initial", "handshake"],
                ),
            },
            symbols={
                "connected": SymbolIR(
                    name="connected",
                    sort_str="cid -> bool",
                    domain_sorts=["cid"],
                    range_sort="bool",
                    is_relation=True,
                ),
            },
            actions={
                "ext:send": ActionIR(
                    name="ext:send",
                    formal_params=["dst:cid"],
                    formal_returns=["ok:bool"],
                    is_exported=True,
                ),
            },
            mixins={
                "ext:send": [
                    MixinIR(mixer="impl.send", mixee="ext:send", kind="before"),
                ],
            },
            isolates={
                "iso_quic": IsolateIR(
                    name="iso_quic",
                    verified_components=["quic_server"],
                    present_components=["quic_server", "net"],
                ),
            },
            success=True,
            source_file="/test.ivy",
            compile_duration=2.5,
        )
        mgr._cache["/test.ivy"] = ir
        server.compiler_manager = mgr
        result = handler(FakeParams(uri="file:///test.ivy"))
        assert result["success"] is True
        assert result["filepath"] == "/test.ivy"
        assert result["compileDuration"] == 2.5
        # sorts
        assert "pkt_type" in result["sorts"]
        assert result["sorts"]["pkt_type"]["isEnumerated"] is True
        assert result["sorts"]["pkt_type"]["constructors"] == [
            "initial",
            "handshake",
        ]
        # symbols
        assert "connected" in result["symbols"]
        assert result["symbols"]["connected"]["isRelation"] is True
        # actions
        assert "ext:send" in result["actions"]
        assert result["actions"]["ext:send"]["isExported"] is True
        # mixins -- flattened from Dict[str, List[MixinIR]]
        assert len(result["mixins"]) == 1
        assert result["mixins"][0]["kind"] == "before"
        assert result["mixins"][0]["mixer"] == "impl.send"
        # isolates
        assert "iso_quic" in result["isolates"]
        assert result["isolates"]["iso_quic"]["verifiedComponents"] == ["quic_server"]

    def test_text_document_param_extraction(self):
        server, handler = self._setup_server()
        mgr = FakeCompilerManager()
        ir = CompiledModuleIR(
            success=True,
            source_file="/test.ivy",
        )
        mgr._cache["/test.ivy"] = ir
        server.compiler_manager = mgr
        # Test textDocument.uri extraction
        result = handler(FakeParams(text_document=FakeTextDocument("file:///test.ivy")))
        assert result["success"] is True

    def test_empty_ir_returns_zero_counts(self):
        server, handler = self._setup_server()
        mgr = FakeCompilerManager()
        ir = CompiledModuleIR(success=True, source_file="/test.ivy")
        mgr._cache["/test.ivy"] = ir
        server.compiler_manager = mgr
        result = handler(FakeParams(uri="file:///test.ivy"))
        assert result["success"] is True
        assert result["axiomCount"] == 0
        assert result["conjectureCount"] == 0
        assert result["requirementCount"] == 0
        assert len(result["sorts"]) == 0
        assert len(result["symbols"]) == 0
        assert len(result["actions"]) == 0
        assert len(result["mixins"]) == 0

    def test_multiple_mixin_groups_flattened(self):
        """Mixins from multiple action keys are flattened into a single list."""
        server, handler = self._setup_server()
        mgr = FakeCompilerManager()
        ir = CompiledModuleIR(
            mixins={
                "action_a": [
                    MixinIR(mixer="m1", mixee="action_a", kind="before"),
                ],
                "action_b": [
                    MixinIR(mixer="m2", mixee="action_b", kind="after"),
                    MixinIR(mixer="m3", mixee="action_b", kind="before"),
                ],
            },
            success=True,
            source_file="/test.ivy",
        )
        mgr._cache["/test.ivy"] = ir
        server.compiler_manager = mgr
        result = handler(FakeParams(uri="file:///test.ivy"))
        assert result["success"] is True
        assert len(result["mixins"]) == 3
        mixer_names = {m["mixer"] for m in result["mixins"]}
        assert mixer_names == {"m1", "m2", "m3"}


class TestCapabilitiesCompiledModel:
    def test_compiled_model_available_false_without_manager(self):
        server = FakeServer()
        from ivy_lsp.features.commands import register

        register(server)
        handler = server._handlers["ivy/capabilities"]
        result = handler()
        assert result["compiledModelAvailable"] is False

    def test_compiled_model_available_true_with_manager(self):
        server = FakeServer()
        server.compiler_manager = FakeCompilerManager()
        from ivy_lsp.features.commands import register

        register(server)
        handler = server._handlers["ivy/capabilities"]
        result = handler()
        assert result["compiledModelAvailable"] is True
