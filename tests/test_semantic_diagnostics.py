"""Tests for semantic diagnostics (Task 11)."""

from lsprotocol import types as lsp

from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode
from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics


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


def test_orphaned_rfc_tag_has_code():
    model = SemanticModel()
    ann = RfcAnnotation(
        id="/tmp/test.ivy:5:0",
        file="/tmp/test.ivy",
        line=5,
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
    source = "#lang ivy1.7\n\n\n\n\nrequire x > 0;  # [rfc9000:99.99]\n"
    diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
    orphan_diags = [d for d in diags if "Orphaned RFC tag" in d.message]
    assert len(orphan_diags) == 1
    assert orphan_diags[0].code == "ivy.rfc.orphanedTag"


def test_missing_bracket_tag_has_code():
    model = SemanticModel()
    req = RfcRequirement(
        id="rfc9000:4.1",
        rfc="RFC9000",
        section="4.1",
        text="senders MUST NOT...",
        level="MUST",
    )
    model.add_node(req)
    source = "#lang ivy1.7\nbefore foo {\n  require x > 0;\n}\n"
    diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
    hint_diags = [d for d in diags if "bracket tag" in d.message.lower()]
    assert len(hint_diags) >= 1
    assert hint_diags[0].code == "ivy.rfc.missingBracketTag"


def test_rfc_tag_gap_detected():
    model = SemanticModel()
    for tag_num in [1, 2, 4, 5, 6, 7]:  # gap at 3, 7 tags total
        ann = RfcAnnotation(
            id=f"/tmp/test.ivy:{tag_num}:0",
            file="/tmp/test.ivy",
            line=tag_num,
            tags=[str(tag_num)],
        )
        model.add_node(ann)
    # Need at least one RfcRequirement for the rfc_reqs guard to pass
    req = RfcRequirement(
        id="rfc9000:4.1",
        rfc="RFC9000",
        section="4.1",
        text="...",
        level="MUST",
    )
    model.add_node(req)
    source = "#lang ivy1.7\n" + "require x > 0;\n" * 8
    diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
    gaps = [d for d in diags if d.code == "ivy.rfc.tagGap"]
    assert len(gaps) == 1
    assert "[3]" in gaps[0].message


def test_rfc_tag_gap_not_flagged_when_sparse():
    model = SemanticModel()
    # Tags [4], [17], [23] — sparse, not sequential
    for tag_num in [4, 17, 23]:
        ann = RfcAnnotation(
            id=f"/tmp/test.ivy:{tag_num}:0",
            file="/tmp/test.ivy",
            line=tag_num,
            tags=[str(tag_num)],
        )
        model.add_node(ann)
    req = RfcRequirement(
        id="rfc9000:4.1",
        rfc="RFC9000",
        section="4.1",
        text="...",
        level="MUST",
    )
    model.add_node(req)
    source = "#lang ivy1.7\n" + "require x > 0;\n" * 24
    diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
    gaps = [d for d in diags if d.code == "ivy.rfc.tagGap"]
    assert len(gaps) == 0


def test_rfc_tag_duplicate_detected():
    model = SemanticModel()
    ann1 = RfcAnnotation(
        id="/tmp/test.ivy:5:0",
        file="/tmp/test.ivy",
        line=5,
        tags=["4"],
    )
    ann2 = RfcAnnotation(
        id="/tmp/test.ivy:10:0",
        file="/tmp/test.ivy",
        line=10,
        tags=["4"],
    )
    model.add_node(ann1)
    model.add_node(ann2)
    req = RfcRequirement(
        id="rfc9000:4.1",
        rfc="RFC9000",
        section="4.1",
        text="...",
        level="MUST",
    )
    model.add_node(req)
    source = "#lang ivy1.7\n" + "require x > 0;\n" * 11
    diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
    dupes = [d for d in diags if d.code == "ivy.rfc.tagDuplicate"]
    assert len(dupes) >= 1
    assert "[4]" in dupes[0].message


def test_shadow_declaration_detected():
    model = SemanticModel()
    sym1 = SymbolNode(
        id="zero_rtt_allowed_base",
        name="zero_rtt_allowed",
        qualified_name="quic.zero_rtt_allowed",
        kind="relation",
        file="/test/quic_shim.ivy",
        line=42,
    )
    model.add_node(sym1)
    sym2 = SymbolNode(
        id="zero_rtt_allowed_mim",
        name="zero_rtt_allowed",
        qualified_name="quic.zero_rtt_allowed",
        kind="relation",
        file="/test/quic_shim_mim.ivy",
        line=15,
    )
    model.add_node(sym2)

    source = "#lang ivy1.7\ninclude quic_shim\nrelation zero_rtt_allowed\n"
    diags = compute_semantic_diagnostics(
        model,
        "/test/quic_shim_mim.ivy",
        source,
    )
    shadow = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
    assert len(shadow) >= 1
    assert "zero_rtt_allowed" in shadow[0].message
    assert "quic_shim.ivy" in shadow[0].message


def test_no_shadow_when_different_kind():
    model = SemanticModel()
    sym1 = SymbolNode(
        id="foo_action",
        name="foo",
        qualified_name="quic.foo",
        kind="action",
        file="/test/base.ivy",
        line=10,
    )
    model.add_node(sym1)
    sym2 = SymbolNode(
        id="foo_relation",
        name="foo",
        qualified_name="quic.foo",
        kind="relation",
        file="/test/ext.ivy",
        line=5,
    )
    model.add_node(sym2)

    source = "#lang ivy1.7\nrelation foo\n"
    diags = compute_semantic_diagnostics(model, "/test/ext.ivy", source)
    shadow = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
    assert len(shadow) == 0


class TestComputeDiagnosticsIntegration:
    """Verify compute_diagnostics accepts semantic_model param."""

    def test_accepts_semantic_model_kwarg(self):
        from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

        # Should not raise even when semantic_model=None
        diags = compute_diagnostics(
            parser=None,
            source="#lang ivy1.7\n",
            filepath="/tmp/test.ivy",
            indexer=None,
            semantic_model=None,
        )
        assert isinstance(diags, list)
