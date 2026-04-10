"""Shared structural lint checks for Ivy source files.

Returns plain dicts so consumers (LSP diagnostics, MCP tools) can
convert to their own output types.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from ivy_lsp.core.parsing.tiered_extractor import INCLUDE_PATTERN


def check_structural_issues_raw(
    source: str,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Fast structural checks without full parsing.

    Returns list of dicts with keys: line (1-based int), severity (str),
    message (str), source (str), code (Optional[str]).
    """
    diags: List[Dict[str, Any]] = []
    lines = source.split("\n")

    # 1. Missing #lang header
    stripped = source.lstrip()
    if not stripped.startswith("#lang"):
        diags.append(
            {
                "line": 1,
                "severity": "warning",
                "message": "Missing '#lang ivy1.7' header",
                "source": "ivy-lint",
                "code": "missing-lang-header",
            }
        )

    # 2. Unmatched braces
    depth = 0
    for i, line_text in enumerate(lines):
        if line_text.strip().startswith("#lang"):
            code = line_text
        else:
            code = line_text.split("#")[0]
        for ch in code:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth < 0:
                diags.append(
                    {
                        "line": i + 1,
                        "severity": "error",
                        "message": "Unmatched closing brace",
                        "source": "ivy-lint",
                    }
                )
                depth = 0
    if depth > 0:
        diags.append(
            {
                "line": len(lines),
                "severity": "error",
                "message": f"Unmatched opening brace ({depth} unclosed)",
                "source": "ivy-lint",
            }
        )

    # 3. Parameter name style — flag multi-char lowercase param names
    _DECL_RE = re.compile(r"^(?:relation|function)\s+\w+\(([^)]+)\)", re.MULTILINE)
    for m in _DECL_RE.finditer(source):
        decl_line = source[: m.start()].count("\n")
        line_text = lines[decl_line].lstrip()
        if line_text.startswith("#"):
            continue
        params_str = m.group(1)
        for param in params_str.split(","):
            param = param.strip()
            if ":" not in param:
                continue
            name = param.split(":")[0].strip()
            if len(name) > 1 and name[0].islower():
                diags.append(
                    {
                        "line": decl_line + 1,
                        "severity": "warning",
                        "message": (
                            f"Parameter name '{name}' is a multi-character lowercase "
                            f"identifier — may collide with Ivy symbols. "
                            f"Prefer single uppercase letters (e.g., S, D, C)."
                        ),
                        "source": "ivy-lint",
                        "code": "param-name-style",
                    }
                )

    # 4. Missing after init — heuristic for uninitialized mutable state
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
        mutable_lines[name] = line_no + 1

    initialized: set[str] = set()
    in_init = False
    init_depth = 0
    for i, line_text in enumerate(lines):
        stripped = line_text.strip()
        if "after init" in stripped:
            in_init = True
            init_depth = 0
        if in_init:
            for ch in stripped:
                if ch == "{":
                    init_depth += 1
                elif ch == "}":
                    init_depth -= 1
                    if init_depth <= 0:
                        in_init = False
            assign_match = re.match(r"(\w+)(?:\(.*?\))?\s*:=", stripped)
            if assign_match:
                initialized.add(assign_match.group(1))

    for name in mutable_names - initialized:
        diags.append(
            {
                "line": mutable_lines[name],
                "severity": "warning",
                "message": (
                    f"'{name}' is never initialized in an 'after init' block "
                    f"— it will start with arbitrary values."
                ),
                "source": "ivy-lint",
                "code": "missing-init",
            }
        )

    # 5. Empty after init blocks
    _INIT_BLOCK_RE = re.compile(r"after\s+init\s*\{([^}]*)\}", re.MULTILINE | re.DOTALL)
    for m in _INIT_BLOCK_RE.finditer(source):
        body = m.group(1).strip()
        if not body:
            line_no = source[: m.start()].count("\n") + 1
            diags.append(
                {
                    "line": line_no,
                    "severity": "warning",
                    "message": "Empty 'after init' block — no state is initialized.",
                    "source": "ivy-lint",
                    "code": "empty-init",
                }
            )

    # 6. Duplicate top-level declarations (same file)
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
                {
                    "line": line_no + 1,
                    "severity": "error",
                    "message": (
                        f"Duplicate declaration of '{name}' "
                        f"(first declared at line {seen_decls[name]})."
                    ),
                    "source": "ivy-lint",
                    "code": "duplicate-decl",
                }
            )
        else:
            seen_decls[name] = line_no + 1

    # 7. Action without require (unguarded state modification)
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
                {
                    "line": action_line + 1,
                    "severity": "hint",
                    "message": (
                        "Action modifies state but has no 'require' precondition "
                        "— consider adding guards."
                    ),
                    "source": "ivy-lint",
                    "code": "unguarded-action",
                }
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
) -> List[Dict[str, Any]]:
    """Check for unresolved include directives.

    Args:
        source: The Ivy source text to scan for include directives.
        filepath: Absolute path to the source file being checked.
        resolve_callback: Optional callable(name, from_file) -> Optional[str].
            If None, uses simple os.path.isfile check in parent directory.
        basename_map: Optional mapping of basename -> list of paths, used
            to suggest near-miss corrections for unresolved includes.
    """
    diags: List[Dict[str, Any]] = []
    parent_dir = os.path.dirname(filepath)

    for match in INCLUDE_PATTERN.finditer(source):
        inc_name = match.group(1)
        if resolve_callback is not None:
            resolved = resolve_callback(inc_name, filepath)
        else:
            candidate = os.path.join(parent_dir, inc_name + ".ivy")
            resolved = candidate if os.path.isfile(candidate) else None

        if resolved is None:
            line_no = source[: match.start()].count("\n") + 1
            suggestion = (
                _find_near_miss(inc_name, basename_map) if basename_map else None
            )
            if suggestion:
                diags.append(
                    {
                        "line": line_no,
                        "severity": "warning",
                        "message": f"Cannot resolve include '{inc_name}'. Did you mean '{suggestion}'?",
                        "source": "ivy-lint",
                        "code": "ivy.include.nearMiss",
                    }
                )
            else:
                diags.append(
                    {
                        "line": line_no,
                        "severity": "error",
                        "message": f"Unresolved include: {inc_name}",
                        "source": "ivy-lint",
                        "code": "unresolved-include",
                    }
                )

    return diags


_TAG_COMMENT_RE = re.compile(r"#\s*tag\s*=\s*(\w+)")


def check_duplicate_tags(
    source: str,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Detect duplicate or placeholder variant tag comments.

    Args:
        source: The Ivy source text.
        filepath: Absolute path to the source file.
    """
    diags: List[Dict[str, Any]] = []
    tags: List[tuple] = []

    for i, line in enumerate(source.splitlines()):
        m = _TAG_COMMENT_RE.search(line)
        if m:
            tag_val = m.group(1)
            line_no = i + 1
            if not tag_val.isdigit():
                diags.append(
                    {
                        "line": line_no,
                        "severity": "info",
                        "message": f"Tag value '{tag_val}' is not numeric — placeholder?",
                        "source": "ivy-lint",
                        "code": "ivy.type.placeholderTag",
                    }
                )
            else:
                tags.append((tag_val, line_no))

    seen: Dict[str, int] = {}
    for tag_val, line_no in tags:
        if tag_val in seen:
            diags.append(
                {
                    "line": line_no,
                    "severity": "warning",
                    "message": (
                        f"Duplicate tag value {tag_val}"
                        f" — also used at line {seen[tag_val]}."
                    ),
                    "source": "ivy-lint",
                    "code": "ivy.type.duplicateTag",
                }
            )
        else:
            seen[tag_val] = line_no

    return diags


_DECL_PARAM_RE = re.compile(
    r"^\s*(relation|function)\s+([\w.]+)\s*\(([^)]+)\)", re.MULTILINE
)


def check_lowercase_params(
    source: str,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Check for lowercase-initial parameters in relation/function declarations.

    In Ivy, uppercase-initial names are logical variables (universally
    quantified). Lowercase-initial names are treated as constant references
    and will cause 'unknown symbol' errors at compile time.

    Only checks ``relation`` and ``function`` declarations. ``action``
    parameters are concrete and legitimately use lowercase names.
    """
    diags: List[Dict[str, Any]] = []

    for match in _DECL_PARAM_RE.finditer(source):
        kind = match.group(1)
        params_str = match.group(3)
        line_no = source[: match.start()].count("\n") + 1

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue
            name = param.split(":")[0].strip()
            if not name:
                continue
            if name[0].islower():
                diags.append(
                    {
                        "line": line_no,
                        "severity": "error",
                        "message": (
                            f"Parameter '{name}' in {kind} declaration must"
                            f" start with uppercase (Ivy treats lowercase"
                            f" as constant references)"
                        ),
                        "source": "ivy-lint",
                        "code": "ivy.declaration.lowercaseParam",
                    }
                )

    return diags


_REQUIREMENT_KEYWORDS = frozenset({"require", "ensure", "assume", "assert"})
_SUPPRESS_KEYWORDS = frozenset({"todo", "fixme", "disabled", "skip", "intentional"})


def check_commented_out_requires(
    source: str,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Detect commented-out require/ensure/assume/assert statements.

    Args:
        source: The Ivy source text.
        filepath: Absolute path to the source file.
    """
    diags: List[Dict[str, Any]] = []
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

        severity = "hint"
        for offset in (-1, -2, 1):
            adj_idx = i + offset
            if 0 <= adj_idx < len(lines):
                adj_lower = lines[adj_idx].lower()
                if any(kw in adj_lower for kw in _SUPPRESS_KEYWORDS):
                    severity = "info"
                    break

        diags.append(
            {
                "line": i + 1,
                "severity": severity,
                "message": (
                    "Commented-out require statement."
                    " Consider removing or re-enabling."
                ),
                "source": "ivy-lint",
                "code": "ivy.require.commentedOut",
            }
        )

    return diags
