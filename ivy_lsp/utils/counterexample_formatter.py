# ivy_lsp/utils/counterexample_formatter.py
"""Human-readable formatting of parsed Ivy counterexamples.

Transforms the structured dict from ``parse_counterexample()`` into
a readable state trace with step-by-step variable changes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Union


def format_counterexample(cex: Union[Mapping[str, Any], None]) -> str:
    """Format a parsed counterexample as a readable state trace.

    Args:
        cex: Output of ``parse_counterexample()``, or None.

    Returns:
        Human-readable string, or empty string if cex is None.
    """
    if cex is None:
        return ""

    lines: List[str] = []

    # Header: assertion info
    assertion = cex.get("assertion")
    assertion_line = cex.get("assertion_line")
    if assertion and assertion_line:
        lines.append(f"Violated assertion (Line {assertion_line}):")
        lines.append(f"  {assertion}")
    elif assertion:
        lines.append(f"Violated assertion: {assertion}")
    else:
        lines.append("No assertion identified in counterexample.")

    steps: List[Dict[str, Any]] = cex.get("steps", [])
    if not steps:
        lines.append("\nNo execution steps in counterexample.")
        return "\n".join(lines)

    lines.append(
        f"\nExecution trace ({len(steps)} step{'s' if len(steps) != 1 else ''}):"
    )
    lines.append("-" * 50)

    prev_assignments: Dict[str, str] = {}
    for step in steps:
        step_num = step.get("step_number", "?")
        action = step.get("action", "(unknown action)")
        assignments = step.get("assignments", {})

        lines.append(f"\n  Step {step_num}: {action}")

        if assignments:
            for var, val in sorted(assignments.items()):
                prev = prev_assignments.get(var)
                if prev is not None and prev != val:
                    lines.append(f"    {var} = {val}  (was: {prev})")
                else:
                    lines.append(f"    {var} = {val}")

        prev_assignments.update(assignments)

    return "\n".join(lines)
