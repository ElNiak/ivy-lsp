"""Tests for analysis tool module.

Covers:
- ivy_include_graph: simple includes, transitive includes, missing includes
"""

import json
import os
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
# Tests: ivy_include_graph
# ---------------------------------------------------------------------------


class TestIncludeGraphSimple:
    @pytest.mark.asyncio
    async def test_simple_include(self, tmp_path):
        """Include graph detects direct includes."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        (tmp_path / "conn.ivy").write_text(
            "#lang ivy1.7\n\ninclude types\n\nrelation connected(X:cid, Y:cid)\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_include_graph", {"relative_path": "conn.ivy"})
        data = json.loads(_extract_text(result))
        assert data["file"] == "conn.ivy"
        module_names = [inc["module"] for inc in data["includes"]]
        assert "types" in module_names

    @pytest.mark.asyncio
    async def test_included_by(self, tmp_path):
        """Include graph detects reverse dependencies (included_by)."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        (tmp_path / "conn.ivy").write_text(
            "#lang ivy1.7\n\ninclude types\n\ntype pkt\n"
        )
        (tmp_path / "test.ivy").write_text(
            "#lang ivy1.7\n\ninclude types\n\ntype test_t\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_include_graph", {"relative_path": "types.ivy"}
        )
        data = json.loads(_extract_text(result))
        assert len(data["included_by"]) == 2


class TestIncludeGraphTransitive:
    @pytest.mark.asyncio
    async def test_transitive_includes(self, tmp_path):
        """Include graph computes transitive includes."""
        (tmp_path / "base.ivy").write_text("#lang ivy1.7\n\ntype base_t\n")
        (tmp_path / "mid.ivy").write_text(
            "#lang ivy1.7\n\ninclude base\n\ntype mid_t\n"
        )
        (tmp_path / "top.ivy").write_text("#lang ivy1.7\n\ninclude mid\n\ntype top_t\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_include_graph", {"relative_path": "top.ivy"})
        data = json.loads(_extract_text(result))
        # Should include both "mid" and transitively "base"
        assert "mid" in data["transitive_includes"]
        assert "base" in data["transitive_includes"]


class TestIncludeGraphMissing:
    @pytest.mark.asyncio
    async def test_missing_include_not_resolved(self, tmp_path):
        """Missing includes have resolved_path=None."""
        (tmp_path / "uses_missing.ivy").write_text(
            "#lang ivy1.7\n\ninclude nonexistent\n\ntype t\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_include_graph", {"relative_path": "uses_missing.ivy"}
        )
        data = json.loads(_extract_text(result))
        inc = data["includes"][0]
        assert inc["module"] == "nonexistent"
        assert inc["resolved_path"] is None


class TestIncludeGraphFull:
    @pytest.mark.asyncio
    async def test_full_graph_without_file(self, tmp_path):
        """Include graph returns summary by default, full with detail='full'."""
        (tmp_path / "a.ivy").write_text("#lang ivy1.7\n\ntype a_t\n")
        (tmp_path / "b.ivy").write_text("#lang ivy1.7\n\ninclude a\n\ntype b_t\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))

        # Default: summary mode
        result = await mcp.call_tool("ivy_include_graph", {})
        data = json.loads(_extract_text(result))
        assert data["total_files"] == 2
        assert "entry_points" in data

        # Full mode
        result = await mcp.call_tool("ivy_include_graph", {"detail": "full"})
        data = json.loads(_extract_text(result))
        assert "files" in data
        assert data["total_files"] == 2
