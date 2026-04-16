# ivy_lsp/core/rfc/service.py
"""Unified RFC service layer.

Composes fetcher, parser, analyzer, cache, and search into a single
facade for all RFC operations.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

from ivy_lsp.core.rfc.analyzer import RfcAnalyzer
from ivy_lsp.core.rfc.cache import RfcCache
from ivy_lsp.core.rfc.fetcher import FetchError, fetch_rfc
from ivy_lsp.core.rfc.parser import RfcSection, parse_rfc_text
from ivy_lsp.core.rfc.search import DataTrackerClient
from ivy_lsp.core.rfc.types import (
    CrossReference,
    NormativeStatement,
    RfcDocument,
    RfcMetadata,
    RfcSearchResult,
)

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^(rfc\d+):(.+)$", re.IGNORECASE)
_RFC_NUM_RE = re.compile(r"^(?:rfc\s*)?(\d+)$", re.IGNORECASE)


class RfcService:
    """Unified entry point for RFC document operations.

    Args:
        cache_dir: Directory for persistent disk cache.
        cache_ttl: Seconds before cached entries are stale.
        local_dir: Directory of user-provided local RFC files.
        offline: If True, never attempt remote fetch.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        cache_ttl: int = 3600,
        local_dir: str | Path | None = None,
        offline: bool = False,
    ):
        """Initialize the RFC service with cache and search configuration."""
        self._cache = RfcCache(
            cache_dir=cache_dir, cache_ttl=cache_ttl, local_dir=local_dir
        )
        self._analyzer = RfcAnalyzer()
        self._search_client = DataTrackerClient()
        self._offline = offline

    async def get_rfc(self, number: str, format: str = "full") -> RfcDocument:
        """Fetch and return an RFC document.

        Args:
            number: RFC number (e.g. "4271" or "rfc4271").
            format: One of "full", "metadata", or "sections".

        Returns:
            Parsed RfcDocument.
        """
        rfc_id = self._normalize_rfc_id(number)
        text = await self._fetch_text(rfc_id, number)
        parsed = parse_rfc_text(text)

        rfc_number = parsed.rfc_number or rfc_id
        if not rfc_number.startswith("rfc"):
            rfc_number = f"rfc{rfc_number}"
        rfc_number = rfc_number.lower()

        metadata = RfcMetadata(date="", status="")

        if format == "metadata":
            return RfcDocument(
                number=rfc_number,
                title=parsed.title,
                sections=[],
                metadata=metadata,
            )

        if format == "sections":
            toc_sections = [
                RfcSection(
                    number=s.number,
                    title=s.title,
                    start_line=s.start_line,
                    text="",
                )
                for s in parsed.sections
            ]
            return RfcDocument(
                number=rfc_number,
                title=parsed.title,
                sections=toc_sections,
                metadata=metadata,
            )

        return RfcDocument(
            number=rfc_number,
            title=parsed.title,
            sections=parsed.sections,
            metadata=metadata,
        )

    async def get_section(self, number: str, section: str) -> Optional[RfcSection]:
        """Return a specific section from an RFC, or None if not found.

        Args:
            number: RFC number.
            section: Section number string (e.g. "3.1").

        Returns:
            Matching RfcSection or None.
        """
        doc = await self.get_rfc(number, format="full")
        for s in doc.sections:
            if s.number == section:
                return s
        return None

    async def search(self, query: str, limit: int = 10) -> List[RfcSearchResult]:
        """Search for RFCs matching a query string.

        Args:
            query: Free-text search query.
            limit: Maximum number of results.

        Returns:
            List of RfcSearchResult, empty when offline.
        """
        if self._offline:
            logger.info("RFC search skipped: offline mode")
            return []
        return await self._search_client.search(query, limit=limit)

    def extract_normative_statements(
        self, section: RfcSection, rfc: str
    ) -> List[NormativeStatement]:
        """Extract MUST/SHOULD/MAY statements from a section.

        Args:
            section: The RFC section to analyze.
            rfc: RFC identifier string.

        Returns:
            List of NormativeStatement objects.
        """
        return self._analyzer.extract_normative_statements(section, rfc=rfc)

    def extract_cross_references(self, section: RfcSection) -> List[CrossReference]:
        """Extract cross-references (e.g. [RFC1771]) from a section.

        Args:
            section: The RFC section to analyze.

        Returns:
            List of CrossReference objects.
        """
        return self._analyzer.extract_cross_references(section)

    async def resolve_tag_to_section(self, tag: str) -> Optional[RfcSection]:
        """Resolve a tag like "rfc4271:3.1" to its section, or None if invalid.

        Args:
            tag: Tag string in the format "rfcNNNN:section".

        Returns:
            Matching RfcSection or None.
        """
        m = _TAG_RE.match(tag)
        if not m:
            return None
        rfc_id = m.group(1).lower()
        section_num = m.group(2)
        try:
            return await self.get_section(rfc_id, section_num)
        except FetchError:
            logger.warning("Failed to resolve tag '%s': fetch error", tag)
            return None

    def set_local_cache_dir(self, path: Path) -> None:
        """Override the local RFC file directory at runtime.

        Args:
            path: New local directory path.
        """
        self._cache._local_dir = path

    def clear_cache(self) -> None:
        """Clear all disk-cached RFC entries."""
        self._cache.clear()

    async def _fetch_text(self, rfc_id: str, original_source: str) -> str:
        local = self._cache.get_local(rfc_id)
        if local is not None:
            return local["text"]

        cached = self._cache.get(rfc_id)
        if cached is not None:
            return cached["text"]

        if self._offline:
            raise FetchError(
                f"RFC {rfc_id} not found locally and offline mode is enabled"
            )

        result = await fetch_rfc(original_source, use_cache=False)
        self._cache.put(rfc_id, result.text, result.content_hash, result.source)
        return result.text

    @staticmethod
    def _normalize_rfc_id(number: str) -> str:
        number = number.strip()
        m = _RFC_NUM_RE.match(number)
        if m:
            return f"rfc{m.group(1)}"
        if number.lower().startswith("rfc"):
            return number.lower()
        return number.lower()
