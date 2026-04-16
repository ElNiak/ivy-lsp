# ivy_lsp/core/rfc/types.py
"""Data types for the RFC service layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ivy_lsp.core.rfc.parser import RfcSection


@dataclass
class NormativeStatement:
    """A normative statement extracted from RFC text."""

    keyword: str
    text: str
    section: str
    rfc: str

    @property
    def tag(self) -> str:
        """Unique identifier combining RFC number and section."""
        return f"{self.rfc}:{self.section}"


@dataclass
class CrossReference:
    """A cross-reference to another RFC or section."""

    source_section: str
    target_rfc: Optional[str]
    target_section: Optional[str]
    context: str


@dataclass
class RfcMetadata:
    """Metadata extracted from an RFC document header."""

    authors: List[str] = field(default_factory=list)
    date: str = ""
    status: str = ""
    obsoletes: List[str] = field(default_factory=list)
    updates: List[str] = field(default_factory=list)


@dataclass
class RfcDocument:
    """A fully parsed RFC document."""

    number: str
    title: str
    sections: List[RfcSection]
    metadata: RfcMetadata = field(default_factory=RfcMetadata)


@dataclass
class RfcSearchResult:
    """A single result from an IETF Datatracker search."""

    number: str
    title: str
    date: str
    status: str
    abstract: str
