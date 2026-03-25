"""Tests for semantic diagnostics (Task 11)."""

from lsprotocol import types as lsp

from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
from ivy_lsp.lsp.diagnostics.publisher import compute_semantic_diagnostics


class TestComputeSemanticDiagnostics:
    def test_returns_empty_when_no_model(self):
        diags = compute_semantic_diagnostics(None, "test.ivy", "")
        assert diags == []

    def test_returns_empty_when_no_rfc_data(self):
        model = SemanticModel()
        diags = compute_semantic_diagnostics(model, "test.ivy", "action foo = {}")
        assert diags == []

    def test_orphaned_rfc_tag(self):
        model = SemanticModel()
        # Add an annotation with a tag that has no matching requirement
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:99.99"],
        )
        model.add_node(ann)
        # Add one requirement, but NOT rfc9000:99.99
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT...",
            level="MUST",
        )
        model.add_node(req)

        source = "#lang ivy1.7\n\n\n\n\nrequire x > 0;  # [rfc9000:99.99]\n"
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        orphan_diags = [d for d in diags if "Orphaned RFC tag" in d.message]
        assert len(orphan_diags) == 1
        assert "rfc9000:99.99" in orphan_diags[0].message

    def test_no_orphan_when_tag_matches(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:4.1"],
        )
        model.add_node(ann)
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT...",
            level="MUST",
        )
        model.add_node(req)

        source = "#lang ivy1.7\n\n\n\n\nrequire x > 0;  # [rfc9000:4.1]\n"
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        orphan_diags = [d for d in diags if "Orphaned RFC tag" in d.message]
        assert len(orphan_diags) == 0

    def test_missing_tag_hint(self):
        model = SemanticModel()
        source = "#lang ivy1.7\n" "before send_pkt {\n" "    require x > 0;\n" "}\n"
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        hint_diags = [
            d
            for d in diags
            if d.severity == lsp.DiagnosticSeverity.Hint
            and "without RFC bracket tag" in d.message
        ]
        assert len(hint_diags) == 1

    def test_no_hint_when_tag_present(self):
        model = SemanticModel()
        source = (
            "#lang ivy1.7\n"
            "before send_pkt {\n"
            "    require x > 0;  # [rfc9000:4.1]\n"
            "}\n"
        )
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        hint_diags = [
            d
            for d in diags
            if d.severity == lsp.DiagnosticSeverity.Hint
            and "without RFC bracket tag" in d.message
        ]
        assert len(hint_diags) == 0


class TestComputeDiagnosticsIntegration:
    """Verify compute_diagnostics accepts semantic_model param."""

    def test_accepts_semantic_model_kwarg(self):
        from ivy_lsp.lsp.diagnostics.publisher import compute_diagnostics

        # Should not raise even when semantic_model=None
        diags = compute_diagnostics(
            parser=None,
            source="#lang ivy1.7\n",
            filepath="/tmp/test.ivy",
            indexer=None,
            semantic_model=None,
        )
        assert isinstance(diags, list)
