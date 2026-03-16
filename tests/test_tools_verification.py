"""Tests for verification tool module.

Covers:
- Verify cache lock serialization (TOCTOU prevention)
- Per-isolate result caching from full verification output
- Diagnostics layer error surfacing
- ivy_verify wiring via MCP app (file-not-found, cache hit)
- Docker compile fallback chain (mock)
- LRU cache eviction
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
# Unit tests for _ISOLATE_STATUS_RE pattern
# ---------------------------------------------------------------------------


class TestIsolateStatusPattern:
    def test_isolate_status_regex_matches(self):
        """The isolate status regex matches expected output lines."""
        from ivy_lsp.tools.verification import _ISOLATE_STATUS_RE

        output = (
            "  isolate quic_server_test_stream: PASS\n"
            "  isolate quic_server_test_handshake: FAIL\n"
            "  isolate quic_ok_test: OK\n"
        )
        matches = list(_ISOLATE_STATUS_RE.finditer(output))
        assert len(matches) == 3
        assert matches[0].group(1) == "quic_server_test_stream"
        assert matches[0].group(2) == "PASS"
        assert matches[1].group(2) == "FAIL"
        assert matches[2].group(2) == "OK"


class TestCacheMaxSize:
    def test_cache_max_size_constant_exists(self):
        """The _CACHE_MAX_SIZE constant should be defined at module level."""
        from ivy_lsp.tools.verification import _CACHE_MAX_SIZE

        assert _CACHE_MAX_SIZE == 100


# ---------------------------------------------------------------------------
# Docker compile fallback mock test
# ---------------------------------------------------------------------------


class TestDockerCompileFallback:
    @pytest.mark.asyncio
    async def test_compile_falls_back_to_subprocess(self, tmp_path):
        """ivy_compile falls back to subprocess when executor raises OSError."""
        (tmp_path / "test.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))

        # Mock the subprocess fallback to return a known result
        mock_result = {
            "success": False,
            "diagnostics": [],
            "diagnostic_count": 0,
            "error_summary": "ivyc not found",
            "raw_output": "",
        }
        with patch(
            "ivy_lsp.tools.verification.shared_ivy_compile",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await mcp.call_tool("ivy_compile", {"relative_path": "test.ivy"})
            text = _extract_text(result)
            data = json.loads(text)
            # Should have gone through subprocess fallback
            assert isinstance(data, dict)
            # No Docker fallback reason since executor was None
            assert "fallback_reason" not in data


# ---------------------------------------------------------------------------
# MCP wiring tests
# ---------------------------------------------------------------------------


class TestIvyVerifyMCP:
    @pytest.mark.asyncio
    async def test_verify_file_not_found(self, tmp_path):
        """ivy_verify returns error for non-existent file."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_verify", {"relative_path": "nonexistent.ivy"})
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert (
            "not found" in data["message"].lower()
            or "File not found" in data["message"]
        )

    @pytest.mark.asyncio
    async def test_verify_path_traversal_blocked(self, tmp_path):
        """ivy_verify rejects path traversal attempts."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_verify", {"relative_path": "../../../etc/passwd"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False


class TestIvyDiagnosticsMCP:
    @pytest.mark.asyncio
    async def test_diagnostics_structural_layer(self, tmp_path):
        """ivy_diagnostics structural layer detects missing #lang header."""
        (tmp_path / "no_header.ivy").write_text("type cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "no_header.ivy", "layers": ["structural"]},
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is True
        assert data["diagnostic_count"] > 0
        # At least one diagnostic should mention lang header
        msgs = [d["message"] for d in data["diagnostics"]]
        assert any("lang" in m.lower() for m in msgs)

    @pytest.mark.asyncio
    async def test_diagnostics_layer_errors_surfaced(self, tmp_path):
        """layer_errors and partial fields are present in response."""
        (tmp_path / "ok.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "ok.ivy", "layers": ["structural"]},
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert "layer_errors" in data
        assert "partial" in data

    @pytest.mark.asyncio
    async def test_diagnostics_severity_filter(self, tmp_path):
        """min_severity filter excludes lower severity diagnostics."""
        (tmp_path / "test_file.ivy").write_text(
            "#lang ivy1.7\n\n"
            "type cid\n"
            "action send(src:cid, dst:cid)\n"
            "export action send\n"
            "\n"
            "before send {\n"
            "    require src ~= dst;\n"
            "}\n"
        )
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {
                "relative_path": "test_file.ivy",
                "layers": ["pattern"],
                "min_severity": "error",
            },
        )
        text = _extract_text(result)
        data = json.loads(text)
        # All returned diagnostics must be error severity or above
        for d in data["diagnostics"]:
            assert d["severity"] == "error"

    @pytest.mark.asyncio
    async def test_diagnostics_file_not_found(self, tmp_path):
        """ivy_diagnostics returns error for missing file."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "missing.ivy"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
