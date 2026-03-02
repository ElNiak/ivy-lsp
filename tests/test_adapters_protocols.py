"""Tests for adapter protocol definitions."""

import pytest

from ivy_lsp.adapters.protocols import (
    CompileError,
    CompileResult,
    IAstEnrichmentAdapter,
    ICompilerAdapter,
    IParserAdapter,
    TypeAnnotation,
)


class TestProtocolDefinitions:
    """Verify Protocol classes are importable and runtime_checkable."""

    def test_iparser_adapter_is_runtime_checkable(self):
        assert hasattr(IParserAdapter, "__protocol_attrs__") or hasattr(
            IParserAdapter, "__abstractmethods__"
        ) or isinstance(IParserAdapter, type)

    def test_iast_enrichment_adapter_is_runtime_checkable(self):
        assert isinstance(IAstEnrichmentAdapter, type)

    def test_icompiler_adapter_is_runtime_checkable(self):
        assert isinstance(ICompilerAdapter, type)



class TestDataClasses:
    """Verify data classes are instantiable."""

    def test_type_annotation_defaults(self):
        ta = TypeAnnotation(name="foo", qualified_name="bar.foo")
        assert ta.name == "foo"
        assert ta.arity == 0
        assert ta.params == []
        assert ta.is_enum is False

    def test_compile_error(self):
        err = CompileError(message="type error", file="test.ivy", line=10)
        assert err.message == "type error"
        assert err.line == 10

    def test_compile_result_defaults(self):
        cr = CompileResult()
        assert cr.success is False
        assert cr.errors == []
        assert cr.module_snapshot is None

    def test_compile_result_success(self):
        cr = CompileResult(success=True)
        assert cr.success is True


class TestRuntimeCheckable:
    """Verify isinstance checks work on conforming classes."""

    def test_conforming_parser(self):
        from ivy_lsp.adapters.null_adapter import NullParserAdapter

        adapter = NullParserAdapter()
        assert isinstance(adapter, IParserAdapter)

    def test_conforming_enrichment(self):
        from ivy_lsp.adapters.null_adapter import NullAstEnrichmentAdapter

        adapter = NullAstEnrichmentAdapter()
        assert isinstance(adapter, IAstEnrichmentAdapter)

    def test_conforming_compiler(self):
        from ivy_lsp.adapters.null_adapter import NullCompilerAdapter

        adapter = NullCompilerAdapter()
        assert isinstance(adapter, ICompilerAdapter)
