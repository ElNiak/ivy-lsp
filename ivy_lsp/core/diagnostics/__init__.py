"""Structured diagnostic system for Ivy LSP.

Provides Pyright-style error codes, rich messages with related locations,
severity modes, and a unified IvyDiagnostic IR that converts to both
LSP Diagnostic and MCP dict formats.
"""

from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY, DiagnosticDescriptor
from ivy_lsp.core.diagnostics.modes import DiagnosticMode, get_active_mode
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic, RelatedLocation

__all__ = [
    "DIAGNOSTIC_REGISTRY",
    "DiagnosticDescriptor",
    "DiagnosticMode",
    "IvyDiagnostic",
    "RelatedLocation",
    "get_active_mode",
]
