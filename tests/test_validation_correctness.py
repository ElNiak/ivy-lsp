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

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

GROUND_TRUTH_DIR = Path(__file__).resolve().parent / "ground_truth"
PROTOCOL_TESTING = IVY_ROOT / "protocol-testing"

# Skip entire module if protocol-testing/ doesn't exist (CI without submodule)
pytestmark = pytest.mark.skipif(
    not PROTOCOL_TESTING.exists(),
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
    from ivy_lsp.mcp_server import start_mcp

    return start_mcp(workspace_root=str(IVY_ROOT), _return_app=True)


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


def _call_and_parse(mcp_app, tool_name, args=None):
    import asyncio

    async def _call():
        result = await mcp_app.call_tool(tool_name, args or {})
        return json.loads(_extract_text(result))

    return asyncio.get_event_loop().run_until_complete(_call())


# ---------------------------------------------------------------------------
# Phase 1: Foundation Tools
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_ivy_check_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_capabilities")
        assert data["success"] is True
        assert data["ivy_check"] is True

    def test_ivyc_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_capabilities")
        assert data["ivyc"] is True

    def test_ivy_show_available(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_capabilities")
        assert data["ivy_show"] is True


class TestLintCorrectness:
    def test_quic_types_clean(self, mcp_app):
        """quic_types.ivy should have 0 lint diagnostics."""
        data = _call_and_parse(
            mcp_app, "ivy_lint", {"relative_path": "quic/quic_stack/quic_types.ivy"}
        )
        assert data["success"] is True
        assert data["diagnostic_count"] == 0

    def test_quic_frame_clean(self, mcp_app):
        """quic_frame.ivy should have 0 lint diagnostics (all includes resolve)."""
        data = _call_and_parse(
            mcp_app, "ivy_lint", {"relative_path": "quic/quic_stack/quic_frame.ivy"}
        )
        assert data["success"] is True
        assert data["diagnostic_count"] == 0


class TestIncludeGraphCorrectness:
    def test_connection_include_count(self, mcp_app, ground_truth):
        """quic_connection.ivy should have exactly 11 includes."""
        data = _call_and_parse(
            mcp_app,
            "ivy_include_graph",
            {"relative_path": "quic/quic_stack/quic_connection.ivy"},
        )
        gt = ground_truth["quic_connection"]
        assert len(data["includes"]) == gt["include_count"]

    def test_connection_include_modules(self, mcp_app, ground_truth):
        """All 11 include modules should be present."""
        data = _call_and_parse(
            mcp_app,
            "ivy_include_graph",
            {"relative_path": "quic/quic_stack/quic_connection.ivy"},
        )
        gt = ground_truth["quic_connection"]
        actual_modules = {inc["module"] for inc in data["includes"]}
        assert actual_modules == set(gt["include_modules"])

    def test_connection_includes_all_resolved(self, mcp_app):
        """All includes should have non-null resolved_path."""
        data = _call_and_parse(
            mcp_app,
            "ivy_include_graph",
            {"relative_path": "quic/quic_stack/quic_connection.ivy"},
        )
        for inc in data["includes"]:
            assert (
                inc["resolved_path"] is not None
            ), f"Include '{inc['module']}' has null resolved_path"

    def test_commented_includes_excluded(self, mcp_app, ground_truth):
        """Commented-out includes should NOT appear."""
        data = _call_and_parse(
            mcp_app,
            "ivy_include_graph",
            {"relative_path": "quic/quic_stack/quic_connection.ivy"},
        )
        gt = ground_truth["quic_connection"]
        actual_modules = {inc["module"] for inc in data["includes"]}
        for commented in gt["commented_includes"]:
            assert commented not in actual_modules

    def test_full_graph_file_count(self, mcp_app, ground_truth):
        """Full graph should report correct total .ivy file count."""
        data = _call_and_parse(mcp_app, "ivy_include_graph", {})
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
        """ivy_verification_dashboard should not crash."""
        try:
            _call_and_parse(mcp_app, "ivy_verification_dashboard")
        except Exception as e:
            pytest.fail(f"Dashboard crashed: {e}")


# ---------------------------------------------------------------------------
# Phase 3: Coverage & Traceability
# ---------------------------------------------------------------------------


class TestCoverageCorrectness:
    def test_total_requirements_match_manifest(self, mcp_app, ground_truth):
        data = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        gt = ground_truth["manifest"]
        assert data["total"] == gt["total_requirements"]

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

    def test_stats_and_gaps_agree(self, mcp_app):
        """Stats uncovered count should match gaps uncovered count."""
        stats = _call_and_parse(mcp_app, "ivy_coverage", {"mode": "stats"})
        gaps = _call_and_parse(mcp_app, "ivy_coverage_gaps", {"protocol": "quic"})
        stats_uncovered = stats["uncovered"]
        gaps_uncovered = len(gaps.get("uncoveredRfcRequirements", []))
        assert (
            stats_uncovered == gaps_uncovered
        ), f"stats says {stats_uncovered} uncovered, gaps says {gaps_uncovered}"


# ---------------------------------------------------------------------------
# Phase 3: Symbol Query — Known Bugs
# ---------------------------------------------------------------------------


class TestSymbolQueryCorrectness:
    def test_cid_found_in_correct_file(self, mcp_app):
        data = _call_and_parse(
            mcp_app, "ivy_query_symbol", {"symbol_name": "cid", "protocol": "quic"}
        )
        assert data["found"] is True
        assert "quic_types.ivy" in data.get("type_info", {}).get("file", "")

    def test_cid_correct_line(self, mcp_app):
        data = _call_and_parse(
            mcp_app, "ivy_query_symbol", {"symbol_name": "cid", "protocol": "quic"}
        )
        line = data.get("type_info", {}).get("line", -1)
        assert line in (29, 30), f"Expected line 29 or 30, got {line}"

    def test_quic_packet_type_kind(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_query_symbol",
            {"symbol_name": "quic_packet_type", "protocol": "quic"},
        )
        kind = data.get("symbol_info", {}).get("kind", "")
        assert kind == "object", f"Expected 'object', got '{kind}'"

    @pytest.mark.xfail(reason="C3: semantic edge graph never computed")
    def test_cross_references_not_empty(self, mcp_app):
        """Cid has 1404 LSP references — MCP should find some too."""
        data = _call_and_parse(
            mcp_app,
            "ivy_cross_references",
            {"node_id": "cid"},
        )
        if not data.get("found", False):
            pytest.skip("Node not found with plain name")
        total = len(data.get("incoming", [])) + len(data.get("outgoing", []))
        assert total > 0


# ---------------------------------------------------------------------------
# Phase 5: Visualization — Known Bugs
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    @pytest.mark.xfail(reason="C3: dependency edges never computed")
    def test_dependency_edges_not_empty(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_action_dependency_graph",
            {"protocol": "quic"},
        )
        assert len(data["edges"]) > 0, "Dependency graph has nodes but no edges"


# ---------------------------------------------------------------------------
# Phase 6: Scaffold & Quality — Known Bugs
# ---------------------------------------------------------------------------


class TestScaffoldCorrectness:
    def test_recovery_layer_detected(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_scaffold_check", {"protocol": "quic"})
        present_layers = {l["layer"] for l in data["layers_present"]}
        assert "recovery" in present_layers

    def test_extensions_layer_detected(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_scaffold_check", {"protocol": "quic"})
        present_layers = {l["layer"] for l in data["layers_present"]}
        assert "extensions" in present_layers

    def test_manifest_detected(self, mcp_app):
        data = _call_and_parse(mcp_app, "ivy_scaffold_check", {"protocol": "quic"})
        assert data["has_manifest"] is True


class TestQualityGate:
    def test_standard_gate_file_count(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_quality_gate",
            {"protocol": "quic", "gate_level": "standard"},
        )
        for check in data["checks"]:
            if check["check"] == "minimum_files":
                assert check["passed"] is True
                assert "202" in check["detail"]

    def test_standard_gate_monitors_exist(self, mcp_app):
        data = _call_and_parse(
            mcp_app,
            "ivy_quality_gate",
            {"protocol": "quic", "gate_level": "standard"},
        )
        for check in data["checks"]:
            if check["check"] == "monitors_exist":
                assert check["passed"] is True
