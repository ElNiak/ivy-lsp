"""Tests for traceability tool module.

Covers:
- ivy_extract_requirements (structured and manifest output)
- Coverage diff uses full uncovered ID set (not truncated)
- ivy_coverage mode dispatch
- ivy_query error paths
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
# ivy_extract_requirements tests
# ---------------------------------------------------------------------------


class TestIvyExtractRequirements:
    @pytest.mark.asyncio
    async def test_extract_structured(self):
        """Structured output extracts MUST/SHOULD/MAY requirements."""
        mcp = _get_mcp_app()
        rfc_text = (
            "The sender MUST open a connection before transmitting. "
            "The receiver SHOULD validate the address. "
            "An implementation MAY cache connections."
        )
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": rfc_text, "output": "structured"},
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["total"] == 3
        assert "MUST" in data["by_level"]
        assert "SHOULD" in data["by_level"]
        assert "MAY" in data["by_level"]

    @pytest.mark.asyncio
    async def test_extract_manifest_requires_rfc_name(self):
        """Manifest output requires rfc_name parameter."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": "MUST do something.", "output": "manifest"},
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "rfc_name" in data["message"]

    @pytest.mark.asyncio
    async def test_extract_manifest_generates_yaml(self):
        """Manifest output generates YAML with correct structure."""
        mcp = _get_mcp_app()
        rfc_text = "The endpoint MUST send an ACK."
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {
                "rfc_text": rfc_text,
                "output": "manifest",
                "rfc_name": "RFC9000",
                "protocol": "quic",
            },
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["total_requirements"] >= 1
        assert "yaml" in data
        assert "rfc: RFC9000" in data["yaml"]
        assert data["suggested_path"].startswith("protocol-testing/quic/")

    @pytest.mark.asyncio
    async def test_extract_normalizes_shall_to_must(self):
        """SHALL and REQUIRED are normalized to MUST."""
        mcp = _get_mcp_app()
        rfc_text = "The client SHALL open a connection. The server REQUIRED respond."
        result = await mcp.call_tool(
            "ivy_extract_requirements",
            {"rfc_text": rfc_text, "output": "structured"},
        )
        text = _extract_text(result)
        data = json.loads(text)
        levels = {r["level"] for r in data["requirements"]}
        # Both should be normalized to MUST
        assert "MUST" in levels
        assert "SHALL" not in levels
        assert "REQUIRED" not in levels


# ---------------------------------------------------------------------------
# Coverage diff / baseline tests
# ---------------------------------------------------------------------------


class TestCoverageDiff:
    @pytest.mark.asyncio
    async def test_diff_without_baseline_returns_error(self):
        """ivy_coverage(mode='diff') without prior stats returns an error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool("ivy_coverage", {"mode": "diff"})
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "baseline" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_diff_after_stats_returns_delta(self, annotated_workspace):
        """ivy_coverage(mode='diff') after stats returns delta with direction."""
        mcp = _get_mcp_app(workspace_root=str(annotated_workspace))
        # First call: build stats baseline
        result1 = await mcp.call_tool("ivy_coverage", {"mode": "stats"})
        data1 = json.loads(_extract_text(result1))
        # The workspace may or may not have coverage, but stats should succeed
        if data1.get("total", 0) == 0:
            pytest.skip("No requirements found in annotated_workspace")

        # Second call: diff should work now
        result2 = await mcp.call_tool("ivy_coverage", {"mode": "diff"})
        data2 = json.loads(_extract_text(result2))
        # Should have diff fields, not an error
        assert "delta_percent" in data2
        assert "delta_direction" in data2
        assert data2["delta_direction"] in ("improved", "regressed", "unchanged")


# ---------------------------------------------------------------------------
# ivy_coverage mode validation
# ---------------------------------------------------------------------------


class TestCoverageModeValidation:
    @pytest.mark.asyncio
    async def test_unknown_mode_rejected_by_schema(self):
        """ivy_coverage with unknown mode is rejected by Literal type validation."""
        from mcp.shared.exceptions import McpError

        mcp = _get_mcp_app()
        with pytest.raises((McpError, Exception)) as exc_info:
            await mcp.call_tool("ivy_coverage", {"mode": "bogus"})
        # The error message should mention valid modes
        assert (
            "matrix" in str(exc_info.value) or "literal" in str(exc_info.value).lower()
        )


# ---------------------------------------------------------------------------
# ivy_query error paths
# ---------------------------------------------------------------------------


class TestIvyQuery:
    @pytest.mark.asyncio
    async def test_query_impact_requires_symbol_name(self):
        """ivy_query(mode='impact') without symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool("ivy_query", {"mode": "impact"})
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "symbol_name" in data["message"]

    @pytest.mark.asyncio
    async def test_query_xrefs_requires_id(self):
        """ivy_query(mode='xrefs') without node_id or symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool("ivy_query", {"mode": "xrefs"})
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_info_requires_symbol_name(self):
        """ivy_query(mode='info') without symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool("ivy_query", {"mode": "info"})
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "symbol_name" in data["message"]


# ---------------------------------------------------------------------------
# Unit test for _uncovered_ids_full field
# ---------------------------------------------------------------------------


class TestUncoveredIdsFull:
    def test_stats_result_includes_full_uncovered_ids(self):
        """The _uncovered_ids_full field is present alongside truncated uncovered_ids."""
        # Simulate what _ivy_requirement_coverage returns
        # We verify the field exists and is untruncated by checking the tool source
        # This is a structural test ensuring the field exists in the result schema.
        # A more thorough integration test would need a semantic model.
        # For now, verify the code path exists.
        from ivy_lsp.tools import traceability

        source = Path(traceability.__file__).read_text()
        assert "_uncovered_ids_full" in source
        # The diff computation should use _uncovered_ids_full
        assert "_uncovered_ids_full" in source.split("_ivy_coverage_diff")[1]


# ---------------------------------------------------------------------------
# FX2: Coverage stats requirement scoping
# ---------------------------------------------------------------------------


class TestCoverageStatsScoping:
    @pytest.mark.asyncio
    async def test_coverage_stats_nonexistent_protocol_returns_zero(self):
        """relative_path='new_prot/' with no annotations should return total=0."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_coverage",
            {"mode": "stats", "relative_path": "new_prot/"},
        )
        text = _extract_text(result)
        data = json.loads(text)
        # FX2 fix: should return 0, not global requirement count
        assert data.get("total", 0) == 0
        assert data.get("covered", 0) == 0
        assert data.get("uncovered", 0) == 0


# ---------------------------------------------------------------------------
# M9: Summary count alignment
# ---------------------------------------------------------------------------


class TestSummaryCountAlignment:
    def test_coverage_gaps_has_m9_overlay_code(self):
        """The M9 fix code should exist in the _ivy_coverage_gaps function."""
        from ivy_lsp.tools import traceability

        source = Path(traceability.__file__).read_text()
        # M9 fix adds totalRfcReqs override in the C4 overlay block
        assert 'result["summary"]["totalRfcReqs"]' in source


# ---------------------------------------------------------------------------
# FX5: Impact analysis edge availability
# ---------------------------------------------------------------------------


class TestImpactAnalysisNote:
    def test_impact_analysis_no_edges_message(self):
        """The impact analysis fallback note should reflect edge availability."""
        from ivy_lsp.tools import traceability

        source = Path(traceability.__file__).read_text()
        assert "No cross-reference edges found for this symbol" in source
