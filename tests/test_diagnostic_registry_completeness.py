"""Diagnostic registry completeness fence for namespaced codes.

Asserts that every namespaced (`ivy.*`) code referenced in diagnostic-emit
sites is registered, and that every `IvyDiagnostic(...)` emit site uses
the registry's source string. Hyphenated legacy codes
(e.g. `missing-lang-header`, `param-name-style`, `unguarded-action`) are
intentionally excluded from the code-registration fence — those emit
sites get migrated to canonical namespaced forms in Tasks 4 and 7, and
the migration commit registers any new canonical code at the same time.

The source-consistency fence
(`test_every_emit_site_source_matches_registry`) closes the recurring
source-string-mismatch failure class
(`feedback_source_mismatch_recurring_in_ir_migrations.md`): a future
edit that drifts an emit-site source from the registered descriptor
fails the fence loudly.
"""

import re
from pathlib import Path

import pytest

from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

pytestmark = pytest.mark.unit

EMIT_DIRS = ["ivy_lsp/core", "ivy_lsp/lsp/diagnostics", "ivy_lsp/mcp/tools"]
# Match both dict-style `"code": "..."` and keyword-arg `code="..."` forms.
# The keyword form is used inside lsp.Diagnostic(...) constructor calls and
# was originally invisible to a dict-only regex (see Task 2 review).
_CODE_DICT_PATTERN = re.compile(r'"code":\s*"([^"]+)"')
_CODE_KWARG_PATTERN = re.compile(r'\bcode\s*=\s*"([^"]+)"')
# Keyword-form source for IvyDiagnostic(...) constructor scanning. The
# dict-form `"source": "..."` does not apply to migrated emit sites since
# raw-dict diagnostics are forbidden by test_no_raw_dict_diagnostics.py.
_SOURCE_KWARG_PATTERN = re.compile(r'\bsource\s*=\s*"([^"]+)"')
_IVY_DIAGNOSTIC_SENTINEL = "IvyDiagnostic("


def _collect_emit_site_codes(repo_root: Path) -> set[str]:
    found: set[str] = set()
    for d in EMIT_DIRS:
        for path in (repo_root / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in (_CODE_DICT_PATTERN, _CODE_KWARG_PATTERN):
                for match in pattern.finditer(text):
                    code = match.group(1)
                    if code in {"undefined", "unknown"}:
                        continue
                    found.add(code)
    return found


def _iter_ivy_diagnostic_bodies(text: str):
    """Yield the kwarg block of every IvyDiagnostic(...) call in *text*.

    Walks the source with a paren-balance counter so nested calls inside
    the constructor (e.g. ``error_info.get("line", 1)``,
    ``max(0, x - 1)``) don't terminate the body early. Yields the
    substring between the opening ``IvyDiagnostic(`` and its matching
    ``)``, suitable for regex-scanning the kwargs.
    """
    i = 0
    sentinel_len = len(_IVY_DIAGNOSTIC_SENTINEL)
    while True:
        start = text.find(_IVY_DIAGNOSTIC_SENTINEL, i)
        if start == -1:
            return
        body_start = start + sentinel_len
        depth = 1
        j = body_start
        while j < len(text) and depth > 0:
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        if depth != 0:
            return  # unbalanced parens; bail to avoid runaway scan
        yield text[body_start : j - 1]
        i = j


def _collect_emit_site_code_source_pairs(repo_root: Path) -> set[tuple[str, str]]:
    """Return every (code, source) pair declared in IvyDiagnostic(...) calls.

    Skips constructor blocks that lack either kwarg — those are covered
    by other fences (registration completeness, raw-dict forbid).
    """
    pairs: set[tuple[str, str]] = set()
    for d in EMIT_DIRS:
        for path in (repo_root / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if _IVY_DIAGNOSTIC_SENTINEL not in text:
                continue
            for body in _iter_ivy_diagnostic_bodies(text):
                code_match = _CODE_KWARG_PATTERN.search(body)
                source_match = _SOURCE_KWARG_PATTERN.search(body)
                if code_match and source_match:
                    pairs.add((code_match.group(1), source_match.group(1)))
    return pairs


def test_every_namespaced_emit_site_code_is_registered():
    """All `ivy.*`-prefixed emit-site codes must be in the registry."""
    repo_root = Path(__file__).resolve().parents[1]
    emit_codes = _collect_emit_site_codes(repo_root)
    namespaced = {c for c in emit_codes if c.startswith("ivy.")}
    unregistered = sorted(c for c in namespaced if c not in DIAGNOSTIC_REGISTRY)
    assert not unregistered, (
        "Namespaced diagnostic code(s) emitted in source but not registered:\n  - "
        + "\n  - ".join(unregistered)
        + "\n\nAdd descriptors in ivy_lsp/core/diagnostics/codes.py."
    )


def test_every_emit_site_source_matches_registry():
    """Every IvyDiagnostic emit site must use the registry's source string.

    Closes the recurring source-string-mismatch failure class. Scans every
    `.py` file under EMIT_DIRS for `IvyDiagnostic(...)` constructor calls,
    extracts the (code, source) pair, and asserts the source matches
    `DIAGNOSTIC_REGISTRY[code].source`.

    Codes not in the registry are skipped here — that gap is reported by
    `test_every_namespaced_emit_site_code_is_registered`. Reporting the
    same code twice would be redundant.
    """
    repo_root = Path(__file__).resolve().parents[1]
    pairs = _collect_emit_site_code_source_pairs(repo_root)

    assert pairs, (
        "Found no (code, source) pairs in IvyDiagnostic(...) emit sites. "
        "Either the scanner regex is broken or no producer migrated to "
        "the IR yet."
    )

    mismatches: list[str] = []
    for code, source in sorted(pairs):
        descriptor = DIAGNOSTIC_REGISTRY.get(code)
        if descriptor is None:
            continue  # surfaced by the registration-completeness test
        if descriptor.source != source:
            mismatches.append(
                f"  {code}: emit-site source={source!r}, "
                + f"registry source={descriptor.source!r}"
            )

    if mismatches:
        pytest.fail(
            "IvyDiagnostic emit-site source strings drifted from the registry:\n"
            + "\n".join(mismatches)
            + "\n\nUpdate either the emit site or the registry in "
            + "ivy_lsp/core/diagnostics/codes.py so they agree."
        )
