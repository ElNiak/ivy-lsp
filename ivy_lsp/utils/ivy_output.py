"""Shared parsers for Ivy tool output, error formatting, and file discovery.

Handles output from ivy_check, ivyc, and ivy_show, including IvyError
tracebacks and C++ compiler errors.  Also provides error formatting for
Ivy parser error objects and tuples, plus workspace file discovery.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

# Standard ivy_check format: file.ivy:42: error: message
_IVY_CHECK_LINE = re.compile(r"(.*?):(\d+):\s*(error|warning):\s*(.*)")

# IvyError traceback format: ivy.ivy_utils.IvyError: file.ivy: line 42: error: msg
# Also handles the variant without explicit severity keyword.
_IVY_ERROR_LINE = re.compile(
    r"ivy\.ivy_utils\.IvyError:\s*(.*?):\s*line\s+(\d+):\s*(?:(error|warning):\s*)?(.*)"
)

# Verbose ivy_check format: file.ivy: line 42: error: message
# Produced by ivy_check when run with absolute paths in MCP staging mode.
_IVY_CHECK_LINE_VERBOSE = re.compile(
    r"(.*?):\s*line\s+(\d+):\s*(error|warning):\s*(.*)"
)

# C++ compiler format: file.cpp:42:10: error: undeclared identifier
# Requires the column number (line:col:) to distinguish from ivy_check format.
# Handles gcc/clang output and "fatal error" severity.
_CPP_ERROR_LINE = re.compile(
    r"(.*?\.\w+):(\d+):\d+:\s*(error|warning|fatal error):\s*(.*)"
)

# Unified exclusion set — superset of mcp_server.py and include_resolver.py
DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "submodules",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)
_DEFAULT_EXCLUDE_DIRS = DEFAULT_EXCLUDE_DIRS  # back-compat alias


def parse_ivy_check_lines(output: str) -> List[Dict[str, Any]]:
    """Parse ivy_check output into structured dicts.

    Each dict has keys: file (str), line (int), severity (str), message (str).
    Non-matching lines are silently skipped.
    """
    results: List[Dict[str, Any]] = []
    for line in output.splitlines():
        m = _IVY_CHECK_LINE.match(line)
        if m:
            results.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "severity": m.group(3),
                    "message": m.group(4),
                }
            )
    return results


def parse_ivy_output(output: str) -> List[Dict[str, Any]]:
    """Parse any Ivy tool output into structured diagnostics.

    Handles three formats:
    1. Standard ivy_check: ``file.ivy:42: error: message``
    2. IvyError traceback: ``ivy.ivy_utils.IvyError: file.ivy: line 42: error: message``
    3. C++ compiler: ``file.cpp:42:10: error: message``

    Each result dict has keys: file, line, severity, message, source.
    The ``source`` field indicates the error origin (``"ivy_check"``,
    ``"ivy_error"``, or ``"cpp_compiler"``).

    Deduplicates entries that share the same file, line, and message.
    """
    results: List[Dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()

    for raw_line in output.splitlines():
        diag: Dict[str, Any] | None = None

        # Try IvyError traceback format first (most specific prefix)
        m = _IVY_ERROR_LINE.search(raw_line)
        if m:
            severity = m.group(3) or "error"
            diag = {
                "file": m.group(1).strip(),
                "line": int(m.group(2)),
                "severity": severity,
                "message": m.group(4).strip(),
                "source": "ivy_error",
            }
        else:
            # Try C++ compiler format (requires line:col:, more specific)
            m = _CPP_ERROR_LINE.match(raw_line)
            if m:
                severity = "error" if m.group(3) == "fatal error" else m.group(3)
                diag = {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "severity": severity,
                    "message": m.group(4),
                    "source": "cpp_compiler",
                }
            else:
                # Try verbose ivy_check format: "file: line N: error: msg"
                m = _IVY_CHECK_LINE_VERBOSE.match(raw_line)
                if m:
                    diag = {
                        "file": m.group(1).strip(),
                        "line": int(m.group(2)),
                        "severity": m.group(3),
                        "message": m.group(4).strip(),
                        "source": "ivy_check",
                    }
                else:
                    # Fall back to standard ivy_check format (file:line: severity:)
                    m = _IVY_CHECK_LINE.match(raw_line)
                    if m:
                        diag = {
                            "file": m.group(1),
                            "line": int(m.group(2)),
                            "severity": m.group(3),
                            "message": m.group(4),
                            "source": "ivy_check",
                        }

        if diag is not None:
            key = (diag["file"], diag["line"], diag["message"])
            if key not in seen:
                seen.add(key)
                results.append(diag)

    return results


def extract_error_summary(
    raw_output: str,
    diagnostics: List[Dict[str, Any]] | None = None,
) -> str:
    """Extract a human-readable one-liner error summary.

    Priority:
    1. First error diagnostic formatted as ``file:line: message``
    2. First non-error diagnostic (e.g. warning) if no errors exist
    3. Last non-empty line of raw output (fallback when no diagnostics)
    4. Empty string if no output at all
    """
    if diagnostics:
        errors = [d for d in diagnostics if d.get("severity") == "error"]
        if errors:
            d = errors[0]
            return f"{d['file']}:{d['line']}: {d['message']}"
        # No errors but have warnings — use first warning
        d = diagnostics[0]
        return f"{d['file']}:{d['line']}: {d['message']}"

    # Fallback: last non-empty line
    for line in reversed(raw_output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _format_location_chain(loc: Any) -> str:
    """Flatten a nested location tuple into a readable chain.

    Ivy's parser stores include-chain locations as nested tuples:
    ``(file, line, (included_file, included_line, (...)))``

    Returns a string like ``"quic_shim.ivy:47 -> quic_protection.ivy:13"``.
    """
    parts: List[str] = []
    current = loc
    while isinstance(current, tuple) and len(current) >= 2:
        fname = current[0]
        lineno = current[1]
        if isinstance(fname, str) and isinstance(lineno, int):
            parts.append(f"{os.path.basename(fname)}:{lineno}")
        current = current[2] if len(current) > 2 else None
    return " -> ".join(parts) if parts else str(loc)


def format_ivy_error(error: Any) -> str:
    """Format a single Ivy parse error for human consumption.

    Handles three cases:

    1. **Error objects** with ``.msg`` attribute (IvyError instances):
       returns the message directly.
    2. **Raw tuples** from Ivy's parser error_list, shaped as
       ``(symbol_name, location1, location2, ...)``, where each
       location is either ``None`` or a nested ``(file, line, ...)``
       tuple representing an include chain.
    3. **Anything else** (strings, generic exceptions): ``str(error)``.
    """
    # Case 1: Ivy error objects with structured attributes
    if hasattr(error, "msg"):
        return str(error.msg)

    # Case 2: Raw parser tuples — (symbol_name, loc1, loc2, ...)
    if isinstance(error, tuple) and len(error) >= 1 and isinstance(error[0], str):
        symbol = error[0]
        locations = error[1:]
        non_none = [loc for loc in locations if loc is not None]

        if not non_none:
            return f"Unresolved '{symbol}'"

        formatted = [_format_location_chain(loc) for loc in non_none]
        if len(non_none) >= 2:
            return f"Duplicate '{symbol}': {', '.join(formatted)}"
        return f"Conflict '{symbol}': {formatted[0]}"

    # Case 3: fallback
    return str(error)


def format_ivy_errors(errors: List[Any]) -> str:
    """Format a list of parse errors into a compact summary.

    For small lists (<=10), returns each error formatted individually.
    For large lists, groups by category (duplicates vs unresolved) with
    counts and sample symbol names.
    """
    if not errors:
        return "(none)"

    if len(errors) <= 10:
        return "; ".join(format_ivy_error(e) for e in errors)

    # Group large error lists by category
    duplicates: List[str] = []
    unresolved: List[str] = []
    other: List[str] = []

    for err in errors:
        if isinstance(err, tuple) and len(err) >= 1 and isinstance(err[0], str):
            locs = err[1:]
            non_none = [loc for loc in locs if loc is not None]
            if not non_none:
                unresolved.append(err[0])
            elif len(non_none) >= 2:
                duplicates.append(err[0])
            else:
                other.append(err[0])
        else:
            other.append(format_ivy_error(err))

    parts: List[str] = []
    _MAX_SAMPLES = 5

    if duplicates:
        samples = ", ".join(duplicates[:_MAX_SAMPLES])
        suffix = ", ..." if len(duplicates) > _MAX_SAMPLES else ""
        parts.append(f"{len(duplicates)} duplicate symbols ({samples}{suffix})")
    if unresolved:
        samples = ", ".join(unresolved[:_MAX_SAMPLES])
        suffix = ", ..." if len(unresolved) > _MAX_SAMPLES else ""
        parts.append(f"{len(unresolved)} unresolved ({samples}{suffix})")
    if other:
        samples = ", ".join(other[:_MAX_SAMPLES])
        suffix = ", ..." if len(other) > _MAX_SAMPLES else ""
        parts.append(f"{len(other)} other ({samples}{suffix})")

    return f"{len(errors)} errors — " + "; ".join(parts)


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
