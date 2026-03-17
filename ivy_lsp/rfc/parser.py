"""RFC text parser: section detection and requirement extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class RfcSection:
    """A section extracted from an RFC document."""

    number: str  # e.g. "4.1"
    title: str  # e.g. "Stream Types and Identifiers"
    start_line: int  # 0-based line index
    text: str = ""  # full section text content


@dataclass
class ParsedRfc:
    """Result of parsing an RFC document into sections."""

    sections: List[RfcSection] = field(default_factory=list)
    title: str = ""
    rfc_number: str = ""


# Section header patterns
# Matches lines like "4.1.  Stream Types and Identifiers"
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s{2,}(\S.*)$")

# ASCII art / table border patterns to reject
_ASCII_ART_RE = re.compile(r"^[\s\-+|=*#_]{5,}$")

# RFC title line pattern (e.g. "RFC 9000  QUIC: ...")
_RFC_TITLE_RE = re.compile(r"RFC\s*(\d+)\s")


def _is_valid_section_title(title: str) -> bool:
    """Check if a candidate section title looks like a real title."""
    # Reject if it's too short or all digits
    stripped = title.strip()
    if len(stripped) < 2:
        return False
    if stripped.isdigit():
        return False
    # Reject if it looks like a figure/table reference
    if stripped.startswith("Figure") or stripped.startswith("Table"):
        return False
    return True


def _parse_section_number(num_str: str) -> list[int]:
    """Parse "4.1.2" into [4, 1, 2] for comparison."""
    try:
        return [int(p) for p in num_str.split(".")]
    except ValueError:
        return []


def _is_monotonic(prev: list[int], curr: list[int]) -> bool:
    """Check if section numbers are monotonically increasing or nested."""
    if not prev:
        return True
    # Same depth: curr must be > prev
    if len(curr) == len(prev):
        return curr > prev
    # Deeper nesting: first len(prev) parts must match prev prefix
    if len(curr) > len(prev):
        return curr[: len(prev)] >= prev
    # Going up: first len(curr)-1 parts must be >= prev prefix
    return True  # Allow going back up to a higher-level section


def parse_rfc_text(text: str) -> ParsedRfc:
    """Parse RFC text into sections with multi-signal detection.

    Uses multiple signals to identify section headers:
    1. Regex match for section number + title pattern
    2. Blank line before the candidate (section headers follow blank lines)
    3. Title validation (not a figure/table reference, not too short)
    4. ASCII art rejection
    5. Monotonic section number validation
    """
    lines = text.split("\n")
    result = ParsedRfc()

    # Try to extract RFC number from first few lines
    for line in lines[:20]:
        m = _RFC_TITLE_RE.search(line)
        if m:
            result.rfc_number = m.group(1)
            # Use the rest of the line as title
            title_part = line[m.end() :].strip()
            if title_part:
                result.title = title_part
            break

    # Collect section header candidates
    candidates: list[tuple[int, str, str]] = []  # (line_idx, number, title)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Skip ASCII art
        if _ASCII_ART_RE.match(stripped):
            continue

        m = _SECTION_RE.match(stripped)
        if not m:
            continue

        number = m.group(1)
        title = m.group(2).strip()

        # Signal: blank line before header
        if i > 0 and lines[i - 1].strip() != "":
            continue

        if not _is_valid_section_title(title):
            continue

        candidates.append((i, number, title))

    # Filter with monotonic section number validation
    filtered: list[tuple[int, str, str]] = []
    prev_parts: list[int] = []
    for line_idx, number, title in candidates:
        parts = _parse_section_number(number)
        if _is_monotonic(prev_parts, parts):
            filtered.append((line_idx, number, title))
            prev_parts = parts

    # Build sections with text content
    for idx, (line_idx, number, title) in enumerate(filtered):
        # Section text extends until the next section header
        if idx + 1 < len(filtered):
            end_line = filtered[idx + 1][0]
        else:
            end_line = len(lines)

        # Extract section text (skip the header line itself)
        section_lines = lines[line_idx + 1 : end_line]
        section_text = "\n".join(section_lines).strip()

        result.sections.append(
            RfcSection(
                number=number,
                title=title,
                start_line=line_idx,
                text=section_text,
            )
        )

    return result


def get_section_text(parsed: ParsedRfc, section_numbers: list[str]) -> str:
    """Extract text from specific sections of a parsed RFC.

    Args:
        parsed: A ParsedRfc from parse_rfc_text()
        section_numbers: List of section numbers to include (e.g. ["4", "4.1"])

    Returns:
        Combined text from matching sections.
    """
    parts: list[str] = []
    for section in parsed.sections:
        for requested in section_numbers:
            # Match exact or prefix (e.g. "4" matches "4", "4.1", "4.2")
            if section.number == requested or section.number.startswith(
                requested + "."
            ):
                parts.append(section.text)
                break
    return "\n\n".join(parts)
