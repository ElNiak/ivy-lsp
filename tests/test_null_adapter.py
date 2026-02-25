"""Tests for null (no-op) adapter implementations."""

import pytest

from ivy_lsp.adapters.null_adapter import (
    NullAstEnrichmentAdapter,
    NullCompilerAdapter,
    NullParserAdapter,
)


class TestNullParserAdapter:
    def test_parse_returns_failure(self):
        adapter = NullParserAdapter()
        result = adapter.parse("action foo = {}", "test.ivy")
        assert result.success is False
        assert result.ast is None
        assert result.errors == []
        assert result.filename == "test.ivy"


class TestNullAstEnrichmentAdapter:
    def test_extract_type_info_returns_empty(self):
        adapter = NullAstEnrichmentAdapter()
        result = adapter.extract_type_info(None, "test.ivy", "")
        assert result == []


class TestNullCompilerAdapter:
    def test_compile_returns_failure(self):
        adapter = NullCompilerAdapter()
        result = adapter.compile("action foo = {}", "test.ivy")
        assert result.success is False
        assert result.errors == []
        assert result.module_snapshot is None
        assert result.signature_snapshot is None
