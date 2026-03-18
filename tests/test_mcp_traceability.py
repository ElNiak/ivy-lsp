"""Tests for MCP traceability and coverage tools.

Covers ivy_extract_requirements (output="structured" and "manifest")
and coverage computation logic.
"""

import json
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_mcp_app(workspace_root=None):
    from ivy_lsp.mcp_server import start_mcp

    root = workspace_root or "/tmp/test-workspace"
    return start_mcp(workspace_root=root, _return_app=True)


def _extract_text(result) -> str:
    if isinstance(result, dict):
        if "result" in result:
            return result["result"]
        return json.dumps(result)
    if isinstance(result, tuple):
        content_blocks = result[0]
        if len(result) > 1 and isinstance(result[1], dict) and "result" in result[1]:
            return result[1]["result"]
        result = content_blocks
    texts = []
    for block in result:
        if hasattr(block, "text"):
            texts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            texts.append(block["text"])
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# ivy_extract_requirements
# ---------------------------------------------------------------------------


class TestIvyExtractRequirements:
    @pytest.mark.asyncio
    async def test_extract_shall_normalized_to_must(self):
        """SHALL -> normalized to MUST per RFC 2119."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": "The sender SHALL send data promptly."},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["total"] >= 1
        reqs = parsed["requirements"]
        assert any(r["level"] == "MUST" for r in reqs)

    @pytest.mark.asyncio
    async def test_extract_must_not_not_greedy(self):
        """'MUST NOT' -> level is 'MUST NOT', not just 'MUST'."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": "The sender MUST NOT send more than the limit."},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["total"] >= 1
        reqs = parsed["requirements"]
        assert any(r["level"] == "MUST NOT" for r in reqs)

    @pytest.mark.asyncio
    async def test_extract_multiple_levels(self):
        """Text with MUST, SHOULD, MAY -> correct by_level counts."""
        mcp = _get_mcp_app()
        rfc_text = (
            "The sender MUST send ACK frames. "
            "The receiver SHOULD validate tokens. "
            "An endpoint MAY send padding."
        )
        result = await mcp.call_tool("ivy_extract_requirements", {"rfc_text": rfc_text})
        parsed = json.loads(_extract_text(result))
        assert parsed["total"] == 3
        assert "by_level" in parsed
        assert parsed["by_level"].get("MUST", 0) >= 1
        assert parsed["by_level"].get("SHOULD", 0) >= 1
        assert parsed["by_level"].get("MAY", 0) >= 1

    @pytest.mark.asyncio
    async def test_extract_empty_text(self):
        """Text with no normative keywords -> total: 0."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": "This text has no normative requirements at all."},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["total"] == 0
        assert parsed["requirements"] == []


# ---------------------------------------------------------------------------
# ivy_extract_requirements (output="manifest")
# ---------------------------------------------------------------------------


class TestIvyGenerateManifest:
    @pytest.mark.asyncio
    async def test_generate_manifest_yaml_valid(self):
        """Generated yaml field contains valid YAML-like content."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {
                "output": "manifest",
                "rfc_name": "RFC9999",
                "rfc_text": "The sender MUST send ACK frames. The receiver SHOULD validate.",
                "protocol": "test_proto",
            },
        )
        parsed = json.loads(_extract_text(result))
        assert "yaml" in parsed
        assert parsed["total_requirements"] >= 2
        # YAML should contain the rfc name
        assert "RFC9999" in parsed["yaml"]

    @pytest.mark.asyncio
    async def test_generate_manifest_suggested_path(self):
        """protocol='quic' -> suggested_path contains 'protocol-testing/quic/'."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {
                "output": "manifest",
                "rfc_name": "RFC9000",
                "rfc_text": "The sender MUST open a connection.",
                "protocol": "quic",
            },
        )
        parsed = json.loads(_extract_text(result))
        assert "suggested_path" in parsed
        assert "protocol-testing/quic/" in parsed["suggested_path"]


# ---------------------------------------------------------------------------
# Coverage computation (direct logic tests)
# ---------------------------------------------------------------------------


class TestCoverageComputation:
    def test_requirement_coverage_by_level(self):
        """2 MUST (1 covered), 1 SHOULD -> correct by_level grouping."""
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

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

        total = len(reqs)
        covered_count = sum(1 for r in reqs if r.id in covered)
        pct = round(100 * covered_count / total, 1)
        assert pct == 33.3


class TestTraceabilityMatrix:
    def test_matrix_covered_vs_uncovered(self):
        """2 requirements, 1 covered by annotation -> correct covered/uncovered."""
        from ivy_lsp.semantic.model import SemanticModel
        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        model = SemanticModel()
        req1 = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="senders MUST NOT send data",
            level="MUST",
        )
        req2 = RfcRequirement(
            id="rfc9000:8.1",
            rfc="RFC9000",
            section="8.1",
            text="receiver SHOULD validate",
            level="SHOULD",
        )
        ann = RfcAnnotation(
            id="/tmp/test.ivy:5:0",
            file="/tmp/test.ivy",
            line=5,
            tags=["rfc9000:4.1"],
        )
        model.add_node(req1)
        model.add_node(req2)
        model.add_node(ann)

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        covered_tags = set()
        for a in annotations:
            covered_tags.update(a.tags)

        covered = [r for r in requirements if r.id in covered_tags]
        uncovered = [r for r in requirements if r.id not in covered_tags]

        assert len(covered) == 1
        assert covered[0].id == "rfc9000:4.1"
        assert len(uncovered) == 1
        assert uncovered[0].id == "rfc9000:8.1"


# ---------------------------------------------------------------------------
# Coverage stats scoping — RfcRequirement has no .file attribute
# ---------------------------------------------------------------------------


class TestCoverageStatsScoping:
    def test_requirement_has_no_file_attribute(self):
        """RfcRequirement has no .file attr — scoping must not filter requirements."""
        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        req = RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
        ann = RfcAnnotation(
            id="f:10:0",
            file="/tmp/quic/quic_types.ivy",
            line=10,
            tags=["rfc9000:4.1"],
        )

        assert not hasattr(req, "file"), "RfcRequirement should not have .file"
        assert hasattr(ann, "file"), "RfcAnnotation should have .file"
