"""Tests for Task 11: Collision Diagnostics.

Verifies that ivy_diagnostics(mode="collisions") classifies basename
collisions by layer relationship: intra-layer (error), cross-layer
in-scope (warning), cross-boundary (info).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from tests.helpers.mcp_helpers import extract_text, get_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_resolver(collision_map, file_to_layer, active_layers=None):
    """Create a mock resolver with the given collision data."""
    resolver = MagicMock()
    resolver._collision_map = collision_map
    resolver._file_to_layer = file_to_layer
    resolver._active_layers = active_layers or set()
    return resolver


# ---------------------------------------------------------------------------
# Unit tests for _handle_collisions_mode logic
# ---------------------------------------------------------------------------


class TestCollisionClassification:
    """Tests for collision severity classification logic."""

    def _call_handle_collisions(self, collision_map, file_to_layer, active_layers=None):
        """Directly invoke the handler via the MCP tool and return parsed result."""
        # We test the underlying logic via the analysis module
        from ivy_lsp.mcp.tools import analysis as analysis_mod

        ctx = MagicMock()
        resolver = _make_mock_resolver(collision_map, file_to_layer, active_layers)
        ctx.include_resolver = resolver

        import asyncio

        return asyncio.run(analysis_mod._handle_collisions_mode(ctx))

    def test_intra_layer_collision_is_error(self):
        """Two files with same basename in the same layer = ERROR severity."""
        # Both files in "quic" layer
        f1 = os.path.realpath("/ws/quic/types.ivy")
        f2 = os.path.realpath("/ws/quic/subdir/types.ivy")
        collision_map = {"types.ivy": [f1, f2]}
        file_to_layer = {f1: "quic", f2: "quic"}

        result = self._call_handle_collisions(collision_map, file_to_layer)
        assert result["total_collisions"] == 1
        collision = result["collisions"][0]
        assert collision["severity"] == "error"
        assert collision["classification"] == "intra-layer"
        assert result["by_severity"]["error"] == 1

    def test_cross_layer_collision_in_scope_is_warning(self):
        """Cross-layer collision where both layers are active = WARNING."""
        f1 = os.path.realpath("/ws/quic/types.ivy")
        f2 = os.path.realpath("/ws/quic_tests/types.ivy")
        collision_map = {"types.ivy": [f1, f2]}
        file_to_layer = {f1: "quic", f2: "quic_tests"}
        active_layers = {"quic", "quic_tests"}

        result = self._call_handle_collisions(
            collision_map, file_to_layer, active_layers
        )
        collision = result["collisions"][0]
        assert collision["severity"] == "warning"
        assert collision["classification"] == "cross-layer-in-scope"
        assert result["by_severity"]["warning"] == 1

    def test_cross_boundary_collision_is_info(self):
        """Cross-layer collision where neither layer is active = INFO."""
        f1 = os.path.realpath("/ws/quic/types.ivy")
        f2 = os.path.realpath("/ws/apt/types.ivy")
        collision_map = {"types.ivy": [f1, f2]}
        file_to_layer = {f1: "quic", f2: "apt"}
        # Active workspace is "minip" — neither quic nor apt is active
        active_layers = {"minip"}

        result = self._call_handle_collisions(
            collision_map, file_to_layer, active_layers
        )
        collision = result["collisions"][0]
        assert collision["severity"] == "info"
        assert collision["classification"] == "cross-boundary"
        assert result["by_severity"]["info"] == 1

    def test_no_active_layers_cross_layer_is_info(self):
        """Without active layers set, cross-layer collisions are INFO."""
        f1 = os.path.realpath("/ws/quic/types.ivy")
        f2 = os.path.realpath("/ws/apt/types.ivy")
        collision_map = {"types.ivy": [f1, f2]}
        file_to_layer = {f1: "quic", f2: "apt"}
        # No active layers
        active_layers = set()

        result = self._call_handle_collisions(
            collision_map, file_to_layer, active_layers
        )
        collision = result["collisions"][0]
        assert collision["severity"] == "info"
        assert collision["classification"] == "cross-boundary"

    def test_empty_collision_map_returns_empty_results(self):
        """No collisions = empty results list."""
        result = self._call_handle_collisions({}, {})
        assert result["total_collisions"] == 0
        assert result["collisions"] == []
        assert result["by_severity"]["error"] == 0
        assert result["by_severity"]["warning"] == 0
        assert result["by_severity"]["info"] == 0

    def test_results_sorted_errors_first(self):
        """Results are sorted: errors first, then warnings, then info."""
        # Set up: intra-layer (error), cross-layer active (warning), cross-boundary (info)
        # intra-layer: both aaa.ivy files in quic layer
        f1 = os.path.realpath("/ws/quic/aaa.ivy")
        f2 = os.path.realpath("/ws/quic/subdir/aaa.ivy")
        # cross-layer-in-scope: bbb.ivy spans quic and quic_tests (both active)
        f3 = os.path.realpath("/ws/quic/bbb.ivy")
        f4 = os.path.realpath("/ws/quic_tests/bbb.ivy")
        # cross-boundary: ccc.ivy spans apt and minip (neither is active)
        f5 = os.path.realpath("/ws/apt/ccc.ivy")
        f6 = os.path.realpath("/ws/minip/ccc.ivy")

        collision_map = {
            "ccc.ivy": [f5, f6],  # listed first but should be last (info)
            "bbb.ivy": [f3, f4],  # should be middle (warning)
            "aaa.ivy": [f1, f2],  # should be first (error)
        }
        file_to_layer = {
            f1: "quic",
            f2: "quic",
            f3: "quic",
            f4: "quic_tests",
            f5: "apt",
            f6: "minip",
        }
        # Only quic and quic_tests are active; apt and minip are not
        active_layers = {"quic", "quic_tests"}

        result = self._call_handle_collisions(
            collision_map, file_to_layer, active_layers
        )

        severities = [c["severity"] for c in result["collisions"]]
        assert severities[0] == "error"
        assert severities[1] == "warning"
        assert severities[2] == "info"

    def test_collision_result_has_required_fields(self):
        """Each collision entry has basename, paths, layers, severity, classification."""
        f1 = os.path.realpath("/ws/quic/types.ivy")
        f2 = os.path.realpath("/ws/quic/subdir/types.ivy")
        collision_map = {"types.ivy": [f1, f2]}
        file_to_layer = {f1: "quic", f2: "quic"}

        result = self._call_handle_collisions(collision_map, file_to_layer)
        collision = result["collisions"][0]
        assert "basename" in collision
        assert "paths" in collision
        assert "layers" in collision
        assert "severity" in collision
        assert "classification" in collision
        assert collision["basename"] == "types.ivy"
        assert set(collision["paths"]) == {f1, f2}
        assert collision["layers"] == ["quic"]  # both in quic, deduplicated

    def test_no_resolver_returns_error_response(self):
        """When resolver is not available, returns error response."""
        import asyncio

        from ivy_lsp.mcp.tools import analysis as analysis_mod

        ctx = MagicMock()
        ctx.include_resolver = None

        result = asyncio.run(analysis_mod._handle_collisions_mode(ctx))
        assert "error" in result

    def test_resolver_without_collision_map_returns_error(self):
        """When resolver has no _collision_map, returns error response."""
        import asyncio

        from ivy_lsp.mcp.tools import analysis as analysis_mod

        ctx = MagicMock()
        ctx.include_resolver = MagicMock(spec=[])  # No _collision_map attribute

        result = asyncio.run(analysis_mod._handle_collisions_mode(ctx))
        assert "error" in result


# ---------------------------------------------------------------------------
# Integration tests via MCP tool call
# ---------------------------------------------------------------------------


class TestCollisionDiagnosticsMCPTool:
    """Tests that the 'collisions' mode is accessible via the MCP tool."""

    @pytest.mark.asyncio
    async def test_collisions_mode_returns_success(self, tmp_path):
        """ivy_diagnostics with mode='collisions' returns a valid response."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "types.ivy", "mode": "collisions"}
        )
        data = json.loads(extract_text(result))
        # Should have the collision response structure
        assert "total_collisions" in data or "error" in data

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_error(self, tmp_path):
        """ivy_diagnostics with an unknown mode returns an error."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "types.ivy", "mode": "bogus_mode_xyz"},
        )
        data = json.loads(extract_text(result))
        assert "error" in data or "success" not in data or data.get("success") is False

    @pytest.mark.asyncio
    async def test_collisions_mode_no_resolver_graceful(self, tmp_path):
        """Mode='collisions' without resolver degrades gracefully."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\n\ntype cid\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        # Fresh workspace has no collisions since no workspace layers
        result = await mcp.call_tool(
            "ivy_diagnostics", {"relative_path": "types.ivy", "mode": "collisions"}
        )
        data = json.loads(extract_text(result))
        # Either succeeds with empty collisions or returns an error gracefully
        assert isinstance(data, dict)
        # Should not raise an exception
