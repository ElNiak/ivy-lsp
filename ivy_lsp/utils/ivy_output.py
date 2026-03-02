"""Shared parsers for ivy_check CLI output and file discovery."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

_IVY_CHECK_LINE = re.compile(r"(.*?):(\d+):\s*(error|warning):\s*(.*)")

# Unified exclusion set — superset of mcp_server.py and include_resolver.py
_DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "submodules", ".tox", ".mypy_cache", ".pytest_cache",
})


def parse_ivy_check_lines(output: str) -> List[Dict[str, Any]]:
    """Parse ivy_check output into structured dicts.

    Each dict has keys: file (str), line (int), severity (str), message (str).
    Non-matching lines are silently skipped.
    """
    results: List[Dict[str, Any]] = []
    for line in output.splitlines():
        m = _IVY_CHECK_LINE.match(line)
        if m:
            results.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "severity": m.group(3),
                "message": m.group(4),
            })
    return results


def find_ivy_files(
    root: str,
    exclude_dirs: frozenset[str] | None = None,
) -> list[str]:
    """Walk *root* and return relative paths to all .ivy files, sorted.

    Args:
        root: Directory to walk.
        exclude_dirs: Directory basenames to skip. Defaults to a broad set
            covering VCS, build, venv, and cache directories.
    """
    if exclude_dirs is None:
        exclude_dirs = _DEFAULT_EXCLUDE_DIRS
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fname in filenames:
            if fname.endswith(".ivy"):
                results.append(os.path.relpath(os.path.join(dirpath, fname), root))
    return sorted(results)
