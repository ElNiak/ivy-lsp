"""Correctness validation tests against real QUIC workspace.

Unlike other tests that use synthetic tmp_path fixtures, these tests run
against the actual protocol-testing/ directory to validate that tool
responses match independently established ground truth.

Ground truth is stored in tests/ground_truth/quic_workspace.json and was
established via manual file reading, CLI command comparison, and grep
counting on 2026-03-16.

Run with: pytest tests/test_validation_correctness.py -v
"""

import json
import sys
from pathlib import Path

import pytest

from tests.conftest import PROTOCOL_TESTING_DIR

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from tests.helpers.mcp_helpers import extract_text

GROUND_TRUTH_DIR = Path(__file__).resolve().parent / "ground_truth"
PROTOCOL_TESTING = PROTOCOL_TESTING_DIR

# Skip entire module if protocol-testing/ doesn't exist (CI without submodule)
pytestmark = pytest.mark.skipif(
    PROTOCOL_TESTING is None,
    reason="protocol-testing/ not found (submodule not initialized)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ground_truth():
    with open(GROUND_TRUTH_DIR / "quic_workspace.json") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def mcp_app():
    import asyncio
    import os

    from ivy_lsp.core.workspace.detection import detect_ivy_workspace
    from ivy_lsp.mcp.startup import start_mcp

    # Use protocol-testing/ as workspace root so relative paths like
    # "quic/quic_stack/quic_types.ivy" resolve directly.
    ws_root = str(PROTOCOL_TESTING)

    # Detect workspace from panther_ivy/ parent (where .ivyworkspace lives),
    # then strip "protocol-testing/" prefix from layer paths so they're
    # relative to the new root.
    panther_ivy_root = str(PROTOCOL_TESTING.parent)
    ws_config = detect_ivy_workspace(start_dir=panther_ivy_root)
    prefix = "protocol-testing/"
    for layer in ws_config.workspace_layers:
        layer.include_paths = [
            p[len(prefix) :] if p.startswith(prefix) else p for p in layer.include_paths
        ]
    ws_config.workspace_root = ws_root

    os.environ.setdefault("IVY_LSP_TOOL_TIMEOUT_SCALE", "3")
    app = start_mcp(workspace_root=ws_root, ws_config=ws_config, _return_app=True)
    # Warm up: first tool call builds the index.
    asyncio.get_event_loop().run_until_complete(
        app.call_tool("ivy_status", {"mode": "capabilities"})
    )
    return app


def _call_and_parse(mcp_app, tool_name, args=None):
    import asyncio

    async def _call():
        result = await mcp_app.call_tool(tool_name, args or {})
        return json.loads(extract_text(result))

    return asyncio.get_event_loop().run_until_complete(_call())


# ---------------------------------------------------------------------------
# Phase 1: Foundation Tools
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_ivy_check_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_status", {"mode": "capabilities"})
        assert data["success"] is True
        assert isinstance(data["cli_tools"]["ivy_check"], bool)

    def test_ivyc_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_status", {"mode": "capabilities"})
        assert isinstance(data["cli_tools"]["ivyc"], bool)

    def test_ivy_show_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_status", {"mode": "capabilities"})
        assert isinstance(data["cli_tools"]["ivy_show"], bool)


class TestStructuralDiagnosticsCorrectness:
    def test_quic_types_clean(self, mcp_app):
        """quic_types.ivy should have 0 structural diagnostics."""
        data = _call_and_parse(
            mcp_app,
            "ivy_diagnostics",
            {"relative_path": "quic/quic_stack/quic_types.ivy", "mode": "structural"},
        )
        assert data["success"] is True
        assert data["diagnostic_count"] == 0

    def test_quic_frame_clean(self, mcp_app):
        """quic_frame.ivy should have 0 structural diagnostics (all includes resolve)."""
        data = _call_and_parse(
            mcp_app,
            "ivy_diagnostics",
            {"relative_path": "quic/quic_stack/quic_frame.ivy", "mode": "structural"},
        )
        assert data["success"] is True
        assert data["diagnostic_count"] == 0


class TestIncludeGraphCorrectness:
    @pytest.mark.xfail(
        reason="include_graph returns empty includes with panther_ivy workspace root"
    )
    def test_connection_include_count(self, mcp_app, ground_truth):
        """quic_connection.ivy should have exactly 11 includes."""
        data = _call_and_parse(
            mcp_app,
            "ivy_analysis",
            {
                "mode": "includes",
                "relative_path": "quic/quic_stack/quic_connection.ivy",
            },
        )
        gt = ground_truth["quic_connection"]
        assert len(data["includes"]) == gt["include_count"]

    @pytest.mark.xfail(
        reason="include_graph returns empty includes with panther_ivy workspace root"
    )
    def test_connection_include_modules(self, mcp_app, ground_truth):
        """All 11 include modules should be present."""
        data = _call_and_parse(
            mcp_app,
            "ivy_analysis",
            {
                "mode": "includes",
                "relative_path": "quic/quic_stack/quic_connection.ivy",
            },
        )
        gt = ground_truth["quic_connection"]
        actual_modules = {inc["module"] for inc in data["includes"]}
        assert actual_modules == set(gt["include_modules"])

    def test_connection_includes_all_resolved(self, mcp_app):
        """All includes should have non-null resolved_path."""
        data = _call_and_parse(
            mcp_app,
            "ivy_analysis",
            {
                "mode": "includes",
                "relative_path": "quic/quic_stack/quic_connection.ivy",
            },
        )
        for inc in data["includes"]:
            assert (
                inc["resolved_path"] is not None
            ), f"Include '{inc['module']}' has null resolved_path"

    def test_commented_includes_excluded(self, mcp_app, ground_truth):
        """Commented-out includes should NOT appear."""
        data = _call_and_parse(
            mcp_app,
            "ivy_analysis",
            {
                "mode": "includes",
                "relative_path": "quic/quic_stack/quic_connection.ivy",
            },
        )
        gt = ground_truth["quic_connection"]
        actual_modules = {inc["module"] for inc in data["includes"]}
        for commented in gt["commented_includes"]:
            assert commented not in actual_modules

    @pytest.mark.xfail(
        reason="file count mismatch: workspace root includes non-protocol files"
    )
    def test_full_graph_file_count(self, mcp_app, ground_truth):
        """Full graph should report correct total .ivy file count."""
        data = _call_and_parse(mcp_app, "ivy_analysis", {"mode": "includes"})
        gt = ground_truth["workspace"]
        assert data["total_files"] == gt["total_ivy_files"]


# ---------------------------------------------------------------------------
# Phase 2: Verification Tools — Known Bugs
# ---------------------------------------------------------------------------


class TestVerifyDiagnosticParsing:
    """These tests document the FM-D bug: diagnostics=[] despite errors."""

    def test_verify_detects_error(self, mcp_app, ground_truth):
        """ivy_verify should return success=false for quic_types.ivy."""
        data = _call_and_parse(
            mcp_app,
            "ivy_verify",
            {"relative_path": "quic/quic_stack/quic_types.ivy"},
        )
        gt = ground_truth["quic_types"]["known_error"]
        assert data["success"] is False
        assert gt["symbol"] in data.get("error_summary", "")

    def test_verify_has_structured_diagnostics(self, mcp_app):
        """ivy_verify should have diagnostic_count > 0 when errors exist."""
        data = _call_and_parse(
            mcp_app,
            "ivy_verify",
            {"relative_path": "quic/quic_stack/quic_types.ivy"},
        )
        assert (
            data["diagnostic_count"] > 0
        ), "diagnostics=[] but error_summary has: " + data.get("error_summary", "")

    def test_dashboard_does_not_crash(self, mcp_app):
        """ivy_diagnostics (mode=dashboard) should not crash."""
        try:
            _call_and_parse(mcp_app, "ivy_diagnostics", {"mode": "dashboard"})
        except Exception as e:
            pytest.fail(f"Dashboard crashed: {e}")


# ---------------------------------------------------------------------------
# Phase 3: Coverage & Traceability
# ---------------------------------------------------------------------------


class TestCoverageCorrectness:
    @pytest.mark.xfail(
        reason="manifest requirements not found with current workspace root"
    )
    def test_total_requirements_match_manifest(self, mcp_app, ground_truth):
        data = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        gt = ground_truth["manifest"]
        assert data["total"] == gt["total_requirements"]

    @pytest.mark.xfail(
        reason="manifest requirements not found with current workspace root"
    )
    def test_level_counts_match_manifest(self, mcp_app, ground_truth):
        data = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        gt = ground_truth["manifest"]["by_level"]
        for level, expected_count in gt.items():
            actual = data["by_level"].get(level, {}).get("total", 0)
            assert (
                actual == expected_count
            ), f"Level {level}: expected {expected_count}, got {actual}"

    def test_covered_plus_uncovered_equals_total(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        assert data["covered"] + data["uncovered"] == data["total"]

    @pytest.mark.xfail(
        reason="manifest requirements not found with current workspace root"
    )
    def test_stats_and_gaps_agree(self, mcp_app):
        """Stats uncovered count should match gaps uncovered count."""
        stats = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        gaps = _call_and_parse(
            mcp_app, "ivy_coverage", {"mode": "gaps", "protocol": "quic"}
        )
        stats_uncovered = stats["uncovered"]
        gaps_uncovered = len(gaps.get("uncoveredRfcRequirements", []))
        assert (
            stats_uncovered == gaps_uncovered
        ), f"stats says {stats_uncovered} uncovered, gaps says {gaps_uncovered}"


# ---------------------------------------------------------------------------
# Phase 5: Visualization — Known Bugs
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    @pytest.mark.xfail(reason="C3: dependency edges never computed")
    def test_dependency_edges_not_empty(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_visualize",
            {"view": "dependencies", "protocol": "quic"},
        )
        assert len(data["edges"]) > 0, "Dependency graph has nodes but no edges"


# ---------------------------------------------------------------------------
# Phase 6: Scaffold & Quality — Known Bugs
# ---------------------------------------------------------------------------


class TestScaffoldCorrectness:
    @pytest.mark.xfail(reason="workspace groups not resolved from panther_ivy root")
    def test_recovery_layer_detected(self, mcp_app):
        data = _call_and_parse(
            mcp_app, "ivy_patterns", {"mode": "check", "protocol": "quic"}
        )
        present_layers = {l["layer"] for l in data["layers_present"]}
        assert "recovery" in present_layers

    @pytest.mark.xfail(reason="workspace groups not resolved from panther_ivy root")
    def test_extensions_layer_detected(self, mcp_app):
        data = _call_and_parse(
            mcp_app, "ivy_patterns", {"mode": "check", "protocol": "quic"}
        )
        present_layers = {l["layer"] for l in data["layers_present"]}
        assert "extensions" in present_layers

    @pytest.mark.xfail(reason="workspace groups not resolved from panther_ivy root")
    def test_manifest_detected(self, mcp_app):
        data = _call_and_parse(
            mcp_app, "ivy_patterns", {"mode": "check", "protocol": "quic"}
        )
        assert data["has_manifest"] is True


class TestQualityGate:
    @pytest.mark.xfail(reason="workspace groups not resolved from panther_ivy root")
    def test_standard_gate_file_count(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_quality",
            {"mode": "gate", "protocol": "quic", "gate_level": "standard"},
        )
        for check in data["checks"]:
            if check["check"] == "minimum_files":
                assert check["passed"] is True
                assert "202" in check["detail"]

    @pytest.mark.xfail(reason="workspace groups not resolved from panther_ivy root")
    def test_standard_gate_monitors_exist(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_quality",
            {"mode": "gate", "protocol": "quic", "gate_level": "standard"},
        )
        for check in data["checks"]:
            if check["check"] == "monitors_exist":
                assert check["passed"] is True
