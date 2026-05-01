"""Integration tests: full LSP schema compliance for the diagnostic pipeline.

Drives the pipeline against a deliberately broken .ivy fixture and asserts
every published Diagnostic carries the LSP 3.17 schema fields.

This is the wire-level fence: while test_no_raw_dict_diagnostics.py
enforces emit-time discipline (every producer constructs IvyDiagnostic),
this test verifies the OUTPUT shape — that fields aren't stripped or
dropped anywhere along the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
from lsprotocol import types as lsp

from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "schema_compliance_sample.ivy"

_VALID_SEVERITIES = frozenset(
    (
        lsp.DiagnosticSeverity.Error,
        lsp.DiagnosticSeverity.Warning,
        lsp.DiagnosticSeverity.Information,
        lsp.DiagnosticSeverity.Hint,
    )
)


def _drive_pipeline(filepath: Path) -> List[lsp.Diagnostic]:
    """Run the structural diagnostic pipeline on *filepath*.

    Mirrors the call shape used by test_task_3_2_diagnostics.py: parser=None
    triggers structural-only checks which cover check_structural_issues()
    and its sub-checks without requiring a live IvyParserWrapper.
    """
    source = filepath.read_text()
    return compute_diagnostics(
        parser=None,
        source=source,
        filepath=str(filepath),
    )


def test_fixture_exists():
    """Guard: fixture file must be present before any pipeline test runs."""
    assert _FIXTURE.exists(), f"Fixture missing: {_FIXTURE}"


def test_published_diagnostics_have_full_lsp_schema(tmp_path):
    """Every Diagnostic from the broken fixture must carry all 6 LSP 3.17 fields.

    Fields checked: message (non-empty), code (non-empty), severity (valid
    enum value), source (non-empty), range (not None), and
    code_description.href (non-empty, Phase 1 design requirement).

    If code_description is missing this test fails and reports the exact
    (code, source) pairs where the gap exists — that is the intended
    deliverable for Phase 1 gap analysis.
    """
    target = tmp_path / "schema_compliance_sample.ivy"
    target.write_text(_FIXTURE.read_text())

    diagnostics = _drive_pipeline(target)

    assert diagnostics, (
        "Fixture is supposed to produce diagnostics but none were returned. "
        "Check that check_structural_issues() runs for the broken fixture."
    )

    violations: List[tuple[str, str, List[str]]] = []
    for d in diagnostics:
        missing_fields: List[str] = []

        # message: required, non-empty
        if not (d.message and d.message.strip()):
            missing_fields.append("message")

        # code: required, non-empty
        if not d.code:
            missing_fields.append("code")

        # severity: must be a valid LSP enum value
        if d.severity not in _VALID_SEVERITIES:
            missing_fields.append("severity")

        # source: required, non-empty
        if not d.source:
            missing_fields.append("source")

        # range: required by LSP spec
        if d.range is None:
            missing_fields.append("range")

        # code_description: required by Phase 1 design — every diagnostic
        # must carry a docs link via codeDescription.href.
        if d.code_description is None or not d.code_description.href:
            missing_fields.append("code_description.href")

        if missing_fields:
            code_label = str(d.code) if d.code else "<no code>"
            source_label = str(d.source) if d.source else "<no source>"
            violations.append((code_label, source_label, missing_fields))

    if violations:
        lines = ["LSP schema gaps detected (code | source | missing fields):"]
        for code_label, source_label, missing in violations:
            lines.append(f"  [{code_label}] source={source_label!r} missing={missing}")
        pytest.fail("\n".join(lines))


def test_at_least_one_code_description_present(tmp_path):
    """Sanity: at least one diagnostic in the pipeline does carry code_description.

    This verifies that IvyDiagnostic.to_lsp() wiring works end-to-end for
    diagnostics that go through the IR (not just raw lsp.Diagnostic paths).
    If this fails, code_description injection in rich_diagnostic.py is broken.
    """
    target = tmp_path / "schema_compliance_sample.ivy"
    target.write_text(_FIXTURE.read_text())

    diagnostics = _drive_pipeline(target)

    with_code_description = [
        d
        for d in diagnostics
        if d.code_description is not None and bool(d.code_description.href)
    ]
    assert with_code_description, (
        "No diagnostic in the pipeline carries code_description. "
        "IvyDiagnostic.to_lsp() code_description injection may be broken."
    )
