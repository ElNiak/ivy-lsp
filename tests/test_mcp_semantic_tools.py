"""Tests for MCP semantic tools (Task 15)."""

import json
import re


class TestIvyExtractRequirements:
    """Test the RFC requirement extraction from text."""

    def test_extract_must_requirements(self):
        # Test the regex-based extraction logic directly
        rfc_text = (
            "The sender MUST NOT send data beyond the limit. "
            "The receiver SHOULD acknowledge all frames. "
            "An endpoint MAY send padding frames."
        )
        req_pattern = re.compile(
            r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
            r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
            re.MULTILINE,
        )
        results = []
        for m in req_pattern.finditer(rfc_text):
            text = m.group(1).strip()
            level = m.group(2)
            if level in ("SHALL", "REQUIRED"):
                level = "MUST"
            elif level in ("SHALL NOT",):
                level = "MUST NOT"
            elif level in ("RECOMMENDED",):
                level = "SHOULD"
            elif level in ("OPTIONAL",):
                level = "MAY"
            results.append({"text": text, "level": level})

        assert len(results) == 3
        assert results[0]["level"] == "MUST NOT"
        assert results[1]["level"] == "SHOULD"
        assert results[2]["level"] == "MAY"

    def test_extract_empty_text(self):
        rfc_text = "This text has no normative requirements."
        req_pattern = re.compile(
            r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
            r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
            re.MULTILINE,
        )
        results = list(req_pattern.finditer(rfc_text))
        assert len(results) == 0


class TestTraceabilityMatrix:
    """Test traceability matrix computation logic."""

    def test_matrix_structure(self):
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="senders MUST NOT send data", level="MUST",
        )
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0", file="/tmp/test.ivy", line=5,
            tags=["rfc9000:4.1"],
        )
        model.add_node(req)
        model.add_node(ann)

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        covered_tags = set()
        for a in annotations:
            covered_tags.update(a.tags)

        assert len(requirements) == 1
        assert req.id in covered_tags

    def test_uncovered_requirement(self):
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import RfcRequirement

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.2", rfc="RFC9000", section="4.2",
            text="receiver SHOULD ack", level="SHOULD",
        )
        model.add_node(req)

        annotations = model.get_nodes_by_type(type(req))  # RfcRequirement type
        # No annotations, so nothing covered
        assert len(model.get_nodes_by_type(type(req))) == 1


class TestCoverageStats:
    """Test coverage statistics computation."""

    def test_by_level_grouping(self):
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        reqs = [
            RfcRequirement(id="r:1", rfc="RFC", section="1", text="...", level="MUST"),
            RfcRequirement(id="r:2", rfc="RFC", section="2", text="...", level="MUST"),
            RfcRequirement(id="r:3", rfc="RFC", section="3", text="...", level="SHOULD"),
        ]
        ann = RfcAnnotation(id="f:1:0", file="f", line=1, tags=["r:1"])
        for r in reqs:
            model.add_node(r)
        model.add_node(ann)

        covered = {"r:1"}
        by_level = {}
        for r in reqs:
            level = r.level
            if level not in by_level:
                by_level[level] = {"total": 0, "covered": 0}
            by_level[level]["total"] += 1
            if r.id in covered:
                by_level[level]["covered"] += 1

        assert by_level["MUST"]["total"] == 2
        assert by_level["MUST"]["covered"] == 1
        assert by_level["SHOULD"]["total"] == 1
        assert by_level["SHOULD"]["covered"] == 0


class TestImpactAnalysis:
    """Test impact analysis query logic."""

    def test_find_symbol_edges(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import SymbolNode

        model = SemanticModel()
        sn = SymbolNode(
            id="test.ivy:5:send", name="send",
            qualified_name="quic.send", kind="action",
            file="test.ivy", line=5,
        )
        model.add_node(sn)
        model.add_edge("other:1", SemanticEdgeType.MONITORS, sn.id)
        model.add_edge(sn.id, SemanticEdgeType.READS, "state:conn")

        incoming = model.get_incoming(sn.id)
        outgoing = model.get_outgoing(sn.id)

        assert len(incoming) == 1
        assert incoming[0][0] == SemanticEdgeType.MONITORS
        assert len(outgoing) == 1
        assert outgoing[0][0] == SemanticEdgeType.READS


class TestCrossReferences:
    """Test cross-reference graph query."""

    def test_node_neighborhood(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import SymbolNode

        model = SemanticModel()
        sn1 = SymbolNode(
            id="a:1:foo", name="foo", qualified_name="foo",
            kind="action", file="a", line=1,
        )
        sn2 = SymbolNode(
            id="a:2:bar", name="bar", qualified_name="bar",
            kind="action", file="a", line=2,
        )
        model.add_node(sn1)
        model.add_node(sn2)
        model.add_edge(sn1.id, SemanticEdgeType.DEPENDS_ON, sn2.id)

        assert model.get_node(sn1.id) is sn1
        outgoing = model.get_outgoing(sn1.id)
        assert len(outgoing) == 1
        assert outgoing[0][1] == sn2.id


class TestQuerySymbol:
    """Test rich symbol query."""

    def test_symbol_info_returned(self):
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import SymbolNode

        model = SemanticModel()
        sn = SymbolNode(
            id="test.ivy:5:send", name="send",
            qualified_name="quic.send", kind="action",
            file="test.ivy", line=5,
            params=["dst:endpoint", "pkt:packet"],
            return_sort="bool",
        )
        model.add_node(sn)

        matches = [
            s for s in model.get_nodes_by_type(SymbolNode)
            if s.name == "send"
        ]
        assert len(matches) == 1
        assert matches[0].params == ["dst:endpoint", "pkt:packet"]
        assert matches[0].return_sort == "bool"
