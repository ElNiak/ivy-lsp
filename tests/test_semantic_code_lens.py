"""Tests for semantic code lens enhancements (Task 13)."""

from unittest.mock import MagicMock

from lsprotocol import types as lsp

from ivy_lsp.features.code_lens import compute_code_lenses
from ivy_lsp.semantic.model import SemanticModel
from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement


def _make_indexer(graph=None):
    indexer = MagicMock()
    indexer.requirement_graph = graph
    indexer.include_graph = None
    return indexer


class TestRfcTagLenses:
    def test_no_lenses_when_no_model(self):
        indexer = _make_indexer()
        lenses = compute_code_lenses(indexer, "test.ivy", "action foo = {}", None)
        assert lenses == []

    def test_rfc_tag_lens_on_annotated_line(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="test.ivy:2:0", file="test.ivy", line=2,
            tags=["rfc9000:4.1"],
        )
        req = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="senders MUST NOT send data beyond limit", level="MUST",
        )
        model.add_node(ann)
        model.add_node(req)

        source = "#lang ivy1.7\n\nrequire x > 0;  # [rfc9000:4.1]\n"
        indexer = _make_indexer()
        lenses = compute_code_lenses(indexer, "test.ivy", source, model)

        rfc_lenses = [l for l in lenses if "RFC:" in (l.command.title if l.command else "")]
        assert len(rfc_lenses) == 1
        assert "rfc9000:4.1" in rfc_lenses[0].command.title
        assert "MUST" in rfc_lenses[0].command.title

    def test_rfc_tag_without_matching_requirement(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="test.ivy:2:0", file="test.ivy", line=2,
            tags=["rfc9000:99.1"],
        )
        model.add_node(ann)

        source = "#lang ivy1.7\n\nrequire x > 0;  # [rfc9000:99.1]\n"
        indexer = _make_indexer()
        lenses = compute_code_lenses(indexer, "test.ivy", source, model)

        rfc_lenses = [l for l in lenses if "RFC:" in (l.command.title if l.command else "")]
        assert len(rfc_lenses) == 1
        assert "[rfc9000:99.1]" in rfc_lenses[0].command.title


class TestCoverageSummaryLens:
    def test_coverage_lens_at_line_zero(self):
        model = SemanticModel()
        req1 = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="...", level="MUST",
        )
        req2 = RfcRequirement(
            id="rfc9000:4.2", rfc="RFC9000", section="4.2",
            text="...", level="SHOULD",
        )
        ann = RfcAnnotation(
            id="test.ivy:5:0", file="test.ivy", line=5,
            tags=["rfc9000:4.1"],
        )
        model.add_node(req1)
        model.add_node(req2)
        model.add_node(ann)

        indexer = _make_indexer()
        source = "#lang ivy1.7\n\naction foo = {}\n"
        lenses = compute_code_lenses(indexer, "test.ivy", source, model)

        coverage_lenses = [
            l for l in lenses
            if l.range.start.line == 0 and "covered" in (l.command.title if l.command else "")
        ]
        assert len(coverage_lenses) == 1
        assert "RFC9000: 1/2 covered" in coverage_lenses[0].command.title

    def test_no_coverage_lens_without_requirements(self):
        model = SemanticModel()
        indexer = _make_indexer()
        source = "#lang ivy1.7\n"
        lenses = compute_code_lenses(indexer, "test.ivy", source, model)

        coverage_lenses = [
            l for l in lenses
            if l.range.start.line == 0 and "covered" in (l.command.title if l.command else "")
        ]
        assert len(coverage_lenses) == 0
