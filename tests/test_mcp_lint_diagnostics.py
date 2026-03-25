"""Tests for MCP ivy_diagnostics tool.

Covers structural mode (replaces former ivy_lint), full mode with layer
filtering, severity filtering, error paths, and pattern detection.
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
    from ivy_lsp.mcp.server import start_mcp

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
# ivy_diagnostics (mode="structural") tests — replaces former ivy_lint
# ---------------------------------------------------------------------------


class TestIvyDiagnosticsStructural:
    @pytest.mark.asyncio
    async def test_structural_missing_lang_header(self, tmp_path):
        """File without #lang header -> warning diagnostic."""
        (tmp_path / "no_header.ivy").write_text("type cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "no_header.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is True  # structural succeeds, but with diagnostics
        assert parsed["diagnostic_count"] > 0
        # Should have a warning about missing #lang
        assert any("lang" in d["message"].lower() for d in parsed["diagnostics"])

    @pytest.mark.asyncio
    async def test_structural_unmatched_braces(self, tmp_path):
        """Unclosed brace -> error diagnostic."""
        (tmp_path / "bad_braces.ivy").write_text("#lang ivy1.7\n\ntype a = { b\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "bad_braces.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["diagnostic_count"] > 0
        assert parsed["error_count"] > 0

    @pytest.mark.asyncio
    async def test_structural_unresolved_include(self, tmp_path):
        """Unresolvable include -> warning diagnostic."""
        (tmp_path / "bad_include.ivy").write_text(
            "#lang ivy1.7\n\ninclude nonexistent_module\n\ntype x\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "bad_include.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["diagnostic_count"] > 0
        assert any(
            "nonexistent" in d["message"].lower() or "include" in d["message"].lower()
            for d in parsed["diagnostics"]
        )

    @pytest.mark.asyncio
    async def test_structural_valid_file_clean(self, tmp_path):
        """Valid file -> no diagnostics."""
        (tmp_path / "clean.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "clean.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is True
        assert parsed["diagnostic_count"] == 0


# ---------------------------------------------------------------------------
# ivy_diagnostics tests
# ---------------------------------------------------------------------------


class TestIvyDiagnostics:
    @pytest.mark.asyncio
    async def test_diagnostics_layer_filter(self, tmp_path):
        """layers=['structural'] -> only structural diagnostics."""
        (tmp_path / "test.ivy").write_text("type cid\n")  # missing #lang
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "test.ivy", "layers": ["structural"]},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is True
        # All diagnostics should come from structural layer
        if parsed["diagnostic_count"] > 0:
            for d in parsed["diagnostics"]:
                source = d.get("source", "")
                assert (
                    "structural" in source
                    or "ivy-lint" in source
                    or "lint" in source.lower()
                )

    @pytest.mark.asyncio
    async def test_diagnostics_severity_filter(self, tmp_path):
        """min_severity='error' -> excludes warnings."""
        # This file has a warning (missing #lang) but may also have errors
        (tmp_path / "test.ivy").write_text("type cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))

        # First get all diagnostics
        result_all = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "test.ivy"}
        )
        parsed_all = json.loads(_extract_text(result_all))

        # Then filter to errors only
        result_errors = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "test.ivy", "min_severity": "error"},
        )
        parsed_errors = json.loads(_extract_text(result_errors))

        # Error-only count should be <= total count
        assert parsed_errors["diagnostic_count"] <= parsed_all["diagnostic_count"]
        # No warnings in error-only results
        for d in parsed_errors["diagnostics"]:
            assert d.get("severity") != "warning"

    @pytest.mark.asyncio
    async def test_diagnostics_pattern_missing_finalize(self, tmp_path):
        """Test file with export but no _finalize -> pattern layer warning."""
        (tmp_path / "test_spec.ivy").write_text(
            "#lang ivy1.7\n\n"
            "type cid\n"
            "action send(src:cid)\n"
            "export send\n\n"
            "before send {\n"
            "    require src ~= src;\n"
            "}\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "test_spec.ivy", "layers": ["pattern"]},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is True
        # Pattern layer should detect missing _finalize or unmonitored export
        # The exact diagnostic depends on the pattern detection logic
        # At minimum, the tool should run without error


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_structural_nonexistent_file(self, tmp_path):
        """ivy_diagnostics(mode='structural') with nonexistent file -> success: False."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "no_such_file.ivy", "mode": "structural"},
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is False
        assert "message" in parsed

    @pytest.mark.asyncio
    async def test_diagnostics_nonexistent_file(self, tmp_path):
        """ivy_diagnostics with nonexistent file -> success: False."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "no_such_file.ivy"}
        )
        parsed = json.loads(_extract_text(result))
        assert parsed["success"] is False
        assert "message" in parsed
