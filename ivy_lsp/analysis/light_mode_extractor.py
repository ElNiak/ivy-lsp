"""Regex-based requirement extraction for light mode (no Z3/full parser).

Extracts requirements from Ivy source text using regular expressions and
brace-depth tracking.  Intended as a fallback when the full Ivy parser is
unavailable.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ivy_lsp.analysis.requirement_graph import RequirementNode

logger = logging.getLogger(__name__)

# Regex patterns
MONITOR_RE = re.compile(
    r"\b(before|after|around)\s+([\w.]+)\s*(?:\([^)]*\))?\s*\{",
    re.MULTILINE,
)
REQUIRE_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+(.+?);\s*(?:#\s*\[(\w+(?::\w+(?:\.\w+)*)?)\])?\s*$",
    re.MULTILINE,
)
ASSIGN_RE = re.compile(r"^\s*([\w.]+(?:\([^)]*\))?)\s*:=", re.MULTILINE)

# Direct action body pattern: "action name(...) = { ... }"
ACTION_RE = re.compile(
    r"\baction\s+([\w.]+)\s*(?:\([^)]*\))?\s*(?:returns\s*\([^)]*\))?\s*=\s*\{",
    re.MULTILINE,
)


def extract_requirements_light(
    source: str,
    filepath: str,
    known_state_vars: Optional[Set[str]] = None,
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Regex-based requirement extraction for light mode.

    Returns:
        A tuple of (requirements, writes) where writes are
        ``(var_name, filepath, line)`` triples.
    """
    if not source:
        return [], []

    source_lines = source.split("\n")
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    # Phase 1: Find monitor blocks (before/after/around)
    monitor_blocks = _find_monitor_blocks(source)

    # Phase 2: Find direct action bodies
    action_blocks = _find_action_blocks(source)

    # Phase 3: Extract requirements from all blocks
    all_blocks = monitor_blocks + action_blocks
    for block in all_blocks:
        block_reqs, block_writes = _extract_from_block(
            block, filepath, source_lines
        )
        requirements.extend(block_reqs)
        writes.extend(block_writes)

    return requirements, writes


def _find_monitor_blocks(
    source: str,
) -> List[Dict[str, Any]]:
    """Find before/after/around monitor blocks using regex + brace depth."""
    blocks = []

    for m in MONITOR_RE.finditer(source):
        mixin_kind_raw = m.group(1)  # "before", "after", "around"
        monitor_action = m.group(2)  # action name
        open_brace_offset = m.end() - 1  # position of the opening brace

        # around desugars to before+after, but we treat it as "before" for now
        if mixin_kind_raw == "around":
            mixin_kind = "before"
        else:
            mixin_kind = mixin_kind_raw

        # Find the matching closing brace using depth tracking
        start_line = source[:m.start()].count("\n")
        body_start = m.end()
        body_end = _find_matching_brace(source, open_brace_offset)

        if body_end is None:
            body_end = len(source)

        blocks.append(
            {
                "monitor_action": monitor_action,
                "mixin_kind": mixin_kind,
                "body_text": source[body_start:body_end],
                "body_start_offset": body_start,
                "start_line": start_line,
            }
        )

    return blocks


def _find_action_blocks(
    source: str,
) -> List[Dict[str, Any]]:
    """Find direct action body blocks: 'action name(...) = { ... }'."""
    blocks = []

    for m in ACTION_RE.finditer(source):
        action_name = m.group(1)
        open_brace_offset = m.end() - 1

        start_line = source[:m.start()].count("\n")
        body_start = m.end()
        body_end = _find_matching_brace(source, open_brace_offset)

        if body_end is None:
            body_end = len(source)

        blocks.append(
            {
                "monitor_action": action_name,
                "mixin_kind": "direct",
                "body_text": source[body_start:body_end],
                "body_start_offset": body_start,
                "start_line": start_line,
            }
        )

    return blocks


def _find_matching_brace(source: str, open_pos: int) -> Optional[int]:
    """Find the position of the matching closing brace.

    *open_pos* points to the opening ``{``.  Returns the position
    just before the closing ``}``, or ``None`` if unmatched.
    """
    depth = 1
    i = open_pos + 1
    in_comment = False

    while i < len(source) and depth > 0:
        ch = source[i]

        if ch == "#" and not in_comment:
            # Skip to end of line (Ivy comment)
            nl = source.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return None


def _extract_from_block(
    block: Dict[str, Any],
    filepath: str,
    source_lines: List[str],
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Extract requirements and writes from a single block."""
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    body_text = block["body_text"]
    body_start_offset = block["body_start_offset"]
    monitor_action = block["monitor_action"]
    mixin_kind = block["mixin_kind"]

    # Find require/ensure/assume/assert in the block
    for m in REQUIRE_RE.finditer(body_text):
        kind = m.group(1)
        formula_text = m.group(2).strip()
        bracket_tag = m.group(3)  # may be None

        # Calculate absolute line number
        rel_offset = m.start()
        abs_offset = body_start_offset + rel_offset
        line = _offset_to_line(abs_offset, source_lines)

        req_id = f"{filepath}:{line}"
        requirements.append(
            RequirementNode(
                id=req_id,
                kind=kind,
                formula_text=formula_text,
                line=line,
                col=0,
                file=filepath,
                monitor_action=monitor_action,
                mixin_kind=mixin_kind,
                bracket_tag=bracket_tag,
            )
        )

    # Find assignments
    for m in ASSIGN_RE.finditer(body_text):
        var_name = m.group(1)
        # Strip function call syntax from LHS: "foo(X)" -> "foo"
        paren_idx = var_name.find("(")
        if paren_idx >= 0:
            var_name = var_name[:paren_idx]

        rel_offset = m.start()
        abs_offset = body_start_offset + rel_offset
        line = _offset_to_line(abs_offset, source_lines)
        writes.append((var_name, filepath, line))

    return requirements, writes


def _offset_to_line(offset: int, source_lines: List[str]) -> int:
    """Convert a character offset to a 0-based line number."""
    running = 0
    for i, line in enumerate(source_lines):
        running += len(line) + 1  # +1 for newline
        if running > offset:
            return i
    return max(0, len(source_lines) - 1)
