"""Migration tests for parse_ivy_check_output.

Asserts the deep-diagnostic parser returns List[IvyDiagnostic] with
verify-namespaced codes and registry-matching source strings.
"""

import pytest

from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic
from ivy_lsp.lsp.diagnostics.compute import parse_ivy_check_output

pytestmark = pytest.mark.unit


# Standard ivy_check format: "file:line: severity: message"
SAMPLE_INVARIANT_FAIL = (
    "file.ivy:42: error: assertion 'session_unique' failed\n"
    "file.ivy:43: warning: possible non-termination\n"
)

# C++ compiler format: "file.cpp:line:col: error: message"
SAMPLE_COMPILE_ERROR = (
    "file.cpp:7:10: error: type mismatch in foo(x:int, y:str)\n"
    "file.cpp:12:5: fatal error: missing header\n"
)

# Verbose ivy_check format (produced for absolute paths in MCP staging)
SAMPLE_VERBOSE = "file.ivy: line 10: error: isolate iso_security failed\n"


class TestReturnType:
    def test_returns_list(self):
        result = parse_ivy_check_output("")
        assert isinstance(result, list)

    def test_invariant_failure_returns_ivydiagnostic(self):
        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        assert len(result) >= 1
        for d in result:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_compile_error_returns_ivydiagnostic(self):
        result = parse_ivy_check_output(SAMPLE_COMPILE_ERROR)
        assert len(result) >= 1
        for d in result:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_verbose_format_returns_ivydiagnostic(self):
        result = parse_ivy_check_output(SAMPLE_VERBOSE)
        assert len(result) >= 1
        for d in result:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"


class TestVerifyNamespacedCodes:
    def test_invariant_failure_uses_check_error_code(self):
        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        error_diags = [d for d in result if d.severity.value == 1]  # Error
        assert len(error_diags) >= 1
        assert all(d.code == "ivy.verify.checkError" for d in error_diags), (
            f"expected ivy.verify.checkError for error entries, got "
            f"{[d.code for d in error_diags]}"
        )

    def test_invariant_warning_uses_check_warning_code(self):
        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        warn_diags = [d for d in result if d.severity.value == 2]  # Warning
        assert len(warn_diags) >= 1
        assert all(d.code == "ivy.verify.checkWarning" for d in warn_diags), (
            f"expected ivy.verify.checkWarning for warning entries, got "
            f"{[d.code for d in warn_diags]}"
        )

    def test_compile_error_uses_compile_error_code(self):
        result = parse_ivy_check_output(SAMPLE_COMPILE_ERROR)
        error_diags = [d for d in result if d.severity.value == 1]
        assert len(error_diags) >= 1
        assert all(d.code == "ivy.verify.compileError" for d in error_diags), (
            f"expected ivy.verify.compileError for cpp_compiler entries, got "
            f"{[d.code for d in error_diags]}"
        )

    def test_all_codes_use_verify_namespace(self):
        for sample in (SAMPLE_INVARIANT_FAIL, SAMPLE_COMPILE_ERROR, SAMPLE_VERBOSE):
            result = parse_ivy_check_output(sample)
            codes = [d.code for d in result]
            assert all(
                c.startswith("ivy.verify.") for c in codes
            ), f"non-verify-namespaced code found in {codes}"


class TestSourceConsistency:
    """Every emitted diagnostic's source must match the registry descriptor.

    (Lesson from Tasks 5/6/8 source-mismatch findings.)
    """

    def test_emitted_source_matches_descriptor_for_check_errors(self):
        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        for d in result:
            descriptor = DIAGNOSTIC_REGISTRY[d.code]
            assert d.source == descriptor.source, (
                f"emit-site source {d.source!r} != descriptor source "
                f"{descriptor.source!r} for code {d.code}"
            )

    def test_emitted_source_matches_descriptor_for_compile_errors(self):
        result = parse_ivy_check_output(SAMPLE_COMPILE_ERROR)
        for d in result:
            descriptor = DIAGNOSTIC_REGISTRY[d.code]
            assert d.source == descriptor.source, (
                f"emit-site source {d.source!r} != descriptor source "
                f"{descriptor.source!r} for code {d.code}"
            )


class TestLspConversionBoundary:
    """to_lsp() conversion preserves the LSP shape expected by downstream consumers."""

    def test_to_lsp_produces_lsp_diagnostic(self):
        from lsprotocol import types as lsp

        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        lsp_diags = [d.to_lsp() for d in result]
        for d in lsp_diags:
            assert isinstance(d, lsp.Diagnostic)

    def test_to_lsp_severity_preserved(self):
        from lsprotocol import types as lsp

        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        lsp_diags = [d.to_lsp() for d in result]
        error_diags = [
            d for d in lsp_diags if d.severity == lsp.DiagnosticSeverity.Error
        ]
        assert len(error_diags) >= 1

    def test_to_lsp_range_uses_next_line_convention(self):
        """Converted diagnostics use lineno+1, char=0 for full-line span."""
        output = "test.ivy:5: error: something went wrong"
        result = parse_ivy_check_output(output)
        assert len(result) == 1
        d = result[0].to_lsp()
        assert d.range.start.line == 4  # 0-indexed: 5-1
        assert d.range.start.character == 0
        assert d.range.end.line == 5
        assert d.range.end.character == 0

    def test_to_lsp_source_matches_descriptor(self):
        result = parse_ivy_check_output(SAMPLE_INVARIANT_FAIL)
        lsp_diags = [d.to_lsp() for d in result]
        for d in lsp_diags:
            assert d.source == "ivy_check"
