"""Inline pattern checks (_finalize, exported-action monitor) inside compute_diagnostics.

The pattern checks now emit IvyDiagnostic with registered codes.
The function returns List[lsp.Diagnostic]; the check is that the
returned lsp.Diagnostic objects carry the canonical code (set by
IvyDiagnostic.to_lsp()) and the registry-specified source string.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY
from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

pytestmark = pytest.mark.unit


# Source that:
#  - triggers _finalize check  (file named "test_*", has export action, no _finalize)
#  - triggers noMonitor check  (action foo defined AND exported, no before/after monitor)
SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE = (
    "#lang ivy1.7\n" "action foo = { }\n" "export action foo\n"
)


def _make_parse_result_success() -> MagicMock:
    """Return a parse_result stub that signals successful parse with no errors."""
    pr = MagicMock()
    pr.success = True
    pr.errors = []
    pr.lexer_errors = []
    return pr


def test_missing_finalize_emits_registered_code(tmp_path):
    """_finalize check must produce a diagnostic with code ivy.action.missingFinalize."""
    f = tmp_path / "test_foo.ivy"
    f.write_text(SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE)

    diags = compute_diagnostics(
        parser=None,
        source=SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE,
        filepath=str(f),
        parse_result=_make_parse_result_success(),
    )

    codes = [getattr(d, "code", None) for d in diags]
    assert (
        "ivy.action.missingFinalize" in codes
    ), f"Expected ivy.action.missingFinalize in diagnostic codes, got: {codes}"


def test_exported_action_without_monitor_emits_registered_code(tmp_path):
    """ivy.action.noMonitor code must appear when exported action has no monitor."""
    f = tmp_path / "test_bar.ivy"
    f.write_text(SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE)

    diags = compute_diagnostics(
        parser=None,
        source=SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE,
        filepath=str(f),
        parse_result=_make_parse_result_success(),
    )

    codes = [getattr(d, "code", None) for d in diags]
    assert (
        "ivy.action.noMonitor" in codes
    ), f"Expected ivy.action.noMonitor in diagnostic codes, got: {codes}"


def test_pattern_diagnostics_use_canonical_source(tmp_path):
    """Source string on returned lsp.Diagnostic must match the registry descriptor.

    Specifically must be 'ivy-semantic', NOT 'ivy-pattern' (the old coined value).
    """
    f = tmp_path / "test_qux.ivy"
    f.write_text(SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE)

    diags = compute_diagnostics(
        parser=None,
        source=SOURCE_EXPORT_NO_MONITOR_NO_FINALIZE,
        filepath=str(f),
        parse_result=_make_parse_result_success(),
    )

    pattern_codes = {"ivy.action.missingFinalize", "ivy.action.noMonitor"}
    pattern_diags = [d for d in diags if getattr(d, "code", None) in pattern_codes]

    assert pattern_diags, "Expected at least one pattern diagnostic to be emitted"

    for d in pattern_diags:
        # `lsp.Diagnostic.code` is typed as `int | str | None`; narrow to
        # the str case since registry lookups require it.
        assert isinstance(d.code, str), f"non-string code on diagnostic: {d.code!r}"
        descriptor = DIAGNOSTIC_REGISTRY[d.code]
        assert d.source == descriptor.source, (
            f"emit-site source {d.source!r} != descriptor source "
            f"{descriptor.source!r} for code {d.code}"
        )
