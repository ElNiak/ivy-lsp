"""Diagnostic code registry for Ivy LSP.

Each diagnostic has a structured code (e.g. ``ivy.syntax.missingLangHeader``),
a title template, explanation, default severity, and metadata about quick-fix
and related-info availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from lsprotocol import types as lsp


@dataclass(frozen=True)
class DiagnosticDescriptor:
    """Immutable descriptor for a single diagnostic code."""

    code: str
    title: str
    explanation: str
    default_severity: lsp.DiagnosticSeverity
    source: str
    has_quick_fix: bool = False
    has_related_info: bool = False


# ---------------------------------------------------------------------------
# Registry: ~28 codes across 8 categories
# ---------------------------------------------------------------------------

DIAGNOSTIC_REGISTRY: Dict[str, DiagnosticDescriptor] = {}


def _reg(d: DiagnosticDescriptor) -> DiagnosticDescriptor:
    DIAGNOSTIC_REGISTRY[d.code] = d
    return d


# -- ivy.syntax.* ----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.syntax.missingLangHeader",
        title="Missing #lang header",
        explanation="Ivy files should start with a #lang directive (e.g. #lang ivy1.7).",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.syntax.unmatchedBrace",
        title="Unmatched brace",
        explanation="Opening or closing brace has no matching counterpart.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy-lint",
        has_quick_fix=True,
        has_related_info=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.syntax.unexpectedToken",
        title="Unexpected token",
        explanation="The parser encountered an unexpected token.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.syntax.lexerError",
        title="Lexer error",
        explanation="The lexer could not tokenize a portion of the source.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy-lsp",
    )
)

# -- ivy.naming.* ----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.naming.duplicateDefinition",
        title="Duplicate definition of '{symbol}'",
        explanation=(
            "The symbol is defined in multiple locations. "
            "Check the related locations to see all definitions."
        ),
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
        has_related_info=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.naming.symbolConflict",
        title="Symbol conflict: '{symbol}'",
        explanation="A symbol conflicts with another definition at the indicated location.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
        has_related_info=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.naming.undefinedSymbol",
        title="Undefined symbol: '{symbol}'",
        explanation="The symbol could not be resolved in any scope.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )
)

# -- ivy.type.* ------------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.type.mismatch",
        title="Type mismatch",
        explanation="Expression type does not match the expected type.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.type.arityError",
        title="Wrong number of arguments",
        explanation="Function or action called with wrong number of arguments.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )
)

# -- ivy.module.* ----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.module.unresolvedInclude",
        title="Unresolved include: '{module}'",
        explanation="The included module could not be found in the workspace.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.module.duplicateInclude",
        title="Duplicate include: '{module}'",
        explanation="The same module is included more than once in this file.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.module.inheritedRequirements",
        title="Inherited requirements from '{module}'",
        explanation=(
            "This include brings requirements into scope from the included module "
            "and its transitive includes."
        ),
        default_severity=lsp.DiagnosticSeverity.Information,
        source="ivy-semantic",
        has_related_info=True,
    )
)

# -- ivy.action.* ----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.action.noMonitor",
        title="Action '{action}' has no monitor",
        explanation=(
            "This action has no before/after monitor requirements. "
            "Add monitors to verify behavior."
        ),
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-semantic",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.action.missingFinalize",
        title="Missing _finalize action",
        explanation=(
            "Test file has export actions but no _finalize action. "
            "Add 'export action _finalize' for end-of-test assertions."
        ),
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-semantic",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.action.unexportedMonitor",
        title="Monitor on non-exported action",
        explanation="A before/after monitor targets an action that is not exported.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-semantic",
    )
)

# -- ivy.invariant.* -------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.invariant.unguardedWrite",
        title="Unguarded state variable write: '{var}'",
        explanation=(
            "State variable is written but not guarded by any requirement. "
            "Add a require/ensure clause to constrain writes."
        ),
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-semantic",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.invariant.highImpactVar",
        title="High-impact state variable: '{var}'",
        explanation=(
            "This state variable is read by many requirements across multiple files. "
            "Changes may have wide-reaching effects."
        ),
        default_severity=lsp.DiagnosticSeverity.Information,
        source="ivy-semantic",
    )
)

# -- ivy.rfc.* -------------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.orphanedTag",
        title="Orphaned RFC tag: [{tag}]",
        explanation=(
            "This bracket tag does not match any loaded requirement manifest. "
            "Check that the RFC number and section are correct."
        ),
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-rfc",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.missingTag",
        title="Assertion without RFC tag",
        explanation=(
            "This assertion (require/ensure/assume/assert) has no RFC bracket "
            "tag annotation. Add a # [rfcNNNN:X.Y] comment."
        ),
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-rfc",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.tagFormatError",
        title="Malformed RFC tag",
        explanation="The bracket tag does not follow the expected format # [rfcNNNN:X.Y].",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-rfc",
    )
)

# -- ivy.verify.* ----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.checkError",
        title="Verification error",
        explanation="ivy_check reported an error during formal verification.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy_check",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.checkWarning",
        title="Verification warning",
        explanation="ivy_check reported a warning during formal verification.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy_check",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.compileError",
        title="Compilation error",
        explanation="C++ compilation failed during ivy_check verification.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy_check",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.isolateFail",
        title="Isolate verification failed: '{isolate}'",
        explanation="The named isolate failed formal verification.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy_check",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.timeout",
        title="Verification timed out",
        explanation="ivy_check did not complete within the configured timeout.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy_check",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.verify.counterexample",
        title="Counterexample found",
        explanation="A counterexample was found during verification, indicating a potential violation.",
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy_check",
        has_related_info=True,
    )
)
