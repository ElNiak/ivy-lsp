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

# Python traceback final exception line: FileNotFoundError: [Errno 2] No such file
# Matches the last line of a Python traceback (the actual exception).
_PYTHON_EXCEPTION_LINE = re.compile(
    r"^(\w+(?:\.\w+)*(?:Error|Exception|Warning|Exit))\s*:\s*(.*)"
)

# ivy_check structured output: isolate headers, section types, action contexts, check results
_ISOLATE_HEADER = re.compile(r"^Isolate\s+(.+?)\s*:$")
_ACTION_CONTEXT = re.compile(r"^in action\s+(.+?)\s+when called from\s+(.+?)\s*:$")
_CHECK_RESULT = re.compile(
    r"^([\w./-]+\.ivy):\s*line\s+(\d+):\s*(.+?)\s+\.\.\.\s*(PASS|FAIL)$"
)
_SECTION_TYPES: Dict[str, str] = {
    "The following properties are to be checked:": "property",
    "The following program assertions are treated as guarantees:": "guarantee",
}

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


def parse_ivy_check_lines(output: str) -> List[Dict[str, Any]]:
    """Parse ivy_check output into structured dicts.

    Each dict has keys: file (str), line (int), severity (str), message (str).
    Non-matching lines are silently skipped.

    Delegates to :func:`parse_ivy_output` so that all error formats are
    handled consistently (standard, verbose with absolute paths, IvyError
    tracebacks, and C++ compiler errors).  The ``source`` key added by
    ``parse_ivy_output`` is stripped to preserve the original return schema.
    """
    return [
        {k: v for k, v in entry.items() if k != "source"}
        for entry in parse_ivy_output(output)
    ]


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

        # Try Python exception line (e.g. FileNotFoundError: ...)
        if diag is None:
            m = _PYTHON_EXCEPTION_LINE.match(raw_line.strip())
            if m:
                diag = {
                    "file": "",
                    "line": 0,
                    "severity": "error",
                    "message": f"{m.group(1)}: {m.group(2)}",
                    "source": "python_exception",
                }

        if diag is not None:
            key = (diag["file"], diag["line"], diag["message"])
            if key not in seen:
                seen.add(key)
                results.append(diag)

    return results


def parse_check_results(output: str) -> Dict[str, Any]:
    """Parse ivy_check PASS/FAIL verification results into structured data.

    Scans output line by line, tracking the current isolate, section type
    (property/guarantee), and action context. Emits a check entry for each
    line ending in ``... PASS`` or ``... FAIL``. Skips ``[assumed]`` lines.

    Args:
        output: Raw ivy_check stdout.

    Returns:
        Dict with keys: total, passed, failed, isolate_summary, failed_checks.
    """
    current_isolate: str | None = None
    current_type: str | None = None
    current_action: str | None = None

    isolate_data: Dict[str, Dict[str, Any]] = {}
    fail_groups: Dict[tuple[str, str | None], List[Dict[str, Any]]] = {}

    total = 0
    passed = 0
    failed = 0

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Isolate header resets section and action context
        m = _ISOLATE_HEADER.match(line)
        if m:
            current_isolate = m.group(1)
            current_type = None
            current_action = None
            if current_isolate not in isolate_data:
                isolate_data[current_isolate] = {
                    "passed": 0,
                    "failed": 0,
                    "by_type": {},
                }
            continue

        # Tracked section header (properties / guarantees)
        matched_section = False
        for header, check_type in _SECTION_TYPES.items():
            if line == header:
                current_type = check_type
                current_action = None
                matched_section = True
                break
        if matched_section:
            continue

        # Non-tracked section header (implementations, monitors, etc.)
        if line.startswith("The following"):
            current_type = None
            current_action = None
            continue

        # Action context within a section
        m = _ACTION_CONTEXT.match(line)
        if m:
            current_action = f"{m.group(1)} when called from {m.group(2)}"
            continue

        # PASS/FAIL check result (only inside a tracked section)
        if current_isolate is not None and current_type is not None:
            m = _CHECK_RESULT.match(line)
            if m:
                result_str = m.group(4)
                total += 1

                iso = isolate_data[current_isolate]
                if current_type not in iso["by_type"]:
                    iso["by_type"][current_type] = {"passed": 0, "failed": 0}

                if result_str == "PASS":
                    passed += 1
                    iso["passed"] += 1
                    iso["by_type"][current_type]["passed"] += 1
                else:
                    failed += 1
                    iso["failed"] += 1
                    iso["by_type"][current_type]["failed"] += 1

                    key = (current_isolate, current_action)
                    if key not in fail_groups:
                        fail_groups[key] = []
                    fail_groups[key].append(
                        {
                            "file": m.group(1),
                            "line": int(m.group(2)),
                            "type": current_type,
                            "result": "FAIL",
                        }
                    )

    isolate_summary = [{"name": name, **data} for name, data in isolate_data.items()]
    failed_checks = [
        {
            "isolate": iso,
            "action_context": action,
            "checks": checks,
        }
        for (iso, action), checks in fail_groups.items()
    ]

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "isolate_summary": isolate_summary,
        "failed_checks": failed_checks,
    }


def extract_error_summary(
    raw_output: str,
    diagnostics: List[Dict[str, Any]] | None = None,
    check_results: Dict[str, Any] | None = None,
) -> str:
    """Extract a human-readable one-liner error summary.

    Priority:
    1. Check result failures (structured verification outcome)
    2. First error diagnostic formatted as ``file:line: message``
    3. First non-error diagnostic (e.g. warning) if no errors exist
    4. All checks passed (positive confirmation when checks ran)
    5. Last non-empty line of raw output (fallback when no diagnostics)
    6. Empty string if no output at all
    """
    # Priority 1: check failures
    if check_results and check_results.get("failed", 0) > 0:
        total = check_results["total"]
        failed_count = check_results["failed"]

        failed_isolates = [
            iso for iso in check_results["isolate_summary"] if iso["failed"] > 0
        ]

        # Collect failure counts per check type
        type_counts: Dict[str, int] = {}
        for iso in failed_isolates:
            for ctype, counts in iso["by_type"].items():
                if counts["failed"] > 0:
                    type_counts[ctype] = type_counts.get(ctype, 0) + counts["failed"]

        # Format type description
        if len(type_counts) == 1:
            ctype = next(iter(type_counts))
            plural = "properties" if ctype == "property" else f"{ctype}s"
            type_desc = f"all {plural}"
        else:
            parts = [
                f"{count} {'properties' if ctype == 'property' else ctype + 's'}"
                for ctype, count in sorted(type_counts.items())
            ]
            type_desc = ", ".join(parts)

        # Format isolate description
        if len(failed_isolates) == 1:
            iso_desc = f"in Isolate {failed_isolates[0]['name']}"
        else:
            iso_parts = [
                f"{iso['name']}: {iso['failed']}"
                for iso in sorted(failed_isolates, key=lambda x: -x["failed"])
            ]
            iso_desc = (
                f"across {len(failed_isolates)} isolates " f"({', '.join(iso_parts)})"
            )

        return f"{failed_count}/{total} checks failed ({type_desc}) {iso_desc}"

    # Priority 2-3: error/warning diagnostics
    if diagnostics:
        errors = [d for d in diagnostics if d.get("severity") == "error"]
        if errors:
            d = errors[0]
            return f"{d['file']}:{d['line']}: {d['message']}"
        d = diagnostics[0]
        return f"{d['file']}:{d['line']}: {d['message']}"

    # Priority 4: all checks passed
    if (
        check_results
        and check_results.get("total", 0) > 0
        and check_results.get("failed", 0) == 0
    ):
        total = check_results["total"]
        n_isolates = len(check_results["isolate_summary"])
        return f"{total}/{total} checks passed across {n_isolates} isolates"

    # Try to extract Python exception from traceback
    if "Traceback (most recent call last):" in raw_output:
        for line in reversed(raw_output.splitlines()):
            stripped = line.strip()
            m = _PYTHON_EXCEPTION_LINE.match(stripped)
            if m:
                return f"{m.group(1)}: {m.group(2)}"

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

    Handles four cases:

    1. **Error objects** with ``.msg`` attribute (IvyError instances):
       returns the message directly.
    2. **PLY parser tuples** shaped as ``(line, token, message)`` where
       line is an int — typically LALR syntax errors.
    3. **Raw tuples** from Ivy's parser error_list, shaped as
       ``(symbol_name, location1, location2, ...)``, where each
       location is either ``None`` or a nested ``(file, line, ...)``
       tuple representing an include chain.
    4. **Anything else** (strings, generic exceptions): ``str(error)``.
    """
    # Case 1: Ivy error objects with structured attributes
    if hasattr(error, "msg"):
        return str(error.msg)

    # Case 2: PLY parser error tuples or ParseError objects with .args
    args = error if isinstance(error, tuple) else getattr(error, "args", None)
    if isinstance(args, tuple) and len(args) >= 2 and isinstance(args[0], int):
        line = args[0]
        token = args[1] if len(args) > 1 else "?"
        msg = args[2] if len(args) > 2 else "parse error"
        return f"line {line}: {msg} (unexpected token '{token}')"

    # Case 3: Raw parser tuples — (symbol_name, loc1, loc2, ...)
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

    # Case 4: fallback
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
        exclude_dirs = DEFAULT_EXCLUDE_DIRS
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fname in filenames:
            if fname.endswith(".ivy"):
                results.append(os.path.relpath(os.path.join(dirpath, fname), root))
    return sorted(results)
