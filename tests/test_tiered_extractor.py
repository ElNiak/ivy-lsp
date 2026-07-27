"""Tests for ivy_lsp.core.parsing.tiered_extractor and symbol_to_model."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from lsprotocol.types import SymbolKind

from ivy_lsp.core.parsing.symbols import IvySymbol
from ivy_lsp.core.parsing.tiered_extractor import (
    ExtractionResult,
    TieredExtractor,
    TierError,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "helpers", "fixtures")


def _read_fixture(name: str) -> str:
    path = os.path.join(FIXTURES_DIR, name)
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------


class TestExtractionResult:
    def test_empty_result(self):
        r = ExtractionResult()
        assert r.symbols == []
        assert r.includes == []
        assert r.tier_used == 0
        assert r.symbol_count == 0

    def test_symbol_count_with_children(self):
        child = IvySymbol(name="inner", kind=SymbolKind.Variable, range=(0, 0, 0, 5))
        parent = IvySymbol(
            name="outer", kind=SymbolKind.Module, range=(0, 0, 0, 10), children=[child]
        )
        r = ExtractionResult(symbols=[parent], tier_used=2)
        assert r.symbol_count == 2


# ---------------------------------------------------------------------------
# TieredExtractor — Tier 3 (regex, always available)
# ---------------------------------------------------------------------------


class TestTier3Regex:
    """Tests that run with both parser and lexer mocked as unavailable."""

    def _make_extractor(self) -> TieredExtractor:
        ext = TieredExtractor()
        ext._parser_available = False
        ext._lexer_available = False
        return ext

    def test_empty_source(self):
        ext = self._make_extractor()
        result = ext.extract("", "/test.ivy")
        assert result.tier_used == 0
        assert result.symbols == []
        assert result.includes == []

    def test_whitespace_only(self):
        ext = self._make_extractor()
        result = ext.extract("   \n  \n  ", "/test.ivy")
        assert result.tier_used == 0

    def test_type_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\ntype cid\ntype pkt_num\n"
        result = ext.extract(source, "/test.ivy")
        assert result.tier_used == 3
        names = [s.name for s in result.symbols]
        assert "cid" in names
        assert "pkt_num" in names

    def test_enum_type_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\ntype stream_kind = {unidir, bidir}\n"
        result = ext.extract(source, "/test.ivy")
        assert result.tier_used == 3
        sym = result.symbols[0]
        assert sym.name == "stream_kind"
        assert sym.kind == SymbolKind.Class
        assert "enum" in (sym.detail or "")

    def test_action_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\naction send(src:cid, dst:cid) returns (ok:bool)\n"
        result = ext.extract(source, "/test.ivy")
        assert result.tier_used == 3
        sym = result.symbols[0]
        assert sym.name == "send"
        assert sym.kind == SymbolKind.Function

    def test_relation_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nrelation connected(X:cid, Y:cid)\n"
        result = ext.extract(source, "/test.ivy")
        sym = result.symbols[0]
        assert sym.name == "connected"
        assert sym.kind == SymbolKind.Function

    def test_function_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nfunction last_pkt(C:cid) : pkt_num\n"
        result = ext.extract(source, "/test.ivy")
        sym = result.symbols[0]
        assert sym.name == "last_pkt"
        assert sym.kind == SymbolKind.Function

    def test_individual_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nindividual the_cid : cid\n"
        result = ext.extract(source, "/test.ivy")
        sym = result.symbols[0]
        assert sym.name == "the_cid"
        assert sym.kind == SymbolKind.Variable

    def test_object_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nobject frame = {\n    type this\n}\n"
        result = ext.extract(source, "/test.ivy")
        sym = [s for s in result.symbols if s.name == "frame"][0]
        assert sym.kind == SymbolKind.Module

    def test_module_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nmodule array(domain, range) = {\n}\n"
        result = ext.extract(source, "/test.ivy")
        sym = [s for s in result.symbols if s.name == "array"][0]
        assert sym.kind == SymbolKind.Module

    def test_isolate_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\nisolate test_iso = {\n}\n"
        result = ext.extract(source, "/test.ivy")
        sym = [s for s in result.symbols if s.name == "test_iso"][0]
        assert sym.kind == SymbolKind.Namespace

    def test_include_extraction(self):
        ext = self._make_extractor()
        source = "#lang ivy1.7\n\ninclude quic_types\ninclude quic_frame\n"
        result = ext.extract(source, "/test.ivy")
        assert "quic_types" in result.includes
        assert "quic_frame" in result.includes

    def test_multi_type_fixture(self):
        ext = self._make_extractor()
        source = _read_fixture("multi_type.ivy")
        result = ext.extract(source, os.path.join(FIXTURES_DIR, "multi_type.ivy"))
        assert result.tier_used == 3
        names = [s.name for s in result.symbols]
        assert "cid" in names
        assert "pkt_num" in names
        assert "stream_kind" in names
        assert "connected" in names
        assert "last_pkt" in names
        assert "the_cid" in names
        assert "send" in names
        assert "recv" in names
        assert result.symbol_count == 8


# ---------------------------------------------------------------------------
# TieredExtractor — Tier 2 (lexer)
# ---------------------------------------------------------------------------


class TestTier2Lexer:
    """Tests with parser unavailable, lexer available."""

    def _make_extractor(self) -> TieredExtractor:
        ext = TieredExtractor()
        ext._parser_available = False
        # lexer_available stays None (will be checked)
        return ext

    def test_lexer_extracts_symbols(self):
        ext = self._make_extractor()
        source = _read_fixture("multi_type.ivy")
        result = ext.extract(source, os.path.join(FIXTURES_DIR, "multi_type.ivy"))
        assert result.tier_used == 2
        names = [s.name for s in result.symbols]
        assert "cid" in names
        assert "send" in names
        assert "connected" in names

    def test_lexer_extracts_includes(self):
        ext = self._make_extractor()
        source = _read_fixture("with_include/conn.ivy")
        result = ext.extract(
            source, os.path.join(FIXTURES_DIR, "with_include", "conn.ivy")
        )
        assert result.tier_used == 2
        assert "types" in result.includes

    def test_lexer_falls_back_on_import_error(self):
        ext = self._make_extractor()
        with patch(
            "ivy_lsp.core.parsing.tiered_extractor.TieredExtractor._try_lexer",
            side_effect=ImportError("no PLY"),
        ):
            source = "#lang ivy1.7\n\ntype cid\n"
            result = ext.extract(source, "/test.ivy")
            assert result.tier_used == 3  # fell through to regex
            assert any(
                e.tier == 2 and e.error_type == "ImportError" for e in result.errors
            )


# ---------------------------------------------------------------------------
# TieredExtractor — Cascade behavior
# ---------------------------------------------------------------------------


class TestCascade:
    def test_import_error_caching(self):
        ext = TieredExtractor()
        ext._parser_available = False
        # Force lexer unavailable on first call
        ext._lexer_available = False
        source = "#lang ivy1.7\n\ntype cid\n"
        result = ext.extract(source, "/test.ivy")
        assert result.tier_used == 3
        # Verify flags are cached
        assert ext._parser_available is False
        assert ext._lexer_available is False

    def test_errors_accumulated(self):
        ext = TieredExtractor()
        # Force both higher tiers to fail
        ext._parser_available = False
        ext._lexer_available = False
        source = "#lang ivy1.7\n\ntype cid\n"
        result = ext.extract(source, "/test.ivy")
        # No errors since both tiers were skipped via cached flags
        assert result.tier_used == 3

    def test_timing_is_positive(self):
        ext = TieredExtractor()
        ext._parser_available = False
        ext._lexer_available = False
        source = "#lang ivy1.7\n\ntype cid\n"
        result = ext.extract(source, "/test.ivy")
        assert result.timing_ms >= 0.0


# ---------------------------------------------------------------------------
# populate_model_from_symbols
# ---------------------------------------------------------------------------


class TestPopulateModelFromSymbols:
    def test_type_node_created(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import TypeNode

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="cid",
                kind=SymbolKind.Class,
                range=(2, 0, 2, 8),
                detail="type",
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy", tier_used=2)
        assert count == 1
        nodes = list(model.get_nodes_by_type(TypeNode))
        assert len(nodes) == 1
        assert nodes[0].name == "cid"
        assert nodes[0].tier == "tier2"

    def test_enum_type_node(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import TypeNode

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="stream_kind",
                kind=SymbolKind.Class,
                range=(4, 0, 4, 35),
                detail="enum: unidir, bidir",
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy", tier_used=3)
        assert count == 1
        nodes = list(model.get_nodes_by_type(TypeNode))
        assert nodes[0].is_enum is True
        assert "unidir" in nodes[0].variants
        assert "bidir" in nodes[0].variants

    def test_action_symbol_node(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="send",
                kind=SymbolKind.Function,
                range=(10, 0, 10, 40),
                detail="action",
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy", tier_used=2)
        assert count == 1
        nodes = list(model.get_nodes_by_type(SymbolNode))
        assert len(nodes) == 1
        assert nodes[0].kind == "action"

    def test_individual_symbol_node(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="the_cid",
                kind=SymbolKind.Variable,
                range=(8, 0, 8, 22),
                detail=": cid",
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy", tier_used=3)
        assert count == 1
        nodes = list(model.get_nodes_by_type(SymbolNode))
        assert nodes[0].kind == "individual"
        assert nodes[0].sort_name == "cid"

    def test_object_symbol_node(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="frame",
                kind=SymbolKind.Module,
                range=(5, 0, 5, 20),
                detail="object",
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy", tier_used=2)
        assert count == 1
        nodes = list(model.get_nodes_by_type(SymbolNode))
        assert nodes[0].kind == "object"

    def test_skips_non_model_symbols(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel

        model = SemanticModel()
        syms = [
            IvySymbol(
                name="my_prop",
                kind=SymbolKind.Property,
                range=(0, 0, 0, 10),
                file_path="/test.ivy",
            ),
        ]
        count = populate_model_from_symbols(model, syms, "/test.ivy")
        assert count == 0

    def test_nested_children(self):
        from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        model = SemanticModel()
        child = IvySymbol(
            name="this",
            kind=SymbolKind.Class,
            range=(6, 0, 6, 15),
            detail="type",
            file_path="/test.ivy",
        )
        parent = IvySymbol(
            name="frame",
            kind=SymbolKind.Module,
            range=(5, 0, 5, 20),
            detail="object",
            file_path="/test.ivy",
            children=[child],
        )
        count = populate_model_from_symbols(model, [parent], "/test.ivy", tier_used=2)
        assert count == 2  # parent + child
        type_nodes = list(model.get_nodes_by_type(TypeNode))
        assert len(type_nodes) == 1
        assert type_nodes[0].qualified_name == "frame.this"
