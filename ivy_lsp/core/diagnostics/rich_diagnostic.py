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

# Module-level severity / tag mappings — used by from_dict, to_lsp, to_mcp_dict.
_STR_TO_SEVERITY: Dict[str, lsp.DiagnosticSeverity] = {
    "error": lsp.DiagnosticSeverity.Error,
    "warning": lsp.DiagnosticSeverity.Warning,
    "info": lsp.DiagnosticSeverity.Information,
    "information": lsp.DiagnosticSeverity.Information,
    "hint": lsp.DiagnosticSeverity.Hint,
}
_SEVERITY_TO_STR: Dict[lsp.DiagnosticSeverity, str] = {
    lsp.DiagnosticSeverity.Error: "error",
    lsp.DiagnosticSeverity.Warning: "warning",
    lsp.DiagnosticSeverity.Information: "info",
    lsp.DiagnosticSeverity.Hint: "hint",
}
_DIAGNOSTIC_TAG_TO_STR: Dict[lsp.DiagnosticTag, str] = {
    lsp.DiagnosticTag.Unnecessary: "unnecessary",
    lsp.DiagnosticTag.Deprecated: "deprecated",
}

# Heuristic upper bound for source line length when an emit site does not
# provide an explicit `end_character`. IvyDiagnostic instances that need a
# precise end column should set `end_character` explicitly; this default
# is used only as a "highlight to roughly the end of line" approximation.
_DEFAULT_END_COLUMN = 80


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
                    end=lsp.Position(end, _DEFAULT_END_COLUMN),
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
    tags: Optional[List[lsp.DiagnosticTag]] = None

    def __post_init__(self) -> None:
        """Validate fields at construction time to catch malformed emit sites early.

        Raises:
            ValueError: If `message` is empty or whitespace-only, if `line` is
                negative, if `severity` is None, or if `code` is not registered
                in DIAGNOSTIC_REGISTRY.
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
        # The dataclass field default is non-None, but callers can pass None
        # at runtime (e.g. via dict-spread from legacy emit sites). Defensive
        # check survives the type-system "unreachable" pyright lint.
        if self.severity is None:  # type: ignore[unreachable]
            raise ValueError(
                f"IvyDiagnostic.severity must not be None (code={self.code!r})"
            )
        if self.code not in DIAGNOSTIC_REGISTRY:
            raise ValueError(
                f"Diagnostic code {self.code!r} not registered in DIAGNOSTIC_REGISTRY."
                + " Add a DiagnosticDescriptor in ivy_lsp/core/diagnostics/codes.py."
            )

    def to_lsp(self) -> lsp.Diagnostic:
        """Convert to an LSP Diagnostic."""
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        end_line = self.end_line if self.end_line is not None else self.line
        end_char = (
            self.end_character
            if self.end_character is not None
            else _DEFAULT_END_COLUMN
        )

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
            tags=self.tags,
        )

    def to_mcp_dict(self) -> Dict[str, Any]:
        """Convert to an MCP-compatible dict with rich fields."""
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        descriptor = DIAGNOSTIC_REGISTRY.get(self.code)

        result: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "line": self.line + 1,  # MCP uses 1-based lines
            "severity": _SEVERITY_TO_STR.get(self.severity, "hint"),
            "source": self.source,
        }

        if self.tags:
            result["tags"] = [_DIAGNOSTIC_TAG_TO_STR.get(t, str(t)) for t in self.tags]

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

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IvyDiagnostic":
        """Build an IvyDiagnostic from a legacy raw-dict emission.

        Accepts the wire shape used by structural_lint and the MCP tools:
        ``{code, message, line(1-based), severity(str), source?(str)}``.
        Unknown severity strings degrade to Hint. Source defaults to the
        registry descriptor if absent.

        Args:
            d: Raw-dict emission with at minimum `code`, `message`, `line`.

        Returns:
            Validated IvyDiagnostic instance.

        Raises:
            ValueError: If `code`, `message`, or `line` is missing from `d`,
                or if the resulting IvyDiagnostic fails ``__post_init__``
                validation (unregistered code, empty message, negative line).
        """
        from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY

        for required in ("code", "message", "line"):
            if required not in d:
                raise ValueError(
                    f"IvyDiagnostic.from_dict: missing required key {required!r}"
                )

        # Treat severity=None / missing key consistently — fall back to "hint".
        sev_raw = d.get("severity") or "hint"
        severity = _STR_TO_SEVERITY.get(
            str(sev_raw).lower(), lsp.DiagnosticSeverity.Hint
        )

        code = d["code"]
        descriptor = DIAGNOSTIC_REGISTRY.get(code)
        source = d.get("source") or (descriptor.source if descriptor else "ivy")

        # Wire shape uses 1-based lines; IR uses 0-based.
        line_zero = max(0, int(d["line"]) - 1)

        return cls(
            code=code,
            message=str(d["message"]),
            line=line_zero,
            severity=severity,
            source=source,
        )
