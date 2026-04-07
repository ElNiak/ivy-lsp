"""Parse counterexample traces from ivy_check output into structured JSON."""

from __future__ import annotations

import re
from typing import Any, TypedDict


class CounterexampleStep(TypedDict):
    """A single step in a counterexample trace."""

    step_number: int
    action: str | None
    assignments: dict[str, str]


class Counterexample(TypedDict):
    """Structured counterexample data from ivy_check output."""

    assertion: str | None
    assertion_line: int | None
    steps: list[CounterexampleStep]


# Matches "The following assertion at line <N> is not always true:"
_ASSERTION_HEADER_RE = re.compile(
    r"The following assertion at line (\d+) is not always true:"
)

# Matches assertion body like "  assert conn_state = connected"
# or "  require pkt_count > 0"
_ASSERTION_BODY_RE = re.compile(
    r"^\s+(assert|require|ensure|assume)\s+(.+)$", re.MULTILINE
)

# Matches "  Step <N>:"
_STEP_RE = re.compile(r"^\s*Step\s+(\d+)\s*:", re.MULTILINE)

# Matches "    Action: <name>"
_ACTION_RE = re.compile(r"^\s*Action:\s*(.+)$", re.MULTILINE)

# Matches "    var_name = value"
_ASSIGNMENT_RE = re.compile(r"^\s{2,}(\w[\w.]*)\s*=\s*(.+)$")


def parse_counterexample(raw_output: str) -> Counterexample | None:
    """Parse counterexample trace from ivy_check output.

    Returns structured counterexample data or None if no counterexample found.

    The returned dict has:
    - assertion: the failing assertion text (or None if not found)
    - assertion_line: the line number of the failing assertion (or None)
    - steps: list of dicts with step_number, action (str or None), and
      assignments dict mapping variable names to their string values
    """
    # Locate the "Counterexample:" block
    cex_idx = raw_output.find("Counterexample:")
    if cex_idx == -1:
        return None

    # Extract assertion info from text before the counterexample block
    preamble = raw_output[:cex_idx]
    assertion_line: int | None = None
    assertion_text: str | None = None

    header_m = _ASSERTION_HEADER_RE.search(preamble)
    if header_m:
        assertion_line = int(header_m.group(1))
        # The assertion body is typically the line right after the header
        after_header = preamble[header_m.end() :]
        body_m = _ASSERTION_BODY_RE.search(after_header)
        if body_m:
            assertion_text = body_m.group(0).strip()

    # Parse the counterexample block
    cex_block = raw_output[cex_idx:]
    steps: list[dict[str, Any]] = []

    # Find all step positions
    step_matches = list(_STEP_RE.finditer(cex_block))
    if not step_matches:
        # Counterexample header exists but no steps parsed -- return minimal
        return {
            "assertion": assertion_text,
            "assertion_line": assertion_line,
            "steps": [],
        }

    for i, step_m in enumerate(step_matches):
        step_number = int(step_m.group(1))

        # Determine the text block for this step (up to next step or end)
        start = step_m.end()
        end = (
            step_matches[i + 1].start() if i + 1 < len(step_matches) else len(cex_block)
        )
        step_text = cex_block[start:end]

        # Extract optional action
        action: str | None = None
        action_m = _ACTION_RE.search(step_text)
        if action_m:
            action = action_m.group(1).strip()

        # Extract variable assignments
        assignments: dict[str, str] = {}
        for line in step_text.splitlines():
            # Skip the Action line (already parsed)
            if action_m and "Action:" in line:
                continue
            assign_m = _ASSIGNMENT_RE.match(line)
            if assign_m:
                var_name = assign_m.group(1)
                var_value = assign_m.group(2).strip()
                assignments[var_name] = var_value

        steps.append(
            {
                "step_number": step_number,
                "action": action,
                "assignments": assignments,
            }
        )

    return {  # type: ignore[return-value]
        "assertion": assertion_text,
        "assertion_line": assertion_line,
        "steps": steps,
    }
