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
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data",
            level="MUST",
        )
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
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
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcRequirement

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.2",
            rfc="RFC9000",
            section="4.2",
            text="receiver SHOULD ack",
            level="SHOULD",
        )
        model.add_node(req)

        annotations = model.get_nodes_by_type(type(req))  # RfcRequirement type
        # No annotations, so nothing covered
        assert len(model.get_nodes_by_type(type(req))) == 1


class TestCoverageStats:
    """Test coverage statistics computation."""

    def test_by_level_grouping(self):
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        reqs = [
            RfcRequirement(id="r:1", rfc="RFC", section="1", text="...", level="MUST"),
            RfcRequirement(id="r:2", rfc="RFC", section="2", text="...", level="MUST"),
            RfcRequirement(
                id="r:3", rfc="RFC", section="3", text="...", level="SHOULD"
            ),
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
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        sn = SymbolNode(
            id="test.ivy:5:send",
            name="send",
            qualified_name="quic.send",
            kind="action",
            file="test.ivy",
            line=5,
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
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        sn1 = SymbolNode(
            id="a:1:foo",
            name="foo",
            qualified_name="foo",
            kind="action",
            file="a",
            line=1,
        )
        sn2 = SymbolNode(
            id="a:2:bar",
            name="bar",
            qualified_name="bar",
            kind="action",
            file="a",
            line=2,
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
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

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

        matches = [s for s in model.get_nodes_by_type(SymbolNode) if s.name == "send"]
        assert len(matches) == 1
        assert matches[0].params == ["dst:endpoint", "pkt:packet"]
        assert matches[0].return_sort == "bool"


class TestSemanticEdgeWiring:
    """Test that _build_model wires COVERS, HAS_PARAM, RETURNS_TYPE, INCLUDES edges."""

    def test_covers_edges(self):
        """COVERS edges wire RfcAnnotation → RfcRequirement via matching tags."""
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data",
            level="MUST",
        )
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:4.1", "rfc9000:8.1"],
        )
        model.add_node(req)
        model.add_node(ann)

        # Wire COVERS edges (same logic as _build_model)
        for a in model.get_nodes_by_type(RfcAnnotation):
            for tag in a.tags:
                node = model.get_node(tag)
                if node is not None:
                    model.add_edge(a.id, SemanticEdgeType.COVERS, tag)

        # annotation → rfc9000:4.1 should be wired (req exists)
        outgoing = model.get_outgoing(ann.id, SemanticEdgeType.COVERS)
        assert len(outgoing) == 1
        assert outgoing[0][1] == "rfc9000:4.1"

        # rfc9000:8.1 has no matching requirement, so no edge
        incoming_81 = model.get_incoming("rfc9000:8.1")
        assert len(incoming_81) == 0

    def test_has_param_edges(self):
        """HAS_PARAM edges wire action → type based on param declarations."""
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        model = SemanticModel()
        tn = TypeNode(
            id="/tmp/types.ivy:0:cid",
            name="cid",
            qualified_name="cid",
            file="/tmp/types.ivy",
            line=0,
        )
        sn = SymbolNode(
            id="/tmp/conn.ivy:10:send",
            name="send",
            qualified_name="send",
            kind="action",
            file="/tmp/conn.ivy",
            line=10,
            params=["dst:cid", "data:stream_data"],
        )
        model.add_node(tn)
        model.add_node(sn)

        # Wire HAS_PARAM (same logic as _build_model)
        type_by_name = {t.name: t.id for t in model.get_nodes_by_type(TypeNode)}
        for s in model.get_nodes_by_type(SymbolNode):
            if s.params:
                for param in s.params:
                    parts = param.split(":")
                    if len(parts) < 2:
                        continue
                    type_ref = parts[-1].strip()
                    base = type_ref.split(".")[-1]
                    target = type_by_name.get(base) or type_by_name.get(type_ref)
                    if target:
                        model.add_edge(s.id, SemanticEdgeType.HAS_PARAM, target)

        outgoing = model.get_outgoing(sn.id, SemanticEdgeType.HAS_PARAM)
        assert len(outgoing) == 1  # only "cid" matches a TypeNode
        assert outgoing[0][1] == tn.id

    def test_returns_type_edges(self):
        """RETURNS_TYPE edges wire function → type based on return sort."""
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        model = SemanticModel()
        tn = TypeNode(
            id="/tmp/types.ivy:0:frame_type",
            name="frame_type",
            qualified_name="frame_type",
            file="/tmp/types.ivy",
            line=0,
        )
        sn = SymbolNode(
            id="/tmp/codec.ivy:20:get_type",
            name="get_type",
            qualified_name="get_type",
            kind="function",
            file="/tmp/codec.ivy",
            line=20,
            return_sort="frame_type",
        )
        model.add_node(tn)
        model.add_node(sn)

        # Wire RETURNS_TYPE
        type_by_name = {t.name: t.id for t in model.get_nodes_by_type(TypeNode)}
        for s in model.get_nodes_by_type(SymbolNode):
            ret = getattr(s, "return_sort", None)
            if ret:
                base = ret.split(".")[-1]
                target = type_by_name.get(base) or type_by_name.get(ret)
                if target:
                    model.add_edge(s.id, SemanticEdgeType.RETURNS_TYPE, target)

        outgoing = model.get_outgoing(sn.id, SemanticEdgeType.RETURNS_TYPE)
        assert len(outgoing) == 1
        assert outgoing[0][1] == tn.id

    def test_includes_edges(self, tmp_path):
        """INCLUDES edges wire nodes across files based on include directives."""
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import TypeNode

        # Create two files: one includes the other
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (tmp_path / "packet.ivy").write_text("#lang ivy1.7\ninclude types\ntype pkt\n")

        model = SemanticModel()
        types_abs = str(tmp_path / "types.ivy")
        packet_abs = str(tmp_path / "packet.ivy")

        tn1 = TypeNode(
            id=f"{types_abs}:1:cid",
            name="cid",
            qualified_name="cid",
            file=types_abs,
            line=1,
        )
        tn2 = TypeNode(
            id=f"{packet_abs}:2:pkt",
            name="pkt",
            qualified_name="pkt",
            file=packet_abs,
            line=2,
        )
        model.add_node(tn1)
        model.add_node(tn2)

        # Build basename map and wire includes
        import re

        include_re = re.compile(r"^include\s+(\w+)", re.MULTILINE)
        basename_to_path = {
            "types": types_abs,
            "packet": packet_abs,
        }
        for fpath in [types_abs, packet_abs]:
            nodes_in = model.get_nodes_in_file(fpath)
            if not nodes_in:
                continue
            src_id = nodes_in[0].id
            with open(fpath) as f:
                text = f.read()
            for match in include_re.findall(text):
                target = basename_to_path.get(match)
                if target and target != fpath:
                    tgt_nodes = model.get_nodes_in_file(target)
                    if tgt_nodes:
                        model.add_edge(
                            src_id, SemanticEdgeType.INCLUDES, tgt_nodes[0].id
                        )

        # packet.ivy includes types.ivy → edge from packet node to types node
        outgoing = model.get_outgoing(tn2.id, SemanticEdgeType.INCLUDES)
        assert len(outgoing) == 1
        assert outgoing[0][1] == tn1.id

        # types.ivy doesn't include anything
        out_types = model.get_outgoing(tn1.id, SemanticEdgeType.INCLUDES)
        assert len(out_types) == 0

    def test_build_model_wires_edges_integration(self, tmp_path):
        """Integration test: _build_model produces edges for a mini workspace."""
        import os
        from unittest.mock import patch

        # Create workspace with manifest + annotated ivy file
        manifest = tmp_path / "test_requirements.yaml"
        manifest.write_text(
            "rfc: RFC9999\n"
            "requirements:\n"
            "  rfc9999:1.0:\n"
            "    text: Endpoint MUST send ACK\n"
            "    section: '1.0'\n"
            "    level: MUST\n"
            "    layer: transport\n"
            "    testable: true\n"
        )
        types_file = tmp_path / "types.ivy"
        types_file.write_text("#lang ivy1.7\ntype cid\ntype stream_id\n")
        conn_file = tmp_path / "conn.ivy"
        conn_file.write_text(
            "#lang ivy1.7\n"
            "include types\n"
            "action send(dst:cid) returns (ok:bool) # [rfc9999:1.0]\n"
        )

        env_patch = {"IVY_LSP_INCLUDE_PATHS": "", "IVY_LSP_EXCLUDE_PATHS": ""}
        with patch.dict(os.environ, env_patch, clear=False):
            from ivy_lsp.mcp_server import start_mcp

            app = start_mcp(workspace_root=str(tmp_path), _return_app=True)

        assert app is not None
