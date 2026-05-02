"""Shared structural lint checks for Ivy source files.

All public helpers return ``List[IvyDiagnostic]``; boundary consumers
convert via ``d.to_lsp()`` (LSP) or ``d.to_mcp_dict()`` (MCP).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic
from ivy_lsp.core.parsing.tiered_extractor import INCLUDE_PATTERN


def check_structural_issues(
    source: str,
    _filepath: str,
) -> List[IvyDiagnostic]:
    """Fast structural checks without full parsing.

    Returns a list of validated ``IvyDiagnostic`` objects with canonical
    namespaced codes.  Callers that need LSP ``Diagnostic`` objects call
    ``[d.to_lsp() for d in diags]``; MCP callers use ``d.to_mcp_dict()``.

    Args:
        source: Ivy source text to check.
        filepath: Absolute path to the source file (used for context only).

    Returns:
        List of ``IvyDiagnostic`` instances, one per structural issue found.
    """
    diags: List[IvyDiagnostic] = []
    lines = source.split("\n")

    # 1. Missing #lang header
    stripped = source.lstrip()
    if not stripped.startswith("#lang"):
        diags.append(
            IvyDiagnostic(
                code="ivy.syntax.missingLangHeader",
                message="Missing '#lang ivy1.7' header",
                line=0,
                severity=lsp.DiagnosticSeverity.Warning,
                source="ivy-lint",
            )
        )

    # 2. Unmatched braces
    depth = 0
    for i, line_text in enumerate(lines):
        if line_text.strip().startswith("#lang"):
            code_text = line_text
        else:
            code_text = line_text.split("#")[0]
        for ch in code_text:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth < 0:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.syntax.unmatchedBrace",
                        message="Unmatched closing brace",
                        line=i,
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lint",
                    )
                )
                depth = 0
    if depth > 0:
        diags.append(
            IvyDiagnostic(
                code="ivy.syntax.unmatchedBrace",
                message=f"Unmatched opening brace ({depth} unclosed)",
                line=max(0, len(lines) - 1),
                severity=lsp.DiagnosticSeverity.Error,
                source="ivy-lint",
            )
        )

    # 3. Missing after init — heuristic for uninitialized mutable state
    _MUTABLE_RE = re.compile(r"^(?:relation|function)\s+(\w+)", re.MULTILINE)
    mutable_names: set[str] = set()
    mutable_lines: dict[str, int] = {}
    for m in _MUTABLE_RE.finditer(source):
        line_no = source[: m.start()].count("\n")
        line_text = lines[line_no].lstrip()
        if line_text.startswith("#"):
            continue
        name = m.group(1)
        mutable_names.add(name)
        mutable_lines[name] = line_no

    initialized: set[str] = set()
    in_init = False
    init_depth = 0
    for i, line_text in enumerate(lines):
        stripped_line = line_text.strip()
        if "after init" in stripped_line:
            in_init = True
            init_depth = 0
        if in_init:
            for ch in stripped_line:
                if ch == "{":
                    init_depth += 1
                elif ch == "}":
                    init_depth -= 1
                    if init_depth <= 0:
                        in_init = False
            assign_match = re.match(r"(\w+)(?:\(.*?\))?\s*:=", stripped_line)
            if assign_match:
                initialized.add(assign_match.group(1))

    for name in mutable_names - initialized:
        diags.append(
            IvyDiagnostic(
                code="ivy.state.missingInit",
                message=(
                    f"'{name}' is never initialized in an 'after init' block "
                    f"— it will start with arbitrary values."
                ),
                line=mutable_lines[name],
                severity=lsp.DiagnosticSeverity.Warning,
                source="ivy-lint",
            )
        )

    # 4. Empty after init blocks
    _INIT_BLOCK_RE = re.compile(r"after\s+init\s*\{([^}]*)\}", re.MULTILINE | re.DOTALL)
    for m in _INIT_BLOCK_RE.finditer(source):
        body = m.group(1).strip()
        if not body:
            line_no = source[: m.start()].count("\n")
            diags.append(
                IvyDiagnostic(
                    code="ivy.state.emptyInit",
                    message="Empty 'after init' block — no state is initialized.",
                    line=line_no,
                    severity=lsp.DiagnosticSeverity.Warning,
                    source="ivy-lint",
                )
            )

    # 5. Duplicate top-level declarations (same file)
    _TOP_DECL_RE = re.compile(
        r"^(relation|function|type|individual)\s+(\w+)", re.MULTILINE
    )
    seen_decls: dict[str, int] = {}
    for m in _TOP_DECL_RE.finditer(source):
        line_no = source[: m.start()].count("\n")
        line_text = lines[line_no].lstrip()
        if line_text.startswith("#"):
            continue
        name = m.group(2)
        if name in seen_decls:
            diags.append(
                IvyDiagnostic(
                    code="ivy.naming.duplicateDecl",
                    message=(
                        f"Duplicate declaration of '{name}' "
                        f"(first declared at line {seen_decls[name] + 1})."
                    ),
                    line=line_no,
                    severity=lsp.DiagnosticSeverity.Error,
                    source="ivy-lint",
                )
            )
        else:
            seen_decls[name] = line_no

    # 6. Action without require (unguarded state modification)
    _ACTION_RE = re.compile(r"^(\s*)action\s+\w+[^=]*=\s*\{", re.MULTILINE)
    for m in _ACTION_RE.finditer(source):
        action_start = m.end()
        action_line = source[: m.start()].count("\n")
        line_text = lines[action_line].lstrip()
        if line_text.startswith("#"):
            continue
        depth = 1
        pos = action_start
        while pos < len(source) and depth > 0:
            if source[pos] == "{":
                depth += 1
            elif source[pos] == "}":
                depth -= 1
            pos += 1
        action_body = source[action_start : pos - 1] if pos > action_start else ""
        has_require = "require " in action_body or "require(" in action_body
        has_assignment = ":=" in action_body
        if has_assignment and not has_require:
            diags.append(
                IvyDiagnostic(
                    code="ivy.action.unguardedWrite",
                    message=(
                        "Action modifies state but has no 'require' precondition "
                        "— consider adding guards."
                    ),
                    line=action_line,
                    severity=lsp.DiagnosticSeverity.Hint,
                    source="ivy-lint",
                )
            )

    return diags


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _find_near_miss(name: str, basename_map: Dict[str, List[str]]) -> Optional[str]:
    """Find a close match for an unresolved include name."""
    name_segments = set(name.split("_"))
    for candidate in basename_map:
        if candidate == name:
            continue
        if set(candidate.split("_")) == name_segments:
            return candidate
    for candidate in basename_map:
        if candidate == name:
            continue
        if _levenshtein(name, candidate) <= 2:
            return candidate
    return None


def check_unresolved_includes_raw(
    source: str,
    filepath: str,
    resolve_callback: Any = None,
    basename_map: Optional[Dict[str, List[str]]] = None,
) -> List[IvyDiagnostic]:
    """Check for unresolved include directives.

    Args:
        source: The Ivy source text to scan for include directives.
        filepath: Absolute path to the source file being checked.
        resolve_callback: Optional callable(name, from_file) -> Optional[str].
            If None, uses simple os.path.isfile check in parent directory.
        basename_map: Optional mapping of basename -> list of paths, used
            to suggest near-miss corrections for unresolved includes.

    Returns:
        List of ``IvyDiagnostic`` instances, one per unresolved include.
    """
    diags: List[IvyDiagnostic] = []
    parent_dir = os.path.dirname(filepath)

    for match in INCLUDE_PATTERN.finditer(source):
        inc_name = match.group(1)
        if resolve_callback is not None:
            resolved = resolve_callback(inc_name, filepath)
        else:
            candidate = os.path.join(parent_dir, inc_name + ".ivy")
            resolved = candidate if os.path.isfile(candidate) else None

        if resolved is None:
            line_no = source[: match.start()].count("\n")  # 0-based
            suggestion = (
                _find_near_miss(inc_name, basename_map) if basename_map else None
            )
            if suggestion:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.include.nearMiss",
                        message=f"Cannot resolve include '{inc_name}'. Did you mean '{suggestion}'?",
                        line=line_no,
                        severity=lsp.DiagnosticSeverity.Warning,
                        source="ivy-lint",
                    )
                )
            else:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.module.unresolvedInclude",
                        message=f"Unresolved include: {inc_name}",
                        line=line_no,
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lint",
                    )
                )

    return diags


_TAG_COMMENT_RE = re.compile(r"#\s*tag\s*=\s*(\w+)")


def check_duplicate_tags(
    source: str,
    _filepath: str,
) -> List[IvyDiagnostic]:
    """Detect duplicate or placeholder variant tag comments.

    Args:
        source: The Ivy source text.
        filepath: Absolute path to the source file.

    Returns:
        List of ``IvyDiagnostic`` instances for tag issues found.
    """
    diags: List[IvyDiagnostic] = []
    tags: List[Tuple[str, int]] = []

    for i, line in enumerate(source.splitlines()):
        m = _TAG_COMMENT_RE.search(line)
        if m:
            tag_val = m.group(1)
            line_no = i  # 0-based
            if not tag_val.isdigit():
                diags.append(
                    IvyDiagnostic(
                        code="ivy.type.placeholderTag",
                        message=f"Tag value '{tag_val}' is not numeric — placeholder?",
                        line=line_no,
                        severity=lsp.DiagnosticSeverity.Information,
                        source="ivy-lint",
                    )
                )
            else:
                tags.append((tag_val, line_no))

    seen: Dict[str, int] = {}
    for tag_val, line_no in tags:
        if tag_val in seen:
            diags.append(
                IvyDiagnostic(
                    code="ivy.type.duplicateTag",
                    message=(
                        f"Duplicate tag value {tag_val}"
                        f" — also used at line {seen[tag_val] + 1}."
                    ),
                    line=line_no,
                    severity=lsp.DiagnosticSeverity.Warning,
                    source="ivy-lint",
                )
            )
        else:
            seen[tag_val] = line_no

    return diags


_DECL_PARAM_RE = re.compile(
    r"^\s*(relation|function)\s+([\w.]+)\s*\(([^)]+)\)", re.MULTILINE
)


def check_lowercase_params(
    source: str,
    _filepath: str,
) -> List[IvyDiagnostic]:
    """Check for lowercase-initial parameters in relation/function declarations.

    In Ivy, uppercase-initial names are logical variables (universally
    quantified). Lowercase-initial names are treated as constant references
    and will cause 'unknown symbol' errors at compile time.

    Only checks ``relation`` and ``function`` declarations. ``action``
    parameters are concrete and legitimately use lowercase names.

    Returns:
        List of ``IvyDiagnostic`` instances for lowercase parameter issues.
    """
    diags: List[IvyDiagnostic] = []
    lines = source.split("\n")

    for match in _DECL_PARAM_RE.finditer(source):
        kind = match.group(1)
        params_str = match.group(3)
        line_no = source[: match.start()].count("\n")  # 0-based
        line_text = lines[line_no].lstrip() if line_no < len(lines) else ""
        if line_text.startswith("#"):
            continue

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue
            name = param.split(":")[0].strip()
            if not name:
                continue
            if name[0].islower():
                diags.append(
                    IvyDiagnostic(
                        code="ivy.declaration.lowercaseParam",
                        message=(
                            f"Parameter '{name}' in {kind} declaration must"
                            f" start with uppercase (Ivy treats lowercase"
                            f" as constant references)"
                        ),
                        line=line_no,
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lint",
                    )
                )

    return diags


_REQUIREMENT_KEYWORDS = frozenset({"require", "ensure", "assume", "assert"})
_SUPPRESS_KEYWORDS = frozenset({"todo", "fixme", "disabled", "skip", "intentional"})


def check_commented_out_requires(
    source: str,
    _filepath: str,
) -> List[IvyDiagnostic]:
    """Detect commented-out require/ensure/assume/assert statements.

    Args:
        source: The Ivy source text.
        filepath: Absolute path to the source file.

    Returns:
        List of ``IvyDiagnostic`` instances for commented-out requirements.
    """
    diags: List[IvyDiagnostic] = []
    lines = source.splitlines()

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        content = stripped.lstrip("#").strip()
        words = content.split()
        first_word = words[0].lower() if words else ""
        if first_word not in _REQUIREMENT_KEYWORDS:
            continue

        severity = lsp.DiagnosticSeverity.Hint
        for offset in (-1, -2, 1):
            adj_idx = i + offset
            if 0 <= adj_idx < len(lines):
                adj_lower = lines[adj_idx].lower()
                if any(kw in adj_lower for kw in _SUPPRESS_KEYWORDS):
                    severity = lsp.DiagnosticSeverity.Information
                    break

        diags.append(
            IvyDiagnostic(
                code="ivy.require.commentedOut",
                message=(
                    "Commented-out require statement."
                    " Consider removing or re-enabling."
                ),
                line=i,  # 0-based
                severity=severity,
                source="ivy-lint",
            )
        )

    return diags
