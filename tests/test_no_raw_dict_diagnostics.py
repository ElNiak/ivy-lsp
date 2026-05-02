"""Forbids new raw-dict diagnostic emissions in producer modules.

Phase 1 of the diagnostic redesign migrated every producer to emit
IvyDiagnostic. This test ensures regressions don't slip in.

If you legitimately need a raw dict (e.g. a transitional MCP envelope
during Phase 1.5), add the file path to ALLOWLIST below with a
one-line comment explaining why and a link to the issue tracking
its removal.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Files that may legitimately emit raw dicts during the migration window.
# Each entry must carry a one-line justification. Remove when the site is
# migrated to IvyDiagnostic (Phase 1 gap — tracked for follow-up).
ALLOWLIST: set[str] = {
    # Tasks 4-9 migration gap: diagnostics_tool builds raw-dict payloads
    # for the legacy MCP envelope. Remove when Phase 1.5 unifies the tool.
    "ivy_lsp/mcp/tools/diagnostics_tool.py",
    # Tasks 4-9 migration gap: propagation tool emits raw dicts inline.
    # Remove once the propagation producer is migrated to IvyDiagnostic.
    "ivy_lsp/mcp/tools/propagation.py",
    # Tasks 4-9 migration gap: traceability tool emits raw dicts inline.
    # Remove once the traceability producer is migrated to IvyDiagnostic.
    "ivy_lsp/mcp/tools/traceability.py",
}

EMIT_DIRS = ["ivy_lsp/core", "ivy_lsp/lsp/diagnostics", "ivy_lsp/mcp/tools"]
# Catches both shapes:
#   .append({\n  "line": ..., ...})         # multi-line
#   .append({"line": ..., ...})              # single-line
# Requires "line" AND one of (severity|file|code|message) so internal
# pattern-extraction records like {"name": ..., "line": 0} don't trigger.
# Lookaheads make key order irrelevant.
RAW_DICT_PATTERN = re.compile(
    r"\.append\(\s*\{"
    r'(?=[^}]*"line"\s*:)'
    r'(?=[^}]*"(?:severity|file|code|message)")',
    re.DOTALL,
)


def test_no_raw_dict_diagnostic_emissions():
    """Producer modules must construct IvyDiagnostic, not raw dicts.

    A failure here means an emit site was added or reverted to the
    pre-migration shape. Migrate it (see Tasks 4-9 in the design plan)
    or add it to ALLOWLIST with justification.
    """
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for d in EMIT_DIRS:
        for path in (repo_root / d).rglob("*.py"):
            rel = str(path.relative_to(repo_root))
            if rel in ALLOWLIST:
                continue
            text = path.read_text(encoding="utf-8")
            if RAW_DICT_PATTERN.search(text):
                offenders.append(rel)
    assert not offenders, (
        "Raw-dict diagnostic emissions detected in:\n  - "
        + "\n  - ".join(offenders)
        + "\n\nUse IvyDiagnostic instead. See "
        + "ivy_lsp/core/diagnostics/rich_diagnostic.py."
    )


def test_audit_fence_regex_catches_both_shapes():
    """RAW_DICT_PATTERN must catch both single-line and multi-line raw-dict emissions.

    Phase 1.5 will start migrating the three ALLOWLIST'd MCP files; the fence
    is the only structural guard preventing regressions. A regex that requires
    a literal newline misses single-line dict emissions entirely.
    """
    multi_line = """
    diags.append({
        "line": 1,
        "code": "ivy.x",
        "message": "..",
    })
    """
    single_line = 'diags.append({"line": 1, "code": "ivy.x", "message": ".."})'
    no_emission = 'result = {"line": 1}  # not an .append call'
    internal_record = 'includes.append({"name": "x", "line": 0})'

    assert (
        RAW_DICT_PATTERN.search(multi_line) is not None
    ), "multi-line shape should match"
    assert (
        RAW_DICT_PATTERN.search(single_line) is not None
    ), "single-line shape should match"
    assert (
        RAW_DICT_PATTERN.search(no_emission) is None
    ), "non-append dict literals must not match"
    assert (
        RAW_DICT_PATTERN.search(internal_record) is None
    ), "non-diagnostic append-dicts must not match"
