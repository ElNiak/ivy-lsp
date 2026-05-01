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
        default_severity=lsp.DiagnosticSeverity.Error,
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

# -- ivy.declaration.* -----------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.declaration.lowercaseParam",
        title="Lowercase parameter in {kind} declaration: '{name}'",
        explanation=(
            "Ivy treats lowercase-initial names as constant references, not "
            "type variables. In relation and function declarations, parameters "
            "must start with an uppercase letter to be treated as universally "
            "quantified logical variables."
        ),
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy-lint",
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

_reg(
    DiagnosticDescriptor(
        code="ivy.action.unguardedWrite",
        title="Unguarded state writes in action '{action}'",
        explanation=(
            "Action writes state variables not guarded by any requirement. "
            "Add require/ensure clauses to constrain writes."
        ),
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-semantic",
        has_quick_fix=True,
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

# ---------------------------------------------------------------------------
# Namespaced emit-site backfill — registered so the IR migration can build
# IvyDiagnostic from these emit sites. Hyphenated legacy codes
# (missing-lang-header, param-name-style, unguarded-action, missing-init,
# empty-init, duplicate-decl, unresolved-include) are intentionally NOT
# registered here; their emit sites are renamed to canonical namespaced
# forms in Tasks 4 and 7.
# ---------------------------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.include.nearMiss",
        title="Unresolved include — did you mean '{suggestion}'?",
        explanation="An include directive could not be resolved but a similarly named file was found nearby.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
)

# -- ivy.state.* -----------------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.state.missingInit",
        title="State variable '{name}' never initialized",
        explanation=(
            "A relation or function is declared but never assigned in an 'after init' block. "
            "Ivy state starts with arbitrary values; add an initializer to make behavior deterministic."
        ),
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.state.emptyInit",
        title="Empty 'after init' block",
        explanation=(
            "An 'after init' block is present but contains no statements. "
            "Either add initializers or remove the empty block."
        ),
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
)

# -- ivy.naming.duplicateDecl ---------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.naming.duplicateDecl",
        title="Duplicate declaration of '{name}'",
        explanation=(
            "A relation, function, type, or individual is declared more than once in the same file. "
            "Remove or rename one of the declarations."
        ),
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy-lint",
        has_related_info=True,
    )
)

# -- ivy.action.unguardedAction -------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.action.unguardedAction",
        title="Action '{action}' modifies state without a 'require' precondition",
        explanation=(
            "The action writes to a state variable but has no 'require' guard. "
            "Add preconditions to prevent unintended state mutations."
        ),
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.type.placeholderTag",
        title="Non-numeric tag value — placeholder?",
        explanation="A # tag= comment contains a non-numeric value, which is likely a placeholder that has not been assigned a real RFC tag number.",
        default_severity=lsp.DiagnosticSeverity.Information,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.type.duplicateTag",
        title="Duplicate RFC tag value",
        explanation="The same numeric tag value appears more than once in the file; each requirement should have a unique tag.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.require.commentedOut",
        title="Commented-out require/ensure/assume/assert statement",
        explanation="A requirement keyword appears inside a comment; consider re-enabling or removing it to keep the model accurate.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lint",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.no-monitor",
        title="Action has no monitor requirements",
        explanation="An action has no before/after monitor requirements; add monitors to verify its behavior.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lsp-coverage",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.unguarded-write",
        title="State variable written without a 'require' guard",
        explanation="A state variable is written by an action but is not guarded by any requirement in the requirement graph.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lsp-coverage",
        has_quick_fix=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.require.deadGuard",
        title="Dead guard: 'require false'",
        explanation="A 'require false' statement marks this path as unreachable; it is only reachable through variant specializations.",
        default_severity=lsp.DiagnosticSeverity.Information,
        source="ivy-lsp-coverage",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.monitor.orphanedHook",
        title="Monitor targets action with no definition in include closure",
        explanation="A before/after monitor references an action that has no declaration in the transitive include closure.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lsp-coverage",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.state.unusedStateVar",
        title="Unused state variable",
        explanation="A state variable has no reads or writes recorded in the requirement graph.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lsp-coverage",
    )
)

# RFC-tag and shadow-declaration emit sites in compute.py use the
# `lsp.Diagnostic(code=...)` keyword-arg form rather than a raw dict; they
# were invisible to the original dict-only completeness regex. Registered
# here with severity/source matching their emit sites.

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.tagDuplicate",
        title="Duplicate RFC tag in file",
        explanation="The same RFC bracket tag appears at two annotation sites in the same file; merge or differentiate them.",
        default_severity=lsp.DiagnosticSeverity.Warning,
        source="ivy-lsp-semantic",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.tagGap",
        title="Missing RFC tag in numeric sequence",
        explanation="A numeric RFC tag is missing from an otherwise dense sequence in the file; this may indicate an unintentional skip.",
        default_severity=lsp.DiagnosticSeverity.Information,
        source="ivy-lsp-semantic",
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.include.shadowDeclaration",
        title="Symbol shadows declaration in another file",
        explanation="A symbol with the same name is also declared in a different file in the same protocol; the local declaration shadows the other.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lsp-semantic",
        has_related_info=True,
    )
)

_reg(
    DiagnosticDescriptor(
        code="ivy.rfc.missingBracketTag",
        title="Assertion without RFC bracket tag",
        explanation="An assertion (require / ensure / assume / assert) is not annotated with an RFC bracket tag for traceability.",
        default_severity=lsp.DiagnosticSeverity.Hint,
        source="ivy-lsp-semantic",
    )
)
