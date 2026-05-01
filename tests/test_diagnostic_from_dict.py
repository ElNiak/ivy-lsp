"""IvyDiagnostic.from_dict() — convert legacy raw-dict emissions into the IR.

Used as a boundary helper while migrating emitters; once all emit sites
construct IvyDiagnostic directly, this helper sees no use except in tests.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic

pytestmark = pytest.mark.unit


class TestFromDict:
    def test_minimal_dict_with_registered_code(self):
        d = {
            "code": "ivy.syntax.missingLangHeader",
            "message": "Missing #lang header",
            "line": 1,
            "severity": "warning",
            "source": "ivy-lint",
        }
        diag = IvyDiagnostic.from_dict(d)
        assert diag.code == "ivy.syntax.missingLangHeader"
        assert diag.severity == lsp.DiagnosticSeverity.Warning
        assert diag.line == 0  # 1-based wire → 0-based IR

    def test_string_severity_maps_to_lsp_enum(self):
        for wire, expected in [
            ("error", lsp.DiagnosticSeverity.Error),
            ("warning", lsp.DiagnosticSeverity.Warning),
            ("info", lsp.DiagnosticSeverity.Information),
            ("hint", lsp.DiagnosticSeverity.Hint),
        ]:
            diag = IvyDiagnostic.from_dict(
                {
                    "code": "ivy.syntax.missingLangHeader",
                    "message": "x",
                    "line": 1,
                    "severity": wire,
                }
            )
            assert diag.severity == expected, f"{wire} → {expected}"

    def test_unknown_severity_falls_back_to_hint(self):
        diag = IvyDiagnostic.from_dict(
            {
                "code": "ivy.syntax.missingLangHeader",
                "message": "x",
                "line": 1,
                "severity": "potato",
            }
        )
        assert diag.severity == lsp.DiagnosticSeverity.Hint

    def test_default_source_from_registry(self):
        diag = IvyDiagnostic.from_dict(
            {
                "code": "ivy.syntax.missingLangHeader",
                "message": "x",
                "line": 1,
                # no "source" key — pulls from descriptor
            }
        )
        assert diag.source == "ivy-lint"

    def test_one_based_line_normalizes_to_zero_based(self):
        diag = IvyDiagnostic.from_dict(
            {"code": "ivy.syntax.missingLangHeader", "message": "x", "line": 5}
        )
        assert diag.line == 4

    def test_unregistered_code_raises_via_post_init(self):
        with pytest.raises(ValueError, match="not registered"):
            IvyDiagnostic.from_dict(
                {"code": "totally.bogus.code", "message": "x", "line": 1}
            )
