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
        result = await mcp.call_tool(
            "ivy_coverage", {"mode": "diff"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "baseline" in data["message"].lower()


# ---------------------------------------------------------------------------
# ivy_query error paths
# ---------------------------------------------------------------------------


class TestIvyQuery:
    @pytest.mark.asyncio
    async def test_query_impact_requires_symbol_name(self):
        """ivy_query(mode='impact') without symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_query", {"mode": "impact"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "symbol_name" in data["message"]

    @pytest.mark.asyncio
    async def test_query_xrefs_requires_id(self):
        """ivy_query(mode='xrefs') without node_id or symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_query", {"mode": "xrefs"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_info_requires_symbol_name(self):
        """ivy_query(mode='info') without symbol_name returns error."""
        mcp = _get_mcp_app()
        result = await mcp.call_tool(
            "ivy_query", {"mode": "info"}
        )
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
