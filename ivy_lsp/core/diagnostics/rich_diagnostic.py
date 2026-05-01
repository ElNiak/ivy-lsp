"""Rich diagnostic intermediate representation.

``IvyDiagnostic`` is the shared IR that all diagnostic producers emit.
It converts to LSP ``Diagnostic`` (via ``to_lsp()``) or MCP dict (via
``to_mcp_dict()``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from lsprotocol import types as lsp


@dataclass
class RelatedLocation:
    """A secondary location related to a diagnostic (e.g. other definition site)."""

    file: str
    line: int
    end_line: Optional[int] = None
    message: str = ""

    def to_lsp(self) -> lsp.DiagnosticRelatedInformation:
        """Convert to an LSP DiagnosticRelatedInformation."""
        uri = (
            f"file://{self.file}" if not self.file.startswith("file://") else self.file
        )
        end = self.end_line if self.end_line is not None else self.line
        return lsp.DiagnosticRelatedInformation(
            location=lsp.Location(
                uri=uri,
                range=lsp.Range(
                    start=lsp.Position(self.line, 0),
                    end=lsp.Position(end, 80),
                ),
            ),
            message=self.message,
        )


@dataclass
class IvyDiagnostic:
    """Unified diagnostic IR for Ivy LSP.

    All diagnostic producers create these; final conversion to LSP or MCP
    happens at the boundary.
    """

    code: str
    message: str
    line: int
    end_line: Optional[int] = None
    character: int = 0
    end_character: Optional[int] = None
    severity: lsp.DiagnosticSeverity = lsp.DiagnosticSeverity.Error
    source: str = "ivy"
    related: List[RelatedLocation] = field(default_factory=list)
    suggested_fix: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Validate fields at construction time to catch malformed emit sites early.

        Raises:
            ValueError: If `message` is empty or whitespace-only, if `line` is
                negative, or if `code` is not registered in DIAGNOSTIC_REGISTRY.
        """
        # Local import avoids circular module load at definition time.
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        if not self.message or not self.message.strip():
            raise ValueError(
                f"IvyDiagnostic.message must be non-empty (code={self.code!r})"
            )
        if self.line < 0:
            raise ValueError(
                f"IvyDiagnostic.line must be >= 0 (got {self.line}, code={self.code!r})"
            )
        if self.code not in DIAGNOSTIC_REGISTRY:
            raise ValueError(
                f"Diagnostic code {self.code!r} not registered in DIAGNOSTIC_REGISTRY."
                " Add a DiagnosticDescriptor in ivy_lsp/core/diagnostics/codes.py."
            )

    def to_lsp(self) -> lsp.Diagnostic:
        """Convert to an LSP Diagnostic."""
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        end_line = self.end_line if self.end_line is not None else self.line
        end_char = self.end_character if self.end_character is not None else 80

        related_info = [r.to_lsp() for r in self.related] if self.related else None

        code_description = None
        descriptor = DIAGNOSTIC_REGISTRY.get(self.code)
        if descriptor and descriptor.explanation:
            code_description = lsp.CodeDescription(
                href=f"https://ivy-lsp.readthedocs.io/diagnostics/{self.code}",
            )

        return lsp.Diagnostic(
            range=lsp.Range(
                start=lsp.Position(self.line, self.character),
                end=lsp.Position(end_line, end_char),
            ),
            message=self.message,
            severity=self.severity,
            source=self.source,
            code=self.code,
            code_description=code_description,
            related_information=related_info,
            tags=self.data.get("tags") if self.data else None,
        )

    def to_mcp_dict(self) -> Dict[str, Any]:
        """Convert to an MCP-compatible dict with rich fields."""
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        descriptor = DIAGNOSTIC_REGISTRY.get(self.code)

        sev_names = {
            lsp.DiagnosticSeverity.Error: "error",
            lsp.DiagnosticSeverity.Warning: "warning",
            lsp.DiagnosticSeverity.Information: "info",
            lsp.DiagnosticSeverity.Hint: "hint",
        }

        result: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "line": self.line + 1,  # MCP uses 1-based lines
            "severity": sev_names.get(self.severity, "hint"),
            "source": self.source,
        }

        if descriptor:
            result["explanation"] = descriptor.explanation

        if self.related:
            result["context"] = [
                {
                    "file": os.path.basename(r.file) if r.file else "",
                    "line": r.line + 1,
                    "message": r.message,
                }
                for r in self.related
            ]

        if self.suggested_fix:
            result["suggested_fix"] = self.suggested_fix
        elif descriptor and descriptor.has_quick_fix:
            result["has_quick_fix"] = True

        return result
