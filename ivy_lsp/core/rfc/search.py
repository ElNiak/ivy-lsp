# ivy_lsp/core/rfc/search.py
"""Async client for the IETF Datatracker REST API.

Uses urllib.request + asyncio.to_thread, matching the pattern in fetcher.py.
No external HTTP dependencies required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import List

from ivy_lsp.core.rfc.types import RfcSearchResult

logger = logging.getLogger(__name__)

_DATATRACKER_BASE = "https://datatracker.ietf.org/api/v1/doc/document/"
_REQUEST_TIMEOUT = 10  # seconds
_CACHE_TTL = 300  # 5 minutes for search results

_STD_LEVEL_MAP = {
    "ps": "Proposed Standard",
    "ds": "Draft Standard",
    "std": "Internet Standard",
    "bcp": "Best Current Practice",
    "inf": "Informational",
    "exp": "Experimental",
    "hist": "Historic",
}


def _fetch_json(url: str) -> dict:
    """Blocking JSON fetch via urllib (run in thread pool)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class DataTrackerClient:
    """Async client for searching RFCs via the IETF Datatracker API."""

    def __init__(self) -> None:
        """Initialize with an empty result cache."""
        self._search_cache: dict[str, tuple[List[RfcSearchResult], float]] = {}

    async def search(self, query: str, limit: int = 10) -> List[RfcSearchResult]:
        """Search for RFCs matching *query*.

        Args:
            query: Search terms.
            limit: Maximum number of results.

        Returns:
            List of RfcSearchResult objects.
        """
        cache_key = f"{query}:{limit}"
        if cache_key in self._search_cache:
            results, ts = self._search_cache[cache_key]
            if time.time() - ts < _CACHE_TTL:
                return results

        params = urllib.parse.urlencode(
            {
                "format": "json",
                "name__contains": query.lower(),
                "type": "rfc",
                "limit": str(limit),
            }
        )
        url = f"{_DATATRACKER_BASE}?{params}"

        try:
            data = await asyncio.to_thread(_fetch_json, url)
        except (OSError, json.JSONDecodeError, TimeoutError) as exc:
            logger.warning("Datatracker search failed: %s", exc)
            return []

        results: list[RfcSearchResult] = []
        for obj in data.get("objects", []):
            name = obj.get("name", "")
            results.append(
                RfcSearchResult(
                    number=self._extract_rfc_number(name),
                    title=obj.get("title", ""),
                    date=obj.get("time", "")[:10],
                    status=self._extract_std_level(obj.get("std_level", "")),
                    abstract=obj.get("abstract", "")[:500],
                )
            )

        self._search_cache[cache_key] = (results, time.time())
        return results

    @staticmethod
    def _extract_rfc_number(name: str) -> str:
        """Normalize an RFC name like 'rfc4271' or 'RFC 9000' to 'rfcNNNN'."""
        m = re.search(r"(\d+)", name)
        return f"rfc{m.group(1)}" if m else name.lower()

    @staticmethod
    def _extract_std_level(level_uri: str) -> str:
        """Convert Datatracker std_level URI to human-readable string."""
        for key, label in _STD_LEVEL_MAP.items():
            if f"/{key}/" in level_uri:
                return label
        return level_uri if level_uri else "Unknown"
