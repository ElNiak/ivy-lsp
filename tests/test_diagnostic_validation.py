"""Validation tests for IvyDiagnostic construction.

Asserts that IvyDiagnostic raises early when required fields are missing,
empty, or reference an unregistered code. Catches malformed emit sites
during migration.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic


class TestIvyDiagnosticValidation:
    def test_valid_construction_succeeds(self):
        diag = IvyDiagnostic(
            code="ivy.syntax.missingLangHeader",
            message="Missing #lang header",
            line=0,
            severity=lsp.DiagnosticSeverity.Warning,
            source="ivy-lint",
        )
        assert diag.code == "ivy.syntax.missingLangHeader"

    def test_empty_message_raises(self):
        with pytest.raises(ValueError, match="message must be non-empty"):
            IvyDiagnostic(
                code="ivy.syntax.missingLangHeader",
                message="",
                line=0,
            )

    def test_whitespace_only_message_raises(self):
        with pytest.raises(ValueError, match="message must be non-empty"):
            IvyDiagnostic(
                code="ivy.syntax.missingLangHeader",
                message="   \n\t  ",
                line=0,
            )

    def test_negative_line_raises(self):
        with pytest.raises(ValueError, match="line must be >= 0"):
            IvyDiagnostic(
                code="ivy.syntax.missingLangHeader",
                message="Missing #lang header",
                line=-1,
            )

    def test_unregistered_code_raises(self):
        with pytest.raises(KeyError, match="not registered"):
            IvyDiagnostic(
                code="ivy.bogus.notRegistered",
                message="Should not exist",
                line=0,
            )

    def test_legacy_hyphenated_code_raises(self):
        # The pre-namespace forms used in raw-dict emit sites (e.g.
        # "missing-lang-header", "param-name-style") must not be accepted —
        # migration replaces them with the namespaced forms from the registry.
        with pytest.raises(KeyError, match="not registered"):
            IvyDiagnostic(
                code="missing-lang-header",
                message="legacy form",
                line=0,
            )
