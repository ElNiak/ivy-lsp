"""Tests for pattern tool module.

Covers:
- Scaffold check: layers_present / layers_missing detection
- Content-marker fallback detection
- Mode validation (unknown mode -> error)
- Mode aliasing ("analyze" -> "detect")
- Mode "check" routes to scaffold
"""

import json
import os
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


from tests.helpers.mcp_helpers import extract_text, get_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_layered_workspace(tmp_path, protocol="minip"):
    """Create a workspace with some layers present for scaffold check testing."""
    prot_dir = tmp_path / "protocol-testing" / protocol / f"{protocol}_stack"
    prot_dir.mkdir(parents=True)

    # Types layer (matches {p}_types.ivy pattern)
    (prot_dir / f"{protocol}_types.ivy").write_text(
        "#lang ivy1.7\n\ntype cid\ntype pkt_num\n"
    )
    # Frame layer (matches {p}_frame.ivy with "variant " content marker)
    (prot_dir / f"{protocol}_frame.ivy").write_text(
        "#lang ivy1.7\n\ninclude minip_types\ntype frame_type = {data, ack}\n"
        "# variant dispatch\n"
    )
    # Connection layer (matches {p}_connection.ivy with "relation conn" marker)
    (prot_dir / f"{protocol}_connection.ivy").write_text(
        "#lang ivy1.7\n\ninclude minip_types\nrelation conn_open(C:cid)\n"
    )
    # Test spec (matches {p}_*_test_*.ivy with "export " marker)
    (prot_dir / f"{protocol}_server_test_basic.ivy").write_text(
        "#lang ivy1.7\n\n"
        "include minip_types\n"
        "action send(src:cid, dst:cid)\n"
        "export action send\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: scaffold check (mode="check")
# ---------------------------------------------------------------------------


class TestScaffoldCheck:
    @pytest.mark.asyncio
    async def test_layers_present_detected(self, tmp_path):
        """Scaffold check detects which layers are present."""
        ws = _create_layered_workspace(tmp_path)
        mcp = get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "minip", "mode": "check"}
        )
        data = json.loads(extract_text(result))
        present_names = {lp["layer"] for lp in data["layers_present"]}
        assert "types" in present_names
        assert "connection" in present_names
        assert "test_specs" in present_names

    @pytest.mark.asyncio
    async def test_layers_missing_detected(self, tmp_path):
        """Scaffold check identifies missing layers."""
        ws = _create_layered_workspace(tmp_path)
        mcp = get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "minip", "mode": "check"}
        )
        data = json.loads(extract_text(result))
        # These layers are not created in _create_layered_workspace
        # and their content markers don't appear in any file
        assert "codec" in data["layers_missing"]
        assert "shim" in data["layers_missing"]
        assert "packet" in data["layers_missing"]
        # Some layers are present; verify missing count is sensible
        assert data["missing"] > 0

    @pytest.mark.asyncio
    async def test_content_marker_fallback(self, tmp_path):
        """Content marker detection works when filename pattern does not match."""
        ws = _create_layered_workspace(tmp_path)
        # Add a file that doesn't match the entities pattern by filename,
        # but contains the content marker "instance "
        prot_dir = ws / "protocol-testing" / "minip" / "minip_stack"
        (prot_dir / "minip_other.ivy").write_text(
            "#lang ivy1.7\n\ninstance idx : unbounded_sequence\n"
        )
        mcp = get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "minip", "mode": "check"}
        )
        data = json.loads(extract_text(result))
        present_names = {lp["layer"] for lp in data["layers_present"]}
        assert "entities" in present_names

    @pytest.mark.asyncio
    async def test_completeness_score(self, tmp_path):
        """Scaffold check returns a completeness score."""
        ws = _create_layered_workspace(tmp_path)
        mcp = get_mcp_app(workspace_root=str(ws))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "minip", "mode": "check"}
        )
        data = json.loads(extract_text(result))
        assert "completeness_score" in data
        assert 0 <= data["completeness_score"] <= 100
        assert data["present"] + data["missing"] == data["total_layers"]

    @pytest.mark.asyncio
    async def test_nonexistent_protocol_error(self, tmp_path):
        """Scaffold check returns error for nonexistent protocol directory."""
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "nonexistent", "mode": "check"}
        )
        data = json.loads(extract_text(result))
        assert data["success"] is False
        assert "not found" in data["message"].lower()


# ---------------------------------------------------------------------------
# Tests: mode validation and aliasing
# ---------------------------------------------------------------------------


class TestModeValidation:
    @pytest.mark.asyncio
    async def test_unknown_mode_returns_error(self, tmp_path):
        """ivy_patterns with unknown mode returns error."""
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_patterns", {"protocol": "test", "mode": "bogus"}
        )
        data = json.loads(extract_text(result))
        assert data["success"] is False
        assert "bogus" in data["message"]
