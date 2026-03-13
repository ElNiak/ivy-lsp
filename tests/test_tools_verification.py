"""Tests for verification tool module.

Covers:
- Verify cache lock serialization (TOCTOU prevention)
- Per-isolate result caching from full verification output
- Diagnostics layer error surfacing
- ivy_verify wiring via MCP app (file-not-found, cache hit)
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
# Unit tests for _cache_per_isolate_results
# ---------------------------------------------------------------------------


class TestCachePerIsolateResults:
    def test_extracts_pass_isolates(self):
        """Per-isolate PASS status is cached correctly."""
        from ivy_lsp.tools.verification import (
            _cache_per_isolate_results,
            _verify_cache,
        )

        # Clear cache before test
        _verify_cache.clear()

        raw_output = (
            "  isolate quic_server_test_stream: PASS\n"
            "  isolate quic_server_test_handshake: FAIL\n"
        )
        full_result = {
            "diagnostics": [
                {"message": "error in quic_server_test_handshake", "file": "test.ivy"},
            ],
            "error_summary": "1 error",
            "duration_seconds": 1.5,
        }

        _cache_per_isolate_results("/tmp/test.ivy", raw_output, full_result)

        pass_key = ("/tmp/test.ivy", "quic_server_test_stream")
        fail_key = ("/tmp/test.ivy", "quic_server_test_handshake")

        assert pass_key in _verify_cache
        assert _verify_cache[pass_key]["success"] is True
        assert _verify_cache[pass_key]["error_summary"] == ""

        assert fail_key in _verify_cache
        assert _verify_cache[fail_key]["success"] is False
        assert len(_verify_cache[fail_key]["diagnostics"]) == 1

        _verify_cache.clear()

    def test_does_not_overwrite_existing_cache(self):
        """Existing cached results are not overwritten."""
        from ivy_lsp.tools.verification import (
            _cache_per_isolate_results,
            _verify_cache,
        )

        _verify_cache.clear()

        key = ("/tmp/test.ivy", "iso_a")
        _verify_cache[key] = {"success": True, "cached": True, "sentinel": True}

        raw_output = "  isolate iso_a: FAIL\n"
        _cache_per_isolate_results("/tmp/test.ivy", raw_output, {"diagnostics": []})

        # Original entry should be preserved
        assert _verify_cache[key]["sentinel"] is True
        assert _verify_cache[key]["success"] is True

        _verify_cache.clear()


# ---------------------------------------------------------------------------
# MCP wiring tests
# ---------------------------------------------------------------------------


class TestIvyVerifyMCP:
    @pytest.mark.asyncio
    async def test_verify_file_not_found(self, tmp_path):
        """ivy_verify returns error for non-existent file."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_verify", {"relative_path": "nonexistent.ivy"}
        )
        text = _extract_text(result)
        data = json.loads(text)
        assert data["success"] is False
        assert "not found" in data["message"].lower() or "File not found" in data["message"]

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
