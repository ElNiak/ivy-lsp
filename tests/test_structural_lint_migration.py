"""Asserts check_structural_issues returns IvyDiagnostic objects.

Verifies registry-validated codes and correct fields.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic
from ivy_lsp.core.structural_lint import check_lowercase_params, check_structural_issues

pytestmark = pytest.mark.unit


SOURCE_MISSING_HEADER = "type t\n# no #lang line\n"
SOURCE_UNMATCHED_BRACE = (
    "#lang ivy1.7\nmodule m = {\n  type t\n# missing closing brace\n"
)
SOURCE_LOWERCASE_PARAM = "#lang ivy1.7\n" + "relation r(srcvar:t)\n"


def test_returns_ivydiagnostic_list():
    diags = check_structural_issues(SOURCE_MISSING_HEADER, "/tmp/x.ivy")
    assert all(
        isinstance(d, IvyDiagnostic) for d in diags
    ), f"expected List[IvyDiagnostic], got types: {[type(d).__name__ for d in diags]}"


def test_missing_lang_header_uses_namespaced_code():
    diags = check_structural_issues(SOURCE_MISSING_HEADER, "/tmp/x.ivy")
    codes = [d.code for d in diags]
    assert "ivy.syntax.missingLangHeader" in codes


def test_unmatched_brace_uses_namespaced_code():
    diags = check_structural_issues(SOURCE_UNMATCHED_BRACE, "/tmp/x.ivy")
    codes = [d.code for d in diags]
    assert "ivy.syntax.unmatchedBrace" in codes


def test_param_name_style_uses_namespaced_code():
    diags = check_lowercase_params(SOURCE_LOWERCASE_PARAM, "/tmp/x.ivy")
    codes = [d.code for d in diags]
    # Post-rename: only the namespaced form is acceptable.
    assert "ivy.declaration.lowercaseParam" in codes


def test_no_legacy_hyphenated_codes_emitted():
    """Regression fence: structural_lint must never emit hyphenated codes."""
    sources = [SOURCE_MISSING_HEADER, SOURCE_UNMATCHED_BRACE, SOURCE_LOWERCASE_PARAM]
    for src in sources:
        diags = check_structural_issues(src, "/tmp/x.ivy")
        for d in diags:
            assert d.code.startswith(
                "ivy."
            ), f"legacy hyphenated code emitted: {d.code}"


def test_every_returned_diagnostic_has_registry_severity():
    diags = check_structural_issues(SOURCE_MISSING_HEADER, "/tmp/x.ivy")
    for d in diags:
        assert d.severity in (
            lsp.DiagnosticSeverity.Error,
            lsp.DiagnosticSeverity.Warning,
            lsp.DiagnosticSeverity.Information,
            lsp.DiagnosticSeverity.Hint,
        )


def test_every_returned_diagnostic_has_nonempty_message():
    diags = check_structural_issues(SOURCE_MISSING_HEADER, "/tmp/x.ivy")
    for d in diags:
        assert d.message.strip()
