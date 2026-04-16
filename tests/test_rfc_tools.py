"""Tests for RFC MCP tools."""

import tempfile
from unittest.mock import patch

import pytest

from ivy_lsp.core.rfc.fetcher import FetchResult
from ivy_lsp.core.rfc.service import RfcService

SAMPLE_RFC_TEXT = """\
Network Working Group                                         Y. Rekhter
Request for Comments: 4271


         A Border Gateway Protocol 4 (BGP-4)

1.  Introduction

   The Border Gateway Protocol is inter-AS routing.

2.  Summary

   Peers MUST use TCP.  See Section 1 for background.
   Implementations SHOULD validate all fields.

3.  Messages

   Each message has a header.
"""


class TestIvyRfcGetTool:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir)

    @pytest.mark.asyncio
    async def test_get_full(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc",
            )
            doc = await self.service.get_rfc("4271", format="full")
        assert doc.number == "rfc4271"
        assert len(doc.sections) >= 3

    @pytest.mark.asyncio
    async def test_get_sections_toc(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc",
            )
            doc = await self.service.get_rfc("4271", format="sections")
        for s in doc.sections:
            assert s.text == ""


class TestIvyRfcSectionTool:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir)

    @pytest.mark.asyncio
    async def test_section_with_analysis(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc",
            )
            section = await self.service.get_section("4271", "2")
            assert section is not None
            stmts = self.service.extract_normative_statements(section, rfc="rfc4271")
            refs = self.service.extract_cross_references(section)

        assert len(stmts) >= 1
        assert any(s.keyword == "MUST" for s in stmts)
        assert any(r.target_section == "1" for r in refs)


class TestIvyRfcSearchTool:
    def setup_method(self):
        self.service = RfcService()

    @pytest.mark.asyncio
    async def test_offline_returns_empty(self):
        self.service._offline = True
        results = await self.service.search("BGP")
        assert results == []


class TestToolMetadataRegistration:
    def test_rfc_tool_in_metadata(self):
        from ivy_lsp.mcp.tools import get_tool_metadata

        meta = get_tool_metadata("ivy_rfc")
        assert meta, "ivy_rfc not registered"
        assert meta["category"] == "rfc"
        assert meta["needs_model"] is False
        assert meta["rendering"] == "raw"
        assert meta["tier"] == "fast"
