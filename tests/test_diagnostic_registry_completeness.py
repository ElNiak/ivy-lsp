"""Diagnostic registry completeness fence for namespaced codes.

Asserts that every namespaced (`ivy.*`) code referenced in diagnostic-emit
sites is registered. Hyphenated legacy codes (e.g. `missing-lang-header`,
`param-name-style`, `unguarded-action`) are intentionally excluded — those
emit sites get migrated to canonical namespaced forms in Tasks 4 and 7,
and the migration commit registers any new canonical code at the same time.
"""

import re
from pathlib import Path

import pytest

from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

pytestmark = pytest.mark.unit

EMIT_DIRS = ["ivy_lsp/core", "ivy_lsp/lsp/diagnostics", "ivy_lsp/mcp/tools"]
CODE_PATTERN = re.compile(r'"code":\s*"([^"]+)"')


def _collect_emit_site_codes(repo_root: Path) -> set[str]:
    found: set[str] = set()
    for d in EMIT_DIRS:
        for path in (repo_root / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in CODE_PATTERN.finditer(text):
                code = match.group(1)
                if code in {"undefined", "unknown"}:
                    continue
                found.add(code)
    return found


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
