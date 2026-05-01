"""Round-trip and boundary-shape tests for IvyDiagnostic.to_lsp and to_mcp_dict.

These methods are invoked at every Tasks 4-9 emit-site boundary; without
unit tests, a regression in range computation, code description, or key
naming would surface only at runtime in a live editor session.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic, RelatedLocation

pytestmark = pytest.mark.unit


def _make_diag(**overrides):
    base = dict(
        code="ivy.syntax.missingLangHeader",
        message="Missing #lang header",
        line=4,
        severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
    base.update(overrides)
    return IvyDiagnostic(**base)


class TestToLsp:
    def test_minimal_diagnostic_round_trip(self):
        d = _make_diag()
        out = d.to_lsp()
        assert isinstance(out, lsp.Diagnostic)
        assert out.code == "ivy.syntax.missingLangHeader"
        assert out.message == "Missing #lang header"
        assert out.severity == lsp.DiagnosticSeverity.Warning
        assert out.source == "ivy-lint"

    def test_range_uses_character_and_end_character(self):
        d = _make_diag(character=7, end_character=15)
        out = d.to_lsp()
        assert out.range.start.line == 4
        assert out.range.start.character == 7
        assert out.range.end.character == 15

    def test_code_description_present_for_registered_code(self):
        d = _make_diag()
        out = d.to_lsp()
        assert out.code_description is not None
        assert out.code_description.href.endswith("ivy.syntax.missingLangHeader")

    def test_related_information_is_populated_when_related_set(self):
        d = _make_diag(
            related=[RelatedLocation(file="/tmp/other.ivy", line=2, message="see also")]
        )
        out = d.to_lsp()
        assert out.related_information is not None
        assert len(out.related_information) == 1
        assert out.related_information[0].message == "see also"

    def test_tags_from_data_dict(self):
        d = _make_diag(data={"tags": [lsp.DiagnosticTag.Unnecessary]})
        out = d.to_lsp()
        assert out.tags == [lsp.DiagnosticTag.Unnecessary]


class TestToMcpDict:
    def test_minimal_diagnostic_dict_shape(self):
        d = _make_diag()
        out = d.to_mcp_dict()
        assert out["code"] == "ivy.syntax.missingLangHeader"
        assert out["message"] == "Missing #lang header"
        assert out["severity"] == "warning"  # string, not enum
        assert out["source"] == "ivy-lint"

    def test_line_is_one_based_in_mcp_output(self):
        d = _make_diag(line=4)  # 0-based IR
        out = d.to_mcp_dict()
        assert out["line"] == 5  # 1-based MCP wire

    def test_explanation_included_when_descriptor_present(self):
        d = _make_diag()
        out = d.to_mcp_dict()
        assert "explanation" in out and out["explanation"]

    def test_context_key_used_not_relatedinformation(self):
        d = _make_diag(
            related=[RelatedLocation(file="/tmp/other.ivy", line=2, message="see also")]
        )
        out = d.to_mcp_dict()
        assert "context" in out
        assert "relatedInformation" not in out
        assert out["context"][0]["message"] == "see also"
        assert out["context"][0]["line"] == 3  # 1-based

    def test_has_quick_fix_flag_when_descriptor_marks_it(self):
        # ivy.syntax.missingLangHeader has has_quick_fix=True in the registry
        d = _make_diag()
        out = d.to_mcp_dict()
        assert out.get("has_quick_fix") is True

    def test_tags_from_data_propagate_to_mcp_dict(self):
        d = _make_diag(data={"tags": [lsp.DiagnosticTag.Unnecessary]})
        out = d.to_mcp_dict()
        assert out.get("tags") == ["unnecessary"]
