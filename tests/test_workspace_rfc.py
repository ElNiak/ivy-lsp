"""Tests for RFC manifest loading and coverage wiring in the workspace indexer.

Creates a temporary workspace with a YAML requirement manifest and Ivy source
files containing bracket tags, then verifies that the WorkspaceIndexer correctly
loads manifests and wires COVERS edges via the RequirementGraph.
"""

import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import EdgeType, RequirementGraph
from ivy_lsp.semantic.nodes import RfcRequirement


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANIFEST_YAML = """\
rfc: "RFC9000"
requirements:
  rfc9000:4.1:
    text: "An endpoint MUST NOT send data on a stream..."
    section: "4.1"
    level: MUST
    layer: frame
    testable: true
  rfc9000:8.1:
    text: "A server SHOULD perform address validation..."
    section: "8.1"
    level: SHOULD
    layer: connection
    testable: true
  rfc9000:17.2:
    text: "Implementations MUST support the version negotiation..."
    section: "17.2"
    level: MUST
    layer: transport
    testable: true
"""

IVY_SOURCE_WITH_TAGS = """\
#lang ivy1.7

type cid

object quic_frame = {
    action send(c:cid)

    before send {
        require c ~= c;  # [rfc9000:4.1, rfc9000:8.1]
    }
}
"""

IVY_SOURCE_WITHOUT_TAGS = """\
#lang ivy1.7

type pkt_num

object quic_transport = {
    action recv(p:pkt_num)

    before recv {
        require p ~= p;
    }
}
"""


# ---------------------------------------------------------------------------
# RequirementGraph unit tests for RFC coverage
# ---------------------------------------------------------------------------


class TestRequirementGraphRfcCoverage:
    """Unit tests for the new RFC-related methods on RequirementGraph."""

    def test_covers_edge_type_exists(self):
        assert EdgeType.COVERS.value == "covers"

    def test_add_rfc_requirement(self):
        graph = RequirementGraph()
        rfc_req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data...",
            level="MUST",
            layer="frame",
        )
        graph.add_rfc_requirement(rfc_req)
        assert "rfc9000:4.1" in graph.rfc_requirements
        assert graph.rfc_requirements["rfc9000:4.1"] is rfc_req

    def test_rfc_requirements_dict_initialized_empty(self):
        graph = RequirementGraph()
        assert graph.rfc_requirements == {}

    def test_wire_coverage_edges_creates_covers_edges(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:8",
            kind="require",
            formula_text="c ~= c",
            line=8,
            col=0,
            file="/test/file.ivy",
            monitor_action="quic_frame.send",
            mixin_kind="before",
            bracket_tags=["rfc9000:4.1", "rfc9000:8.1"],
        )
        graph.add_requirement(req)

        rfc1 = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        rfc2 = RfcRequirement(
            id="rfc9000:8.1",
            rfc="RFC9000",
            section="8.1",
            text="...",
            level="SHOULD",
        )
        graph.add_rfc_requirement(rfc1)
        graph.add_rfc_requirement(rfc2)

        graph.wire_coverage_edges()

        covers_edges = [
            (s, t) for s, et, t in graph.edges if et == EdgeType.COVERS
        ]
        assert ("/test/file.ivy:8", "rfc9000:4.1") in covers_edges
        assert ("/test/file.ivy:8", "rfc9000:8.1") in covers_edges

    def test_wire_coverage_edges_ignores_unknown_tags(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:1",
            kind="require",
            formula_text="true",
            line=1,
            col=0,
            file="/test/file.ivy",
            monitor_action="act",
            mixin_kind="before",
            bracket_tags=["nonexistent:99"],
        )
        graph.add_requirement(req)

        graph.wire_coverage_edges()

        covers_edges = [e for e in graph.edges if e[1] == EdgeType.COVERS]
        assert len(covers_edges) == 0

    def test_get_coverage_stats(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:8",
            kind="require",
            formula_text="true",
            line=8,
            col=0,
            file="/test/file.ivy",
            monitor_action="act",
            mixin_kind="before",
            bracket_tags=["rfc9000:4.1"],
        )
        graph.add_requirement(req)

        rfc1 = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="...", level="MUST",
        )
        rfc2 = RfcRequirement(
            id="rfc9000:8.1", rfc="RFC9000", section="8.1",
            text="...", level="SHOULD",
        )
        graph.add_rfc_requirement(rfc1)
        graph.add_rfc_requirement(rfc2)

        stats = graph.get_coverage_stats()
        assert stats["total"] == 2
        assert stats["covered"] == 1
        assert stats["uncovered"] == 1

    def test_get_coverage_stats_empty(self):
        graph = RequirementGraph()
        stats = graph.get_coverage_stats()
        assert stats == {"total": 0, "covered": 0, "uncovered": 0}

    def test_get_uncovered_requirements(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:8",
            kind="require",
            formula_text="true",
            line=8,
            col=0,
            file="/test/file.ivy",
            monitor_action="act",
            mixin_kind="before",
            bracket_tags=["rfc9000:4.1"],
        )
        graph.add_requirement(req)

        rfc1 = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="covered text", level="MUST",
        )
        rfc2 = RfcRequirement(
            id="rfc9000:8.1", rfc="RFC9000", section="8.1",
            text="uncovered text", level="SHOULD",
        )
        rfc3 = RfcRequirement(
            id="rfc9000:17.2", rfc="RFC9000", section="17.2",
            text="also uncovered", level="MUST",
        )
        graph.add_rfc_requirement(rfc1)
        graph.add_rfc_requirement(rfc2)
        graph.add_rfc_requirement(rfc3)

        uncovered = graph.get_uncovered_requirements()
        uncovered_ids = {r.id for r in uncovered}
        assert uncovered_ids == {"rfc9000:8.1", "rfc9000:17.2"}

    def test_wire_coverage_edges_idempotent(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:8",
            kind="require",
            formula_text="true",
            line=8,
            col=0,
            file="/test/file.ivy",
            monitor_action="act",
            mixin_kind="before",
            bracket_tags=["rfc9000:4.1"],
        )
        graph.add_requirement(req)

        rfc1 = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        graph.add_rfc_requirement(rfc1)

        # Call wire_coverage_edges twice
        graph.wire_coverage_edges()
        covers_after_first = [
            e for e in graph.edges if e[1] == EdgeType.COVERS
        ]
        graph.wire_coverage_edges()
        covers_after_second = [
            e for e in graph.edges if e[1] == EdgeType.COVERS
        ]

        # Should have the same number of COVERS edges (no duplicates)
        assert len(covers_after_first) == len(covers_after_second)
        assert len(covers_after_second) == 1

    def test_get_uncovered_requirements_all_covered(self):
        from ivy_lsp.analysis.requirement_graph import RequirementNode

        graph = RequirementGraph()

        req = RequirementNode(
            id="/test/file.ivy:8",
            kind="require",
            formula_text="true",
            line=8,
            col=0,
            file="/test/file.ivy",
            monitor_action="act",
            mixin_kind="before",
            bracket_tags=["rfc9000:4.1"],
        )
        graph.add_requirement(req)

        rfc1 = RfcRequirement(
            id="rfc9000:4.1", rfc="RFC9000", section="4.1",
            text="...", level="MUST",
        )
        graph.add_rfc_requirement(rfc1)

        uncovered = graph.get_uncovered_requirements()
        assert uncovered == []


# ---------------------------------------------------------------------------
# Workspace-level integration tests
# ---------------------------------------------------------------------------


class TestWorkspaceRfcIntegration:
    """Integration tests: create a temp workspace with manifest + Ivy files,
    index it, and verify coverage edges are wired correctly."""

    def _setup_workspace(self, tmp_path: Path) -> Path:
        """Create temp workspace with manifest and Ivy files."""
        # Create protocol-testing directory structure for find_manifests
        pt_dir = tmp_path / "protocol-testing" / "quic"
        pt_dir.mkdir(parents=True)
        manifest_path = pt_dir / "quic_requirements.yaml"
        manifest_path.write_text(MANIFEST_YAML)

        # Write Ivy source files at the workspace root
        (tmp_path / "quic_frame.ivy").write_text(IVY_SOURCE_WITH_TAGS)
        (tmp_path / "quic_transport.ivy").write_text(IVY_SOURCE_WITHOUT_TAGS)

        return tmp_path

    def test_manifests_loaded_into_graph(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        ws = self._setup_workspace(tmp_path)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(ws))
        indexer = WorkspaceIndexer(str(ws), parser, resolver)
        indexer.index_workspace()

        rfc_reqs = indexer.requirement_graph.rfc_requirements
        assert len(rfc_reqs) == 3
        assert "rfc9000:4.1" in rfc_reqs
        assert "rfc9000:8.1" in rfc_reqs
        assert "rfc9000:17.2" in rfc_reqs

    def test_coverage_edges_wired(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        ws = self._setup_workspace(tmp_path)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(ws))
        indexer = WorkspaceIndexer(str(ws), parser, resolver)
        indexer.index_workspace()

        graph = indexer.requirement_graph
        covers_edges = [
            (s, t) for s, et, t in graph.edges if et == EdgeType.COVERS
        ]

        # The Ivy source has bracket tags [rfc9000:4.1, rfc9000:8.1]
        covered_targets = {t for _, t in covers_edges}
        assert "rfc9000:4.1" in covered_targets
        assert "rfc9000:8.1" in covered_targets
        # rfc9000:17.2 has no bracket tag referencing it
        assert "rfc9000:17.2" not in covered_targets

    def test_coverage_stats(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        ws = self._setup_workspace(tmp_path)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(ws))
        indexer = WorkspaceIndexer(str(ws), parser, resolver)
        indexer.index_workspace()

        stats = indexer.requirement_graph.get_coverage_stats()
        assert stats["total"] == 3
        assert stats["covered"] == 2  # rfc9000:4.1 and rfc9000:8.1
        assert stats["uncovered"] == 1  # rfc9000:17.2

    def test_uncovered_requirements(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        ws = self._setup_workspace(tmp_path)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(ws))
        indexer = WorkspaceIndexer(str(ws), parser, resolver)
        indexer.index_workspace()

        uncovered = indexer.requirement_graph.get_uncovered_requirements()
        assert len(uncovered) == 1
        assert uncovered[0].id == "rfc9000:17.2"
        assert uncovered[0].section == "17.2"

    def test_no_manifest_directory_no_crash(self, tmp_path):
        """Workspace without protocol-testing/ dir should not crash."""
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        (tmp_path / "simple.ivy").write_text("#lang ivy1.7\ntype t\n")

        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        assert len(indexer.requirement_graph.rfc_requirements) == 0
        stats = indexer.requirement_graph.get_coverage_stats()
        assert stats == {"total": 0, "covered": 0, "uncovered": 0}

    def test_rfc_requirement_fields_loaded_correctly(self, tmp_path):
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        ws = self._setup_workspace(tmp_path)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(ws))
        indexer = WorkspaceIndexer(str(ws), parser, resolver)
        indexer.index_workspace()

        rfc_reqs = indexer.requirement_graph.rfc_requirements

        req_41 = rfc_reqs["rfc9000:4.1"]
        assert req_41.rfc == "RFC9000"
        assert req_41.section == "4.1"
        assert req_41.level == "MUST"
        assert req_41.layer == "frame"
        assert req_41.testable is True
        assert "MUST NOT send data" in req_41.text

        req_81 = rfc_reqs["rfc9000:8.1"]
        assert req_81.level == "SHOULD"
        assert req_81.layer == "connection"
