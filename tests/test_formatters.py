"""Tests for ivy_lsp.mcp.tools.formatters — markdown formatting layer."""

from __future__ import annotations

from ivy_lsp.mcp.tools.formatters import (
    _format_generic,
    format_error,
    format_tool_result,
)

# ---------------------------------------------------------------------------
# format_error
# ---------------------------------------------------------------------------


class TestFormatError:
    def test_basic_error(self):
        md = format_error({"success": False, "message": "File not found: x.ivy"})
        assert "**Error**" in md
        assert "File not found: x.ivy" in md

    def test_error_with_note(self):
        md = format_error(
            {
                "success": False,
                "message": "Semantic model unavailable",
                "note": "LSP is still indexing.",
            }
        )
        assert "Semantic model unavailable" in md
        assert "LSP is still indexing." in md

    def test_timeout_error(self):
        md = format_error(
            {
                "success": False,
                "message": "Tool timed out after 180s",
                "timeout": True,
                "tool": "ivy_verify",
            }
        )
        assert "timed out" in md.lower()
        assert "ivy_verify" in md


# ---------------------------------------------------------------------------
# format_tool_result dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_tool_falls_back_to_generic(self):
        md = format_tool_result("nonexistent_tool", {"key": "value"})
        assert "```json" in md
        assert '"key"' in md

    def test_known_tool_dispatches(self):
        md = format_tool_result(
            "ivy_verify",
            {"success": True, "duration_seconds": 1.23, "diagnostics": []},
        )
        assert "Verification" in md
        assert "PASS" in md

    def test_formatter_crash_falls_back(self):
        """If a formatter raises, we get generic JSON rather than a crash."""
        # Pass data that would cause an error in the formatter
        # (None for something expecting dict, etc.)
        md = format_tool_result("ivy_coverage", None)  # type: ignore[arg-type]
        # Should not raise — falls back to generic
        assert md is not None


# ---------------------------------------------------------------------------
# _format_generic
# ---------------------------------------------------------------------------


class TestFormatGeneric:
    def test_renders_json_fence(self):
        md = _format_generic({"foo": "bar", "count": 42})
        assert "```json" in md
        assert '"foo"' in md
        assert "42" in md

    def test_strips_internal_fields(self):
        md = _format_generic({"visible": 1, "_internal": 2})
        assert "visible" in md
        assert "_internal" not in md

    def test_empty_dict(self):
        md = _format_generic({})
        assert "```json" in md


# ---------------------------------------------------------------------------
# ivy_verify
# ---------------------------------------------------------------------------


class TestVerifyFormatter:
    def test_pass(self):
        md = format_tool_result(
            "ivy_verify",
            {
                "success": True,
                "duration_seconds": 2.31,
                "diagnostics": [],
                "cached": True,
            },
        )
        assert "## Verification: PASS" in md
        assert "2.31s" in md
        assert "cached" in md.lower()

    def test_fail_with_diagnostics(self):
        md = format_tool_result(
            "ivy_verify",
            {
                "success": False,
                "duration_seconds": 5.12,
                "diagnostics": [
                    {
                        "file": "quic_types.ivy",
                        "line": 42,
                        "severity": "error",
                        "message": "type mismatch",
                    }
                ],
                "error_summary": "1 error found",
                "counterexample_trace": "step 0: x = 0\nstep 1: x = 1",
            },
        )
        assert "FAIL" in md
        assert "quic_types.ivy:42" in md
        assert "type mismatch" in md
        assert "Counterexample" in md
        assert "step 0" in md

    def test_with_isolate(self):
        md = format_tool_result(
            "ivy_verify",
            {
                "success": True,
                "duration_seconds": 1.0,
                "diagnostics": [],
                "isolate": "quic_connection",
            },
        )
        assert "quic_connection" in md


# ---------------------------------------------------------------------------
# ivy_compile
# ---------------------------------------------------------------------------


class TestCompileFormatter:
    def test_success(self):
        md = format_tool_result(
            "ivy_compile",
            {
                "success": True,
                "duration_seconds": 45.3,
                "diagnostics": [],
                "target": "test",
            },
        )
        assert "Compilation: SUCCESS" in md
        assert "test" in md

    def test_fallback(self):
        md = format_tool_result(
            "ivy_compile",
            {
                "success": True,
                "duration_seconds": 10.0,
                "diagnostics": [],
                "fallback": "subprocess",
                "fallback_reason": "Docker unavailable",
            },
        )
        assert "Fallback" in md
        assert "subprocess" in md


# ---------------------------------------------------------------------------
# ivy_diagnostics
# ---------------------------------------------------------------------------


class TestDiagnosticsFormatter:
    def test_structural_mode(self):
        md = format_tool_result(
            "ivy_diagnostics",
            {
                "success": True,
                "file": "test.ivy",
                "mode": "structural",
                "diagnostics": [],
                "diagnostic_count": 0,
                "error_count": 0,
                "warning_count": 0,
                "hint_count": 0,
                "info_count": 0,
            },
        )
        assert "structural" in md
        assert "test.ivy" in md

    def test_full_mode_with_issues(self):
        md = format_tool_result(
            "ivy_diagnostics",
            {
                "success": True,
                "file": "test.ivy",
                "mode": "full",
                "diagnostics": [
                    {
                        "line": 1,
                        "severity": "error",
                        "message": "bad",
                        "source": "lexer",
                    }
                ],
                "diagnostic_count": 1,
                "error_count": 1,
                "warning_count": 0,
                "hint_count": 0,
                "info_count": 0,
                "by_source": {"lexer": 1},
                "layer_errors": [],
                "partial": False,
            },
        )
        assert "1 error" in md
        assert "lexer" in md


# ---------------------------------------------------------------------------
# ivy_verification_dashboard
# ---------------------------------------------------------------------------


class TestDashboardFormatter:
    def test_basic(self):
        md = format_tool_result(
            "ivy_verification_dashboard",
            {
                "success": True,
                "total_files": 10,
                "verified": 5,
                "failed": 2,
                "pending": 3,
                "cache_size": 7,
                "cache_max": 100,
                "verified_files": ["a.ivy", "b.ivy"],
                "failed_files": ["c.ivy"],
            },
        )
        assert "Dashboard" in md
        assert "10" in md
        assert "a.ivy" in md


# ---------------------------------------------------------------------------
# ivy_include_graph
# ---------------------------------------------------------------------------


class TestIncludeGraphFormatter:
    def test_single_file(self):
        md = format_tool_result(
            "ivy_include_graph",
            {
                "file": "quic_types.ivy",
                "includes": [{"module": "quic_base", "resolved_path": "quic_base.ivy"}],
                "included_by": ["quic_packet.ivy"],
                "transitive_includes": ["quic_base"],
            },
        )
        assert "quic_types.ivy" in md
        assert "quic_base" in md
        assert "Included By" in md

    def test_full_graph(self):
        md = format_tool_result(
            "ivy_include_graph",
            {
                "files": {"a.ivy": {"includes": ["b"]}, "b.ivy": {"includes": []}},
                "total_files": 2,
            },
        )
        assert "full workspace" in md
        assert "2" in md


# ---------------------------------------------------------------------------
# ivy_capabilities
# ---------------------------------------------------------------------------


class TestCapabilitiesFormatter:
    def test_all_available(self):
        md = format_tool_result(
            "ivy_capabilities",
            {"success": True, "ivy_check": True, "ivyc": True, "ivy_show": False},
        )
        assert "[+] `ivy_check`" in md
        assert "[-] `ivy_show`" in md


# ---------------------------------------------------------------------------
# ivy_coverage
# ---------------------------------------------------------------------------


class TestCoverageFormatter:
    def test_stats_mode(self):
        md = format_tool_result(
            "ivy_coverage",
            {
                "total": 97,
                "covered": 58,
                "uncovered": 39,
                "coverage_percent": 59.8,
                "by_level": {
                    "MUST": {
                        "total": 55,
                        "covered": 40,
                        "uncovered": 15,
                        "coverage_percent": 72.7,
                    },
                    "SHOULD": {
                        "total": 30,
                        "covered": 15,
                        "uncovered": 15,
                        "coverage_percent": 50.0,
                    },
                },
                "uncovered_ids": ["rfc9000:4.1:1", "rfc9000:4.2:3"],
            },
        )
        assert "Coverage Statistics" in md
        assert "59.8%" in md
        assert "MUST" in md
        assert "rfc9000:4.1:1" in md

    def test_matrix_mode(self):
        md = format_tool_result(
            "ivy_coverage",
            {
                "total_requirements": 10,
                "covered": 5,
                "uncovered": 5,
                "matrix": [
                    {
                        "id": "rfc9000:4.1:1",
                        "level": "MUST",
                        "covered": True,
                        "assertions": [{"file": "test.ivy", "line": 10}],
                    }
                ],
            },
        )
        assert "Traceability Matrix" in md
        assert "rfc9000:4.1:1" in md

    def test_diff_mode(self):
        md = format_tool_result(
            "ivy_coverage",
            {
                "baseline_coverage_percent": 50.0,
                "current_coverage_percent": 60.0,
                "delta_percent": 10.0,
                "delta_direction": "improved",
                "summary": "Coverage improved by 10.0% (5 recovered)",
                "recovered": ["r1", "r2"],
                "new_gaps": [],
                "unchanged_covered": 40,
                "unchanged_uncovered": 30,
            },
        )
        assert "improved" in md
        assert "10.0%" in md
        assert "Recovered" in md

    def test_gaps_mode(self):
        md = format_tool_result(
            "ivy_coverage",
            {
                "summary": {
                    "totalRfcReqs": 97,
                    "uncoveredRfcCount": 39,
                    "unguardedCount": 5,
                },
                "unguardedStateVars": [{"name": "conn_state", "file": "quic.ivy"}],
                "uncoveredRfcRequirements": [
                    {"id": "rfc9000:4.1", "level": "MUST", "text": "Something"}
                ],
            },
        )
        assert "Coverage Gaps" in md
        assert "conn_state" in md
        assert "rfc9000:4.1" in md


# ---------------------------------------------------------------------------
# ivy_extract_requirements
# ---------------------------------------------------------------------------


class TestExtractRequirementsFormatter:
    def test_structured(self):
        md = format_tool_result(
            "ivy_extract_requirements",
            {
                "requirements": [
                    {"text": "An endpoint MUST do X.", "level": "MUST", "offset": 0},
                ],
                "total": 1,
                "by_level": {"MUST": 1},
            },
        )
        assert "Extracted Requirements" in md
        assert "MUST" in md

    def test_manifest(self):
        md = format_tool_result(
            "ivy_extract_requirements",
            {
                "yaml": "rfc: RFC9000\nrequirements:\n  rfc9000:1:\n    level: MUST\n",
                "total_requirements": 1,
                "by_level": {"MUST": 1},
                "suggested_path": "protocol-testing/quic/rfc9000_requirements.yaml",
            },
        )
        assert "Generated Manifest" in md
        assert "```yaml" in md
        assert "rfc9000" in md


# ---------------------------------------------------------------------------
# ivy_manifest
# ---------------------------------------------------------------------------


class TestManifestFormatter:
    def test_info(self):
        md = format_tool_result(
            "ivy_manifest",
            {
                "manifests": [
                    {
                        "path": "protocol-testing/quic/rfc9000_requirements.yaml",
                        "protocol": "quic",
                        "requirements": 97,
                        "has_metadata": True,
                        "warnings": 0,
                    }
                ],
                "total_manifests": 1,
                "protocols_without_manifests": [],
            },
        )
        assert "Manifest Info" in md
        assert "quic" in md

    def test_validate(self):
        md = format_tool_result(
            "ivy_manifest",
            {
                "results": [
                    {"path": "rfc9000.yaml", "warnings": [], "valid": True},
                    {
                        "path": "rfc9001.yaml",
                        "warnings": ["Missing field"],
                        "valid": False,
                    },
                ],
                "total_manifests": 2,
                "all_valid": False,
            },
        )
        assert "Validation" in md
        assert "Issues found" in md
        assert "[+]" in md
        assert "[X]" in md


# ---------------------------------------------------------------------------
# ivy_patterns
# ---------------------------------------------------------------------------


class TestPatternsFormatter:
    def test_check_mode(self):
        md = format_tool_result(
            "ivy_patterns",
            {
                "protocol": "quic",
                "completeness_score": 71,
                "total_layers": 14,
                "present": 10,
                "missing": 4,
                "total_ivy_files": 50,
                "has_manifest": True,
                "layers_present": [{"layer": "types", "files": ["quic_types.ivy"]}],
                "layers_missing": ["recovery"],
                "suggestions": [
                    {
                        "layer": "recovery",
                        "priority": "medium",
                        "suggestion": "Add recovery layer",
                    }
                ],
            },
        )
        assert "Pattern Check" in md
        assert "71.0%" in md
        assert "recovery" in md


# ---------------------------------------------------------------------------
# ivy_quality
# ---------------------------------------------------------------------------


class TestQualityFormatter:
    def test_gate_passed(self):
        md = format_tool_result(
            "ivy_quality",
            {
                "protocol": "quic",
                "gate_level": "minimal",
                "passed": True,
                "checks_passed": 3,
                "checks_total": 3,
                "checks": [
                    {
                        "check": "lang_header",
                        "level": "minimal",
                        "passed": True,
                        "detail": "All files have #lang header",
                    }
                ],
            },
        )
        assert "PASSED" in md
        assert "3/3" in md

    def test_suggestions(self):
        md = format_tool_result(
            "ivy_quality",
            {
                "suggestions": [
                    {"message": "Consider adding a monitor", "severity": "info"}
                ]
            },
        )
        assert "Suggestions" in md
        assert "monitor" in md


# ---------------------------------------------------------------------------
# ivy_health_check
# ---------------------------------------------------------------------------


class TestHealthCheckFormatter:
    def test_basic(self):
        md = format_tool_result(
            "ivy_health_check",
            {
                "success": True,
                "server": {"workspace": "/tmp/test", "staging_dir": None},
                "model_status": {"state": "ready"},
                "capabilities": {"ivy_check": True, "ivyc": False, "ivy_show": True},
                "workspace_files": 42,
                "tool_metrics": {
                    "ivy_verify": {
                        "call_count": 5,
                        "avg_duration_seconds": 2.3,
                        "error_count": 0,
                        "timeout_count": 0,
                    }
                },
            },
        )
        assert "Health Check" in md
        assert "ready" in md
        assert "[+] `ivy_check`" in md
        assert "[-] `ivyc`" in md
        assert "ivy_verify" in md


# ---------------------------------------------------------------------------
# ivy_scope
# ---------------------------------------------------------------------------


class TestScopeFormatter:
    def test_with_mirrors(self):
        md = format_tool_result(
            "ivy_scope",
            {
                "file": "quic_types.ivy",
                "abs_path": "/tmp/quic_types.ivy",
                "endpoint_mirrors": ["quic_server_test_stream.ivy"],
                "endpoint_mirror_count": 1,
                "tester_role": "client",
                "include_closure_size": 5,
            },
        )
        assert "Scope" in md
        assert "quic_types.ivy" in md
        assert "client" in md


# ---------------------------------------------------------------------------
# ivy_visualize
# ---------------------------------------------------------------------------


class TestVisualizeFormatter:
    def test_dependencies(self):
        md = format_tool_result(
            "ivy_visualize",
            {
                "nodes": [{"name": "send_packet"}, {"name": "recv_packet"}],
                "edges": [{"from": "send_packet", "to": "recv_packet"}],
            },
        )
        assert "Dependencies" in md
        assert "send_packet" in md

    def test_state_machine(self):
        md = format_tool_result(
            "ivy_visualize",
            {
                "states": [{"name": "conn_state", "type": "enum"}],
                "transitions": [
                    {
                        "action": "connect",
                        "reads": ["conn_state"],
                        "writes": ["conn_state"],
                    }
                ],
            },
        )
        assert "State Machine" in md
        assert "conn_state" in md

    def test_layers(self):
        md = format_tool_result(
            "ivy_visualize",
            {
                "layers": [{"name": "quic_types.ivy", "items": ["type1", "type2"]}],
            },
        )
        assert "Layered Overview" in md


# ---------------------------------------------------------------------------
# ivy_model_summary
# ---------------------------------------------------------------------------


class TestModelSummaryFormatter:
    def test_summary_table(self):
        md = format_tool_result(
            "ivy_model_summary",
            {
                "rows": [
                    {"actionName": "send", "counts": {"MUST": 3}, "stateVarCount": 2},
                    {"actionName": "recv", "counts": {"SHOULD": 1}, "stateVarCount": 1},
                ],
            },
        )
        assert "Model Summary" in md
        assert "send" in md
        assert "recv" in md


# ---------------------------------------------------------------------------
# ivy_pattern_scaffold
# ---------------------------------------------------------------------------


class TestPatternScaffoldFormatter:
    def test_generated_code(self):
        md = format_tool_result(
            "ivy_pattern_scaffold",
            {
                "protocol": "minip",
                "pattern": "serdes",
                "code": "#lang ivy1.8\nmodule minip_serdes = { }",
                "suggested_path": "protocol-testing/minip/minip_serdes.ivy",
            },
        )
        assert "Pattern Scaffold" in md
        assert "```ivy" in md
        assert "minip_serdes" in md
