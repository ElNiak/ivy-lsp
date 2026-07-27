"""Structured semantic analysis of RFC section text.

Extracts normative statements (RFC 2119 keywords) and cross-references
from parsed RFC sections. Separate from parser.py to keep section
detection and semantic analysis as distinct concerns.
"""

from __future__ import annotations

import re
from typing import List

from ivy_lsp.core.rfc.parser import RfcSection
from ivy_lsp.core.rfc.types import CrossReference, NormativeStatement

_KEYWORD_PRIORITY = [
    "MUST NOT",
    "SHALL NOT",
    "SHOULD NOT",
    "MUST",
    "SHALL",
    "REQUIRED",
    "SHOULD",
    "RECOMMENDED",
    "MAY",
    "OPTIONAL",
]

_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _KEYWORD_PRIORITY) + r")\b"
)

_SECTION_REF_RE = re.compile(r"(?:see\s+)?Section\s+(\d+(?:\.\d+)*)", re.IGNORECASE)

_RFC_SECTION_REF_RE = re.compile(
    r"\[RFC\s*(\d+)\]\s*,?\s*Section\s+(\d+(?:\.\d+)*)", re.IGNORECASE
)

_BARE_RFC_RE = re.compile(r"(?:\[RFC\s*(\d+)\]|RFC\s+(\d+))", re.IGNORECASE)

# Pre-compiled prefix check: used to skip same-document Section refs
# that are part of an [RFCnnnn] Section pattern already captured.
_RFC_PREFIX_RE = re.compile(r"\[RFC\s*\d+\]\s*,?\s*$", re.IGNORECASE)


def _join_lines(text: str) -> str:
    return re.sub(r"\n\s+", " ", text).strip()


def _split_sentences(text: str) -> List[str]:
    joined = _join_lines(text)
    parts = re.split(r"\.(?:\s+(?=[A-Z])|\s*$)", joined)
    return [p.strip() + "." for p in parts if p.strip()]


class RfcAnalyzer:
    """Extracts normative statements and cross-references from RFC sections."""

    def extract_normative_statements(
        self, section: RfcSection, rfc: str
    ) -> List[NormativeStatement]:
        """Extract RFC 2119 normative statements from a section.

        Args:
            section: Parsed RFC section containing text and metadata.
            rfc: RFC identifier (e.g. "rfc4271") to attach to each statement.

        Returns:
            List of normative statements, deduplicated per sentence.
        """
        if not section.text:
            return []

        results: list[NormativeStatement] = []
        seen_sentences: set[str] = set()

        for sentence in _split_sentences(section.text):
            matches = _KEYWORD_RE.findall(sentence)
            if not matches:
                continue

            norm = re.sub(r"\s+", " ", sentence.strip())
            if norm in seen_sentences:
                continue
            seen_sentences.add(norm)

            best_keyword = min(matches, key=lambda k: _KEYWORD_PRIORITY.index(k))

            results.append(
                NormativeStatement(
                    keyword=best_keyword,
                    text=norm,
                    section=section.number,
                    rfc=rfc,
                )
            )

        return results

    def extract_cross_references(self, section: RfcSection) -> List[CrossReference]:
        """Extract cross-references to other RFC sections from a section.

        Args:
            section: Parsed RFC section to scan for references.

        Returns:
            List of cross-references found, deduplicated by target.
        """
        if not section.text:
            return []

        joined = _join_lines(section.text)
        results: list[CrossReference] = []
        seen: set[tuple] = set()

        for m in _RFC_SECTION_REF_RE.finditer(joined):
            rfc_num = m.group(1)
            sec_num = m.group(2)
            key = (f"rfc{rfc_num}", sec_num)
            if key not in seen:
                seen.add(key)
                start = max(0, m.start() - 40)
                end = min(len(joined), m.end() + 40)
                results.append(
                    CrossReference(
                        source_section=section.number,
                        target_rfc=f"rfc{rfc_num}",
                        target_section=sec_num,
                        context=joined[start:end].strip(),
                    )
                )

        for m in _SECTION_REF_RE.finditer(joined):
            sec_num = m.group(1)
            prefix = joined[max(0, m.start() - 15) : m.start()]
            if _RFC_PREFIX_RE.search(prefix):
                continue
            key = (None, sec_num)
            if key not in seen:
                seen.add(key)
                start = max(0, m.start() - 40)
                end = min(len(joined), m.end() + 40)
                results.append(
                    CrossReference(
                        source_section=section.number,
                        target_rfc=None,
                        target_section=sec_num,
                        context=joined[start:end].strip(),
                    )
                )

        for m in _BARE_RFC_RE.finditer(joined):
            rfc_num = m.group(1) or m.group(2)
            rfc_id = f"rfc{rfc_num}"
            if any(k[0] == rfc_id for k in seen):
                continue
            key = (rfc_id, None)
            if key not in seen:
                seen.add(key)
                start = max(0, m.start() - 40)
                end = min(len(joined), m.end() + 40)
                results.append(
                    CrossReference(
                        source_section=section.number,
                        target_rfc=rfc_id,
                        target_section=None,
                        context=joined[start:end].strip(),
                    )
                )

        return results
