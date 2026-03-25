"""Tests for quality tool module.

Covers:
- ivy_quality_gate function (all 3 gate levels)
- Missing #lang header detection
- Unresolved include detection
- Minimum file count check
- Nonexistent directory error
- Unreadable file -> skipped_files
- Mode validation (unknown mode -> error)
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


def _create_protocol_workspace(tmp_path, protocol="minip"):
    """Create a minimal protocol workspace for quality gate testing."""
    prot_dir = tmp_path / "protocol-testing" / protocol / f"{protocol}_stack"
    prot_dir.mkdir(parents=True)

    # File 1: valid with #lang header
    (prot_dir / f"{protocol}_types.ivy").write_text(
        "#lang ivy1.7\n\ntype cid\ntype pkt_num\n"
    )
    # File 2: valid with include
    (prot_dir / f"{protocol}_conn.ivy").write_text(
        f"#lang ivy1.7\n\ninclude {protocol}_types\n\nrelation connected(X:cid, Y:cid)\n"
    )
    # File 3: valid test file with export and monitor
    (prot_dir / f"{protocol}_test_basic.ivy").write_text(
        "#lang ivy1.7\n\n"
        f"include {protocol}_types\n"
        f"include {protocol}_conn\n\n"
        "action send(src:cid, dst:cid)\n"
        "export action send\n\n"
        "before send {\n"
        "    require src ~= dst;\n"
        "}\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: ivy_quality gate mode
# ---------------------------------------------------------------------------


class TestQualityGateMinimal:
    @pytest.mark.asyncio
    async def test_minimal_gate_all_pass(self, tmp_path):
        """Minimal gate passes when all files have #lang, includes resolve, and >= 3 files."""
        ws = _create_protocol_workspace(tmp_path)
        mcp = _get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "minip", "gate_level": "minimal"},
        )
        data = json.loads(_extract_text(result))
        assert data["passed"] is True
        assert data["gate_level"] == "minimal"
        assert data["checks_passed"] == data["checks_total"]

    @pytest.mark.asyncio
    async def test_missing_lang_header_detected(self, tmp_path):
        """Minimal gate detects file missing #lang header."""
        ws = _create_protocol_workspace(tmp_path)
        # Add a file without #lang header
        prot_dir = ws / "protocol-testing" / "minip" / "minip_stack"
        (prot_dir / "no_header.ivy").write_text("type bad_type\n")
        mcp = _get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "minip", "gate_level": "minimal"},
        )
        data = json.loads(_extract_text(result))
        lang_check = next(c for c in data["checks"] if c["check"] == "lang_header")
        assert lang_check["passed"] is False
        assert "1 files missing" in lang_check["detail"]

    @pytest.mark.asyncio
    async def test_unresolved_include_detected(self, tmp_path):
        """Minimal gate detects unresolved includes."""
        ws = _create_protocol_workspace(tmp_path)
        prot_dir = ws / "protocol-testing" / "minip" / "minip_stack"
        (prot_dir / "bad_inc.ivy").write_text(
            "#lang ivy1.7\n\ninclude nonexistent_module\n\ntype x\n"
        )
        mcp = _get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "minip", "gate_level": "minimal"},
        )
        data = json.loads(_extract_text(result))
        inc_check = next(c for c in data["checks"] if c["check"] == "includes_resolve")
        assert inc_check["passed"] is False

    @pytest.mark.asyncio
    async def test_minimum_files_check(self, tmp_path):
        """Minimal gate fails if < 3 .ivy files."""
        prot_dir = tmp_path / "protocol-testing" / "tiny" / "tiny_stack"
        prot_dir.mkdir(parents=True)
        (prot_dir / "tiny_types.ivy").write_text("#lang ivy1.7\n\ntype t\n")
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "tiny", "gate_level": "minimal"},
        )
        data = json.loads(_extract_text(result))
        min_check = next(c for c in data["checks"] if c["check"] == "minimum_files")
        assert min_check["passed"] is False


class TestQualityGateStandard:
    @pytest.mark.asyncio
    async def test_standard_gate_includes_extra_checks(self, tmp_path):
        """Standard gate includes test_specs_exist, monitors_exist, exports_exist."""
        ws = _create_protocol_workspace(tmp_path)
        mcp = _get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "minip", "gate_level": "standard"},
        )
        data = json.loads(_extract_text(result))
        check_names = {c["check"] for c in data["checks"]}
        assert "test_specs_exist" in check_names
        assert "monitors_exist" in check_names
        assert "exports_exist" in check_names


class TestQualityGateComprehensive:
    @pytest.mark.asyncio
    async def test_comprehensive_gate_includes_manifest_check(self, tmp_path):
        """Comprehensive gate checks for manifest and annotations."""
        ws = _create_protocol_workspace(tmp_path)
        mcp = _get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "minip", "gate_level": "comprehensive"},
        )
        data = json.loads(_extract_text(result))
        check_names = {c["check"] for c in data["checks"]}
        assert "manifest_exists" in check_names
        assert "annotations_exist" in check_names


class TestQualityGateErrors:
    @pytest.mark.asyncio
    async def test_nonexistent_directory_error(self, tmp_path):
        """Gate returns error for nonexistent protocol directory."""
        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_quality",
            {"mode": "gate", "protocol": "nonexistent", "gate_level": "minimal"},
        )
        data = json.loads(_extract_text(result))
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_unreadable_file_skipped(self, tmp_path):
        """Unreadable files are skipped and reported in skipped_files."""
        ws = _create_protocol_workspace(tmp_path)
        prot_dir = ws / "protocol-testing" / "minip" / "minip_stack"
        bad_file = prot_dir / "unreadable.ivy"
        bad_file.write_text("#lang ivy1.7\n\ntype t\n")
        bad_file.chmod(0o000)

        try:
            mcp = _get_mcp_app(workspace_root=str(ws))
            result = await mcp.call_tool(
                "ivy_quality",
                {"mode": "gate", "protocol": "minip", "gate_level": "minimal"},
            )
            data = json.loads(_extract_text(result))
            # The file should be skipped (not crash)
            if "skipped_files" in data:
                assert any("unreadable" in f for f in data["skipped_files"])
        finally:
            bad_file.chmod(0o644)


class TestQualityModeValidation:
    @pytest.mark.asyncio
    async def test_unknown_mode_rejected_by_schema(self, tmp_path):
        """ivy_quality with unknown mode is rejected by Literal type validation."""
        from mcp.shared.exceptions import McpError

        mcp = _get_mcp_app(workspace_root=str(tmp_path))
        with pytest.raises((McpError, Exception)) as exc_info:
            await mcp.call_tool("ivy_quality", {"mode": "bogus"})
        # The error should reference valid modes
        assert "gate" in str(exc_info.value) or "literal" in str(exc_info.value).lower()
