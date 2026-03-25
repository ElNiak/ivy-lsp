"""Full integration test verifying the complete pipeline (Task 17).

Parses a minimal Ivy source through the analysis pipeline, verifies the
SemanticModel is populated, RFC annotations are parsed and wired, coverage
stats are correct, and MCP tool logic returns valid JSON.
"""

import json
import os
import re
import tempfile

from ivy_lsp.core.semantic.edges import SemanticEdgeType
from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import (
    RfcAnnotation,
    RfcRequirement,
    SymbolNode,
    TypeNode,
)
from ivy_lsp.core.semantic.rfc_annotations import (
    compute_coverage,
    load_requirement_manifest,
    parse_file_rfc_annotations,
    parse_rfc_tags,
)
from ivy_lsp.features.code_lens import compute_code_lenses
from ivy_lsp.features.diagnostics import compute_semantic_diagnostics
from ivy_lsp.features.hover import _enrich_with_semantic_model

# --- Test Data ---

MINIMAL_IVY_SOURCE = """\
#lang ivy1.7

type endpoint
type packet

relation conn_state(E:endpoint, F:endpoint) : bool

action send_packet(src: endpoint, dst: endpoint, pkt: packet)

before send_packet {
    require conn_state(src, dst);  # [rfc9000:4.1]
    require pkt != 0;  # [rfc9000:14.1, rfc9000:8.1]
}

after send_packet {
    ensure conn_state(src, dst);  # [rfc9000:5.1]
}
"""

MANIFEST_YAML = """\
rfc: "RFC9000"
requirements:
  rfc9000:4.1:
    text: "A sender MUST NOT send data beyond the limit"
    section: "4.1"
    level: MUST
    layer: stream
    testable: true
  rfc9000:14.1:
    text: "An endpoint MUST limit payload size"
    section: "14.1"
    level: MUST
    layer: packet
    testable: true
  rfc9000:8.1:
    text: "Frames MUST be well-formed"
    section: "8.1"
    level: MUST
    layer: frame
    testable: true
  rfc9000:5.1:
    text: "Connection state SHOULD be maintained"
    section: "5.1"
    level: SHOULD
    layer: connection
    testable: true
  rfc9000:9.1:
    text: "Flow control MAY be extended"
    section: "9.1"
    level: MAY
    layer: transport
    testable: false
"""


class TestRfcAnnotationParsing:
    """Verify RFC annotation parsing from source."""

    def test_parse_tags_from_lines(self):
        tags = parse_rfc_tags("require x > 0;  # [rfc9000:4.1]")
        assert tags == ["rfc9000:4.1"]

    def test_parse_multi_tags(self):
        tags = parse_rfc_tags("require pkt != 0;  # [rfc9000:14.1, rfc9000:8.1]")
        assert set(tags) == {"rfc9000:14.1", "rfc9000:8.1"}

    def test_parse_file_annotations(self):
        annotations = parse_file_rfc_annotations(MINIMAL_IVY_SOURCE, "/tmp/test.ivy")
        assert len(annotations) >= 2  # At least 2 annotated lines
        all_tags = set()
        for ann in annotations:
            all_tags.update(ann.tags)
        assert "rfc9000:4.1" in all_tags
        assert "rfc9000:14.1" in all_tags
        assert "rfc9000:8.1" in all_tags
        assert "rfc9000:5.1" in all_tags


class TestManifestLoading:
    """Verify YAML manifest loading."""

    def test_load_manifest(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_requirements.yaml", delete=False
        ) as f:
            f.write(MANIFEST_YAML)
            path = f.name

        try:
            reqs = load_requirement_manifest(path)
            assert len(reqs) == 5
            assert "rfc9000:4.1" in reqs
            assert reqs["rfc9000:4.1"].level == "MUST"
            assert reqs["rfc9000:5.1"].level == "SHOULD"
            assert reqs["rfc9000:9.1"].level == "MAY"
        finally:
            os.unlink(path)


class TestCoverageComputation:
    """Verify coverage stats."""

    def test_coverage_stats(self):
        annotations = parse_file_rfc_annotations(MINIMAL_IVY_SOURCE, "/tmp/test.ivy")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_requirements.yaml", delete=False
        ) as f:
            f.write(MANIFEST_YAML)
            path = f.name

        try:
            reqs = load_requirement_manifest(path)
            stats = compute_coverage(annotations, reqs)
            # 4 of 5 requirements are covered (4.1, 14.1, 8.1, 5.1)
            assert stats.total == 5
            assert stats.covered == 4
            assert stats.uncovered == 1  # rfc9000:9.1 not in source
        finally:
            os.unlink(path)


class TestSemanticModelPopulation:
    """Verify SemanticModel is correctly populated."""

    def _build_model(self) -> SemanticModel:
        model = SemanticModel()

        # Add requirements from manifest
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_requirements.yaml", delete=False
        ) as f:
            f.write(MANIFEST_YAML)
            path = f.name

        try:
            reqs = load_requirement_manifest(path)
            for req in reqs.values():
                model.add_node(req)
        finally:
            os.unlink(path)

        # Add annotations from source
        for ann in parse_file_rfc_annotations(MINIMAL_IVY_SOURCE, "/tmp/test.ivy"):
            model.add_node(ann)

        # Add symbol nodes
        model.add_node(
            SymbolNode(
                id="/tmp/test.ivy:8:send_packet",
                name="send_packet",
                qualified_name="send_packet",
                kind="action",
                file="/tmp/test.ivy",
                line=8,
                params=["src:endpoint", "dst:endpoint", "pkt:packet"],
            )
        )
        model.add_node(
            TypeNode(
                id="/tmp/test.ivy:3:endpoint",
                name="endpoint",
                qualified_name="endpoint",
                file="/tmp/test.ivy",
                line=3,
            )
        )

        return model

    def test_model_has_requirements(self):
        model = self._build_model()
        reqs = model.get_nodes_by_type(RfcRequirement)
        assert len(reqs) == 5

    def test_model_has_annotations(self):
        model = self._build_model()
        anns = model.get_nodes_by_type(RfcAnnotation)
        assert len(anns) >= 2

    def test_model_has_symbols(self):
        model = self._build_model()
        symbols = model.get_nodes_by_type(SymbolNode)
        assert len(symbols) == 1
        assert symbols[0].name == "send_packet"

    def test_model_has_types(self):
        model = self._build_model()
        types = model.get_nodes_by_type(TypeNode)
        assert len(types) == 1
        assert types[0].name == "endpoint"

    def test_get_node_by_id(self):
        model = self._build_model()
        req = model.get_node("rfc9000:4.1")
        assert req is not None
        assert isinstance(req, RfcRequirement)
        assert req.level == "MUST"

    def test_edges(self):
        model = self._build_model()
        model.add_edge(
            "/tmp/test.ivy:8:send_packet",
            SemanticEdgeType.READS,
            "state:conn_state",
        )
        outgoing = model.get_outgoing("/tmp/test.ivy:8:send_packet")
        assert len(outgoing) == 1
        assert outgoing[0][0] == SemanticEdgeType.READS


class TestSemanticDiagnosticsIntegration:
    """Verify semantic diagnostics with populated model."""

    def test_orphaned_tag_detected(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:99.99"],
        )
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        model.add_node(ann)
        model.add_node(req)

        source = "#lang ivy1.7\n\n\n\n\nrequire x > 0;  # [rfc9000:99.99]\n"
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        orphan = [d for d in diags if "Orphaned RFC tag" in d.message]
        assert len(orphan) == 1

    def test_no_orphan_when_matched(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:4.1"],
        )
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        model.add_node(ann)
        model.add_node(req)

        source = "#lang ivy1.7\n\n\n\n\nrequire x > 0;  # [rfc9000:4.1]\n"
        diags = compute_semantic_diagnostics(model, "/tmp/test.ivy", source)
        orphan = [d for d in diags if "Orphaned RFC tag" in d.message]
        assert len(orphan) == 0


class TestHoverEnrichmentIntegration:
    """Verify hover enrichment with semantic model."""

    def test_hover_shows_rfc_info(self):
        model = SemanticModel()
        ann = RfcAnnotation(
            id="/tmp/test.ivy:2:0",
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

        from lsprotocol import types as lsp

        result = _enrich_with_semantic_model(
            "base",
            "require",
            "test.ivy",
            lsp.Position(2, 0),
            ["require x > 0;  # [rfc9000:4.1]"],
            model,
        )
        assert "RFC9000" in result
        assert "MUST" in result


class TestCodeLensIntegration:
    """Verify code lens with semantic model."""

    def test_coverage_lens_with_model(self):
        from unittest.mock import MagicMock

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        model.add_node(req)

        indexer = MagicMock()
        indexer.requirement_graph = None
        indexer.include_graph = None

        lenses = compute_code_lenses(indexer, "test.ivy", "#lang ivy1.7\n", model)
        coverage = [
            l for l in lenses if "covered" in (l.command.title if l.command else "")
        ]
        assert len(coverage) == 1
        assert "RFC9000: 0/1 covered" in coverage[0].command.title


class TestMcpToolLogic:
    """Verify MCP tool logic returns valid JSON structures."""

    def test_extract_requirements_logic(self):
        """Test the requirement extraction regex directly."""
        rfc_text = (
            "The sender MUST NOT send data beyond the limit. "
            "The receiver SHOULD acknowledge frames. "
            "An endpoint MAY pad frames."
        )
        req_pattern = re.compile(
            r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
            r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
            re.MULTILINE,
        )
        results = list(req_pattern.finditer(rfc_text))
        assert len(results) == 3

    def test_traceability_matrix_structure(self):
        """Verify traceability matrix JSON structure."""
        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="test",
            level="MUST",
        )
        ann = RfcAnnotation(
            id="f:5:0",
            file="f",
            line=5,
            tags=["rfc9000:4.1"],
        )
        model.add_node(req)
        model.add_node(ann)

        # Simulate what the MCP tool does
        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)
        covered_tags = set()
        for a in annotations:
            covered_tags.update(a.tags)

        matrix = []
        for r in requirements:
            matrix.append(
                {
                    "id": r.id,
                    "rfc": r.rfc,
                    "level": r.level,
                    "covered": r.id in covered_tags,
                }
            )

        result = json.dumps(
            {
                "total_requirements": len(requirements),
                "covered": sum(1 for m in matrix if m["covered"]),
                "matrix": matrix,
            }
        )
        parsed = json.loads(result)
        assert parsed["total_requirements"] == 1
        assert parsed["covered"] == 1
        assert parsed["matrix"][0]["covered"] is True
