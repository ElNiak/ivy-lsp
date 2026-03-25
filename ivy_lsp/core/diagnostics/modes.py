"""Diagnostic severity modes for Ivy LSP.

Three modes control which diagnostics are reported:
- **basic**: errors only
- **standard** (default): errors + warnings + selected hints
- **strict**: everything
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Set

from lsprotocol import types as lsp


@dataclass(frozen=True)
class DiagnosticMode:
    """A named severity filter for diagnostics."""

    name: str
    description: str
    included_severities: Set[lsp.DiagnosticSeverity]

    def accepts(self, severity: lsp.DiagnosticSeverity) -> bool:
        """Return True if this mode includes the given severity."""
        return severity in self.included_severities


BASIC_MODE = DiagnosticMode(
    name="basic",
    description="Errors only",
    included_severities={lsp.DiagnosticSeverity.Error},
)

STANDARD_MODE = DiagnosticMode(
    name="standard",
    description="Errors, warnings, and informational hints",
    included_severities={
        lsp.DiagnosticSeverity.Error,
        lsp.DiagnosticSeverity.Warning,
        lsp.DiagnosticSeverity.Information,
    },
)

STRICT_MODE = DiagnosticMode(
    name="strict",
    description="All diagnostics including hints",
    included_severities={
        lsp.DiagnosticSeverity.Error,
        lsp.DiagnosticSeverity.Warning,
        lsp.DiagnosticSeverity.Information,
        lsp.DiagnosticSeverity.Hint,
    },
)

MODES: Dict[str, DiagnosticMode] = {
    "basic": BASIC_MODE,
    "standard": STANDARD_MODE,
    "strict": STRICT_MODE,
}


def get_active_mode() -> DiagnosticMode:
    """Return the active diagnostic mode based on ``IVY_LSP_DIAGNOSTIC_MODE`` env var."""
    mode_name = os.environ.get("IVY_LSP_DIAGNOSTIC_MODE", "standard").lower()
    return MODES.get(mode_name, STANDARD_MODE)
