"""Tests for the diagnostic registry, IvyDiagnostic, and severity modes."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from lsprotocol import types as lsp

from ivy_lsp.diagnostics import (
    DIAGNOSTIC_REGISTRY,
    DiagnosticDescriptor,
    DiagnosticMode,
    IvyDiagnostic,
    RelatedLocation,
    get_active_mode,
)
from ivy_lsp.diagnostics.modes import BASIC_MODE, STANDARD_MODE, STRICT_MODE

# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestDiagnosticRegistry:
    def test_registry_populated(self):
        assert len(DIAGNOSTIC_REGISTRY) >= 20

    def test_all_codes_have_dot_prefix(self):
        for code in DIAGNOSTIC_REGISTRY:
            assert code.startswith("ivy."), f"Code {code} doesn't start with ivy."

    def test_code_lookup(self):
        desc = DIAGNOSTIC_REGISTRY["ivy.syntax.missingLangHeader"]
        assert desc.code == "ivy.syntax.missingLangHeader"
        assert desc.has_quick_fix is True
        assert desc.default_severity == lsp.DiagnosticSeverity.Warning

    def test_descriptor_is_frozen(self):
        desc = DIAGNOSTIC_REGISTRY["ivy.syntax.missingLangHeader"]
        with pytest.raises(AttributeError):
            desc.code = "changed"  # type: ignore[misc]

    def test_categories_present(self):
        categories = {code.rsplit(".", 1)[0] for code in DIAGNOSTIC_REGISTRY}
        expected = {
            "ivy.syntax",
            "ivy.naming",
            "ivy.type",
            "ivy.module",
            "ivy.action",
            "ivy.invariant",
            "ivy.rfc",
            "ivy.verify",
        }
        assert expected <= categories

    def test_each_descriptor_has_source(self):
        for code, desc in DIAGNOSTIC_REGISTRY.items():
            assert desc.source, f"{code} missing source"

    def test_each_descriptor_has_explanation(self):
        for code, desc in DIAGNOSTIC_REGISTRY.items():
            assert desc.explanation, f"{code} missing explanation"


# ---------------------------------------------------------------------------
# IvyDiagnostic tests
# ---------------------------------------------------------------------------


class TestIvyDiagnostic:
    def test_basic_to_lsp(self):
        diag = IvyDiagnostic(
            code="ivy.syntax.missingLangHeader",
            message="Missing '#lang ivy1.7' header",
            line=0,
            severity=lsp.DiagnosticSeverity.Warning,
            source="ivy-lint",
        )
        lsp_diag = diag.to_lsp()
        assert lsp_diag.code == "ivy.syntax.missingLangHeader"
        assert lsp_diag.message == "Missing '#lang ivy1.7' header"
        assert lsp_diag.severity == lsp.DiagnosticSeverity.Warning
        assert lsp_diag.source == "ivy-lint"
        assert lsp_diag.range.start.line == 0

    def test_to_lsp_with_related(self):
        diag = IvyDiagnostic(
            code="ivy.naming.duplicateDefinition",
            message="Duplicate 'foo'",
            line=5,
            severity=lsp.DiagnosticSeverity.Error,
            source="ivy",
            related=[
                RelatedLocation(
                    file="/tmp/a.ivy", line=10, message="First defined here"
                ),
                RelatedLocation(
                    file="/tmp/b.ivy", line=20, message="Also defined here"
                ),
            ],
        )
        lsp_diag = diag.to_lsp()
        assert lsp_diag.related_information is not None
        assert len(lsp_diag.related_information) == 2
        assert lsp_diag.related_information[0].message == "First defined here"

    def test_to_lsp_code_description(self):
        diag = IvyDiagnostic(
            code="ivy.syntax.missingLangHeader",
            message="test",
            line=0,
            source="ivy-lint",
        )
        lsp_diag = diag.to_lsp()
        assert lsp_diag.code_description is not None
        assert "ivy.syntax.missingLangHeader" in lsp_diag.code_description.href

    def test_to_mcp_dict(self):
        diag = IvyDiagnostic(
            code="ivy.syntax.missingLangHeader",
            message="Missing header",
            line=0,
            severity=lsp.DiagnosticSeverity.Warning,
            source="ivy-lint",
        )
        d = diag.to_mcp_dict()
        assert d["code"] == "ivy.syntax.missingLangHeader"
        assert d["line"] == 1  # 1-based
        assert d["severity"] == "warning"
        assert "explanation" in d

    def test_to_mcp_dict_with_related(self):
        diag = IvyDiagnostic(
            code="ivy.naming.duplicateDefinition",
            message="Dup",
            line=5,
            severity=lsp.DiagnosticSeverity.Error,
            source="ivy",
            related=[
                RelatedLocation(file="/tmp/a.ivy", line=10, message="here"),
            ],
        )
        d = diag.to_mcp_dict()
        assert "context" in d
        assert len(d["context"]) == 1
        assert d["context"][0]["line"] == 11  # 1-based

    def test_to_mcp_dict_with_suggested_fix(self):
        diag = IvyDiagnostic(
            code="ivy.action.noMonitor",
            message="No monitor",
            line=3,
            severity=lsp.DiagnosticSeverity.Hint,
            source="ivy-semantic",
            suggested_fix="Add an 'after' monitor block.",
        )
        d = diag.to_mcp_dict()
        assert d["suggested_fix"] == "Add an 'after' monitor block."

    def test_to_mcp_dict_quick_fix_flag(self):
        diag = IvyDiagnostic(
            code="ivy.action.noMonitor",
            message="No monitor",
            line=3,
            severity=lsp.DiagnosticSeverity.Hint,
            source="ivy-semantic",
        )
        d = diag.to_mcp_dict()
        assert d.get("has_quick_fix") is True

    def test_roundtrip_preserves_code(self):
        diag = IvyDiagnostic(
            code="ivy.module.unresolvedInclude",
            message="Unresolved include: foo",
            line=7,
            severity=lsp.DiagnosticSeverity.Warning,
            source="ivy-lint",
        )
        lsp_diag = diag.to_lsp()
        mcp_dict = diag.to_mcp_dict()
        assert lsp_diag.code == mcp_dict["code"]

    def test_diagnostic_tags_passthrough(self):
        diag = IvyDiagnostic(
            code="ivy.action.noMonitor",
            message="No monitor",
            line=3,
            severity=lsp.DiagnosticSeverity.Hint,
            source="ivy-semantic",
            data={"tags": [lsp.DiagnosticTag.Unnecessary]},
        )
        lsp_diag = diag.to_lsp()
        assert lsp_diag.tags == [lsp.DiagnosticTag.Unnecessary]


# ---------------------------------------------------------------------------
# RelatedLocation tests
# ---------------------------------------------------------------------------


class TestRelatedLocation:
    def test_to_lsp(self):
        rl = RelatedLocation(file="/tmp/a.ivy", line=10, message="defined here")
        lsp_ri = rl.to_lsp()
        assert lsp_ri.message == "defined here"
        assert lsp_ri.location.uri == "file:///tmp/a.ivy"
        assert lsp_ri.location.range.start.line == 10

    def test_file_uri_passthrough(self):
        rl = RelatedLocation(file="file:///already/uri", line=0, message="x")
        lsp_ri = rl.to_lsp()
        assert lsp_ri.location.uri == "file:///already/uri"


# ---------------------------------------------------------------------------
# Mode tests
# ---------------------------------------------------------------------------


class TestDiagnosticModes:
    def test_basic_accepts_errors_only(self):
        assert BASIC_MODE.accepts(lsp.DiagnosticSeverity.Error)
        assert not BASIC_MODE.accepts(lsp.DiagnosticSeverity.Warning)
        assert not BASIC_MODE.accepts(lsp.DiagnosticSeverity.Information)
        assert not BASIC_MODE.accepts(lsp.DiagnosticSeverity.Hint)

    def test_standard_accepts_errors_warnings_info(self):
        assert STANDARD_MODE.accepts(lsp.DiagnosticSeverity.Error)
        assert STANDARD_MODE.accepts(lsp.DiagnosticSeverity.Warning)
        assert STANDARD_MODE.accepts(lsp.DiagnosticSeverity.Information)
        assert not STANDARD_MODE.accepts(lsp.DiagnosticSeverity.Hint)

    def test_strict_accepts_all(self):
        assert STRICT_MODE.accepts(lsp.DiagnosticSeverity.Error)
        assert STRICT_MODE.accepts(lsp.DiagnosticSeverity.Warning)
        assert STRICT_MODE.accepts(lsp.DiagnosticSeverity.Information)
        assert STRICT_MODE.accepts(lsp.DiagnosticSeverity.Hint)

    def test_get_active_mode_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IVY_LSP_DIAGNOSTIC_MODE", None)
            mode = get_active_mode()
            assert mode.name == "standard"

    def test_get_active_mode_from_env(self):
        with patch.dict(os.environ, {"IVY_LSP_DIAGNOSTIC_MODE": "strict"}):
            mode = get_active_mode()
            assert mode.name == "strict"

    def test_get_active_mode_invalid_falls_back(self):
        with patch.dict(os.environ, {"IVY_LSP_DIAGNOSTIC_MODE": "nonexistent"}):
            mode = get_active_mode()
            assert mode.name == "standard"
