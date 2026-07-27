"""Migration tests for compute_semantic_diagnostics.

Asserts the function returns List[IvyDiagnostic] with registry-validated
codes. RFC-tag and shadow-declaration paths covered.
"""

import pytest

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic, RelatedLocation
from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode
from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics

pytestmark = pytest.mark.unit


def _make_empty_model() -> SemanticModel:
    """Return a SemanticModel with no nodes."""
    return SemanticModel()


def _make_model_with_orphaned_tag() -> SemanticModel:
    """Return a model with an RFC annotation whose tag has no matching requirement."""
    model = SemanticModel()
    ann = RfcAnnotation(
        id="/tmp/x.ivy:2:0",
        file="/tmp/x.ivy",
        line=2,
        tags=["rfc9000:99.99"],
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
    return model


def _make_model_with_shadow() -> tuple[SemanticModel, str]:
    """Return a model with a shadow declaration and the filepath being tested."""
    model = SemanticModel()
    sym1 = SymbolNode(
        id="foo_base",
        name="foo",
        qualified_name="quic.foo",
        kind="relation",
        file="/test/base.ivy",
        line=10,
    )
    model.add_node(sym1)
    sym2 = SymbolNode(
        id="foo_ext",
        name="foo",
        qualified_name="quic.foo",
        kind="relation",
        file="/test/ext.ivy",
        line=5,
    )
    model.add_node(sym2)
    return model, "/test/ext.ivy"


class TestReturnType:
    def test_returns_list_on_none_model(self):
        diags = compute_semantic_diagnostics(
            model=None,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n",
        )
        assert isinstance(diags, list)
        assert diags == []

    def test_returns_list_on_empty_model(self):
        model = _make_empty_model()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n",
        )
        assert isinstance(diags, list)

    def test_every_item_is_ivydiagnostic_empty(self):
        model = _make_empty_model()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n",
        )
        for d in diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_every_item_is_ivydiagnostic_with_orphaned_tag(self):
        model = _make_model_with_orphaned_tag()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n\n\nrequire x > 0;  # [rfc9000:99.99]\n",
        )
        assert len(diags) >= 1
        for d in diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_every_item_is_ivydiagnostic_with_shadow(self):
        model, filepath = _make_model_with_shadow()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath=filepath,
            source="#lang ivy1.7\nrelation foo\n",
        )
        for d in diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"


class TestEmittedCodes:
    """Verify each emit-site code is what we expect.

    Note: registry membership is enforced at IvyDiagnostic construction
    (via ``__post_init__``); a successfully-returned diagnostic with an
    unregistered code is impossible. These tests only assert that the
    emit sites use the *expected* canonical codes.
    """

    def test_orphaned_tag_emit_site_uses_canonical_code(self):
        model = _make_model_with_orphaned_tag()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n\n\nrequire x > 0;  # [rfc9000:99.99]\n",
        )
        codes = [d.code for d in diags]
        assert "ivy.rfc.orphanedTag" in codes

    def test_missing_bracket_tag_emit_site_uses_canonical_code(self):
        model = _make_empty_model()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\nbefore foo {\n  require x > 0;\n}\n",
        )
        codes = [d.code for d in diags]
        assert "ivy.rfc.missingBracketTag" in codes

    def test_shadow_declaration_emit_site_uses_canonical_code(self):
        model, filepath = _make_model_with_shadow()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath=filepath,
            source="#lang ivy1.7\nrelation foo\n",
        )
        codes = [d.code for d in diags]
        assert "ivy.include.shadowDeclaration" in codes


class TestShadowDeclarationRelatedInfo:
    def test_shadow_has_related_location(self):
        model, filepath = _make_model_with_shadow()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath=filepath,
            source="#lang ivy1.7\nrelation foo\n",
        )
        shadows = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert len(shadows) >= 1
        shadow = shadows[0]
        assert shadow.related, "shadow diagnostic must carry RelatedLocation"
        assert isinstance(shadow.related[0], RelatedLocation)

    def test_shadow_related_points_to_original_file(self):
        model, filepath = _make_model_with_shadow()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath=filepath,
            source="#lang ivy1.7\nrelation foo\n",
        )
        shadows = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert shadows
        rel = shadows[0].related[0]
        assert rel.file == "/test/base.ivy"
        assert "original declaration" in rel.message


class TestToLspConversion:
    def test_ivydiagnostic_converts_to_lsp(self):
        """to_lsp() must produce a valid lsp.Diagnostic without error."""
        from lsprotocol import types as lsp

        model = _make_model_with_orphaned_tag()
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source="#lang ivy1.7\n\n\nrequire x > 0;  # [rfc9000:99.99]\n",
        )
        assert diags
        for d in diags:
            lsp_diag = d.to_lsp()
            assert isinstance(lsp_diag, lsp.Diagnostic)
            assert lsp_diag.code == d.code


class TestRangePrecision:
    """Phase 5 cluster 5.4: token-precise spans on semantic diagnostics."""

    def test_orphaned_tag_spans_bracket_tag(self):
        """Span the tag value inside the bracket block, not full line."""
        model = _make_model_with_orphaned_tag()
        # Fixture marks the annotation at line 2 (0-based). Provide a
        # source where line 2 holds the bracket block so the per-tag span
        # search succeeds.
        source = "#lang ivy1.7\n\nrequire x > 0;  # [rfc9000:99.99]\n"
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source=source,
        )
        d = next(x for x in diags if x.code == "ivy.rfc.orphanedTag")
        assert d.line == 2
        line_text = source.splitlines()[2]
        bracket_open = line_text.index("[")
        assert d.character == bracket_open + 1
        assert d.end_character == d.character + len("rfc9000:99.99")

    def test_missing_bracket_tag_spans_assertion(self):
        """Span the assertion keyword + body, not full line."""
        model = _make_empty_model()
        # Line 2 = "  require x > 0;"; assertion starts at col 2 ("require").
        source = "#lang ivy1.7\nbefore foo {\n  require x > 0;\n}\n"
        diags = compute_semantic_diagnostics(
            model=model,
            filepath="/tmp/x.ivy",
            source=source,
        )
        d = next(x for x in diags if x.code == "ivy.rfc.missingBracketTag")
        assert d.line == 2
        line_text = source.splitlines()[2]
        # Span starts at the keyword (skipping leading whitespace).
        assert d.character == line_text.index("require")
        # Span ends at the `;`.
        assert d.end_character == line_text.index(";") + 1
