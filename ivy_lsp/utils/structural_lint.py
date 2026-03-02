"""Shared structural lint checks for Ivy source files.

Returns plain dicts so consumers (LSP diagnostics, MCP tools) can
convert to their own output types.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List


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
        diags.append({
            "line": 1,
            "severity": "warning",
            "message": "Missing '#lang ivy1.7' header",
            "source": "ivy-lint",
            "code": "missing-lang-header",
        })

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
                diags.append({
                    "line": i + 1,
                    "severity": "error",
                    "message": "Unmatched closing brace",
                    "source": "ivy-lint",
                })
                depth = 0
    if depth > 0:
        diags.append({
            "line": len(lines),
            "severity": "error",
            "message": f"Unmatched opening brace ({depth} unclosed)",
            "source": "ivy-lint",
        })

    return diags


def check_unresolved_includes_raw(
    source: str,
    filepath: str,
    resolve_callback: Any = None,
) -> List[Dict[str, Any]]:
    """Check for unresolved include directives.

    Args:
        resolve_callback: Optional callable(name, from_file) -> Optional[str].
            If None, uses simple os.path.isfile check in parent directory.
    """
    diags: List[Dict[str, Any]] = []
    parent_dir = os.path.dirname(filepath)

    for match in re.finditer(r"^include\s+(\w+)", source, re.MULTILINE):
        inc_name = match.group(1)
        if resolve_callback is not None:
            resolved = resolve_callback(inc_name, filepath)
        else:
            candidate = os.path.join(parent_dir, inc_name + ".ivy")
            resolved = candidate if os.path.isfile(candidate) else None

        if resolved is None:
            line_no = source[: match.start()].count("\n") + 1
            diags.append({
                "line": line_no,
                "severity": "warning",
                "message": f"Unresolved include: {inc_name}",
                "source": "ivy-lint",
                "code": "unresolved-include",
            })

    return diags
