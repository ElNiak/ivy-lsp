"""Tests for enhanced hover with SemanticModel (Task 12)."""

from unittest.mock import MagicMock

from lsprotocol import types as lsp

from ivy_lsp.core.parsing.symbols import IvySymbol
from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode
from ivy_lsp.features.hover import (
    _enrich_with_semantic_model,
    format_hover_content,
    get_hover_info,
)


class TestEnrichWithSemanticModel:
    def test_returns_original_when_no_model(self):
        result = _enrich_with_semantic_model(
            "base content",
            "foo",
            "test.ivy",
            lsp.Position(0, 0),
            ["foo"],
            None,
        )
        assert result == "base content"

    def test_adds_rfc_requirement_text(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="test.ivy:2:0",
            file="test.ivy",
            line=2,
            tags=["rfc9000:4.1"],
        )
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data beyond limit",
            level="MUST",
        )
        model.add_node(ann)
        model.add_node(req)

        result = _enrich_with_semantic_model(
            "base",
            "require",
            "test.ivy",
            lsp.Position(2, 0),
            ["require x > 0;  # [rfc9000:4.1]"],
            model,
        )
        assert "RFC9000" in result
        assert "4.1" in result
        assert "MUST" in result

    def test_adds_symbol_params(self):
        model = SemanticModel()
        sn = SymbolNode(
            id="test.ivy:5:send",
            name="send",
            qualified_name="quic.send",
            kind="action",
            file="test.ivy",
            line=5,
            params=["dst:endpoint", "pkt:packet"],
            return_sort="bool",
        )
        model.add_node(sn)

        result = _enrich_with_semantic_model(
            "base",
            "send",
            "test.ivy",
            lsp.Position(5, 0),
            ["action send(...)"],
            model,
        )
        assert "dst:endpoint" in result
        assert "bool" in result

    def test_adds_cross_reference_count(self):
        model = SemanticModel()
        from ivy_lsp.core.semantic.edges import SemanticEdgeType

        sn = SymbolNode(
            id="test.ivy:5:foo",
            name="foo",
            qualified_name="foo",
            kind="action",
            file="test.ivy",
            line=5,
        )
        model.add_node(sn)
        model.add_edge("other:1", SemanticEdgeType.CONSTRAINS, sn.id)

        result = _enrich_with_semantic_model(
            "base",
            "foo",
            "test.ivy",
            lsp.Position(5, 0),
            ["action foo = {}"],
            model,
        )
        assert "incoming" in result


class TestGetHoverInfoWithModel:
    def _make_indexer(self, symbols):
        """Create a mock indexer that returns the given symbols."""
        indexer = MagicMock()
        if symbols:
            from ivy_lsp.core.indexer.workspace_indexer import SymbolLocation

            locations = [
                SymbolLocation(
                    symbol=s,
                    filepath=s.file_path or "",
                    range=s.range,
                )
                for s in symbols
            ]
            indexer.lookup_symbol.return_value = locations
        else:
            indexer.lookup_symbol.return_value = []
        return indexer

    def test_passes_model_to_enrichment(self):
        sym = IvySymbol(
            name="foo",
            kind=lsp.SymbolKind.Function,
            range=(5, 0, 5, 20),
            file_path="test.ivy",
        )
        indexer = self._make_indexer([sym])
        model = SemanticModel()

        result = get_hover_info(
            indexer,
            "test.ivy",
            lsp.Position(5, 5),
            ["", "", "", "", "", "action foo = {}"],
            semantic_model=model,
        )
        assert result is not None
