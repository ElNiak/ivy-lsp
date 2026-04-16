# tests/test_rfc_search.py
"""Tests for IETF Datatracker search client."""

import json
from unittest.mock import patch

import pytest

from ivy_lsp.core.rfc.search import DataTrackerClient
from ivy_lsp.core.rfc.types import RfcSearchResult

MOCK_RESPONSE = {
    "meta": {"total_count": 2},
    "objects": [
        {
            "name": "rfc4271",
            "title": "A Border Gateway Protocol 4 (BGP-4)",
            "time": "2006-01-01T00:00:00",
            "std_level": "/api/v1/name/stdlevelname/ps/",
            "abstract": "This document discusses the Border Gateway Protocol.",
        },
        {
            "name": "rfc4456",
            "title": "BGP Route Reflection",
            "time": "2006-04-01T00:00:00",
            "std_level": "/api/v1/name/stdlevelname/ps/",
            "abstract": "Route reflection for BGP.",
        },
    ],
}


class TestDataTrackerClient:
    def setup_method(self):
        self.client = DataTrackerClient()

    @pytest.mark.asyncio
    async def test_search_parses_results(self):
        with patch("ivy_lsp.core.rfc.search._fetch_json", return_value=MOCK_RESPONSE):
            results = await self.client.search("BGP", limit=5)

        assert len(results) == 2
        assert isinstance(results[0], RfcSearchResult)
        assert results[0].number == "rfc4271"
        assert results[0].title == "A Border Gateway Protocol 4 (BGP-4)"

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        empty_response = {"meta": {"total_count": 0}, "objects": []}
        with patch("ivy_lsp.core.rfc.search._fetch_json", return_value=empty_response):
            results = await self.client.search("nonexistent_xyz")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_caches_results(self):
        with patch(
            "ivy_lsp.core.rfc.search._fetch_json", return_value=MOCK_RESPONSE
        ) as mock_fetch:
            results1 = await self.client.search("BGP", limit=5)
            results2 = await self.client.search("BGP", limit=5)

        # Second call should hit cache, so only one HTTP call
        assert mock_fetch.call_count == 1
        assert len(results2) == 2

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        with patch(
            "ivy_lsp.core.rfc.search._fetch_json",
            side_effect=OSError("Connection refused"),
        ):
            results = await self.client.search("BGP")

        assert results == []

    def test_extract_rfc_number(self):
        assert self.client._extract_rfc_number("rfc4271") == "rfc4271"
        assert self.client._extract_rfc_number("RFC 9000") == "rfc9000"

    def test_extract_std_level(self):
        assert (
            self.client._extract_std_level("/api/v1/name/stdlevelname/ps/")
            == "Proposed Standard"
        )
        assert self.client._extract_std_level("unknown") == "unknown"
