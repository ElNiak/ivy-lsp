"""Tests for SymbolReference extraction from Ivy source."""

from __future__ import annotations

import pytest

from ivy_lsp.parsing.symbols import SymbolReference


class TestSymbolReference:
    @pytest.mark.unit
    def test_create_call_reference(self):
        ref = SymbolReference(
            source_name="process",
            target_name="connect",
            kind="call",
            line=7,
        )
        assert ref.source_name == "process"
        assert ref.target_name == "connect"
        assert ref.kind == "call"
        assert ref.line == 7
        assert ref.col == 0
        assert ref.file_path is None

    @pytest.mark.unit
    def test_create_instance_reference(self):
        ref = SymbolReference(
            source_name="net",
            target_name="tcp_endpoint",
            kind="instance",
            line=12,
            file_path="proto.ivy",
        )
        assert ref.kind == "instance"
        assert ref.file_path == "proto.ivy"

    @pytest.mark.unit
    def test_create_monitor_reference(self):
        ref = SymbolReference(
            source_name="before connect",
            target_name="connect",
            kind="monitor",
            line=10,
        )
        assert ref.kind == "monitor"


class TestEdgeTypes:
    @pytest.mark.unit
    def test_calls_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.CALLS.value == "calls"

    @pytest.mark.unit
    def test_uses_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.USES.value == "uses"

    @pytest.mark.unit
    def test_monitors_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.MONITORS.value == "monitors"

    @pytest.mark.unit
    def test_contains_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.CONTAINS.value == "contains"


def _make_regex_only_extractor():
    """Create a TieredExtractor forced to use Tier 3 (regex only)."""
    from ivy_lsp.parsing.tiered_extractor import TieredExtractor

    ext = TieredExtractor()
    ext._parser_available = False
    ext._lexer_available = False
    return ext


def _make_lexer_only_extractor():
    """Create a TieredExtractor forced to use Tier 2 (lexer only)."""
    from ivy_lsp.parsing.tiered_extractor import TieredExtractor

    ext = TieredExtractor()
    ext._parser_available = False
    return ext


class TestTier3RegexExtraction:
    @pytest.mark.unit
    def test_extracts_call_references(self):
        source = (
            "#lang ivy1.7\n"
            "type cid\n"
            "action connect(src:cid, dst:cid)\n"
            "action process(c:cid) = {\n"
            "    call connect(c, c);\n"
            "}\n"
        )
        ext = _make_regex_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 3
        call_refs = [r for r in result.references if r.kind == "call"]
        assert len(call_refs) >= 1
        assert any(r.target_name == "connect" for r in call_refs)

    @pytest.mark.unit
    def test_extracts_instance_references(self):
        source = (
            "#lang ivy1.7\n" "module endpoint = {\n" "}\n" "instance net : endpoint\n"
        )
        ext = _make_regex_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 3
        inst_refs = [r for r in result.references if r.kind == "instance"]
        assert len(inst_refs) >= 1
        assert any(
            r.target_name == "endpoint" and r.source_name == "net" for r in inst_refs
        )

    @pytest.mark.unit
    def test_extracts_monitor_references(self):
        source = (
            "#lang ivy1.7\n"
            "type cid\n"
            "action connect(src:cid, dst:cid)\n"
            "before connect {\n"
            "    require src ~= dst;\n"
            "}\n"
        )
        ext = _make_regex_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 3
        mon_refs = [r for r in result.references if r.kind == "monitor"]
        assert len(mon_refs) >= 1
        assert any(r.target_name == "connect" for r in mon_refs)

    @pytest.mark.unit
    def test_extraction_result_has_references_field(self):
        from ivy_lsp.parsing.tiered_extractor import ExtractionResult

        result = ExtractionResult()
        assert result.references == []


class TestTier2LexerExtraction:
    @pytest.mark.unit
    def test_lexer_extracts_call_references(self):
        source = (
            "#lang ivy1.7\n"
            "type cid\n"
            "action connect(src:cid, dst:cid)\n"
            "action process(c:cid) = {\n"
            "    call connect(c, c);\n"
            "}\n"
        )
        ext = _make_lexer_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 2
        call_refs = [r for r in result.references if r.kind == "call"]
        assert len(call_refs) >= 1
        assert any(r.target_name == "connect" for r in call_refs)

    @pytest.mark.unit
    def test_lexer_extracts_instance_references(self):
        source = (
            "#lang ivy1.7\n" "module endpoint = {\n" "}\n" "instance net : endpoint\n"
        )
        ext = _make_lexer_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 2
        inst_refs = [r for r in result.references if r.kind == "instance"]
        assert len(inst_refs) >= 1
        assert any(
            r.target_name == "endpoint" and r.source_name == "net" for r in inst_refs
        )

    @pytest.mark.unit
    def test_lexer_extracts_monitor_references(self):
        source = (
            "#lang ivy1.7\n"
            "type cid\n"
            "action connect(src:cid, dst:cid)\n"
            "before connect {\n"
            "    require src ~= dst;\n"
            "}\n"
        )
        ext = _make_lexer_only_extractor()
        result = ext.extract(source, "test.ivy")
        assert result.tier_used == 2
        mon_refs = [r for r in result.references if r.kind == "monitor"]
        assert len(mon_refs) >= 1
        assert any(r.target_name == "connect" for r in mon_refs)
