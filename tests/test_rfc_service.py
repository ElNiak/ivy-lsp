# tests/test_rfc_service.py
"""Tests for the RfcService facade."""

import os
import tempfile
from unittest.mock import patch

import pytest

from ivy_lsp.core.rfc.service import RfcService
from ivy_lsp.core.rfc.types import RfcDocument

SAMPLE_RFC_TEXT = """\
Network Working Group                                         Y. Rekhter
Request for Comments: 4271                                      T. Li


         A Border Gateway Protocol 4 (BGP-4)

Status of this Memo

   This document specifies an Internet standards track protocol.

1.  Introduction

   The Border Gateway Protocol (BGP) is an inter-Autonomous System
   routing protocol.

2.  Summary of Operation

   BGP peers MUST use TCP as the transport protocol.

3.  Message Formats

3.1.  Message Header Format

   Each message has a fixed-size header.  See Section 2 for context.
   Implementations SHOULD follow [RFC1771] guidelines.
"""


class TestRfcServiceGetRfc:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir)

    @pytest.mark.asyncio
    async def test_get_rfc_full(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            doc = await self.service.get_rfc("4271", format="full")

        assert isinstance(doc, RfcDocument)
        assert doc.number == "rfc4271"
        assert len(doc.sections) > 0

    @pytest.mark.asyncio
    async def test_get_rfc_metadata(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            doc = await self.service.get_rfc("4271", format="metadata")

        assert doc.number == "rfc4271"
        assert doc.sections == []

    @pytest.mark.asyncio
    async def test_get_rfc_sections_toc(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            doc = await self.service.get_rfc("4271", format="sections")

        assert len(doc.sections) > 0
        for s in doc.sections:
            assert s.text == ""


class TestRfcServiceGetSection:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir)

    @pytest.mark.asyncio
    async def test_get_existing_section(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            section = await self.service.get_section("4271", "2")

        assert section is not None
        assert section.number == "2"
        assert "MUST" in section.text

    @pytest.mark.asyncio
    async def test_get_nonexistent_section(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            section = await self.service.get_section("4271", "99.9")

        assert section is None


class TestRfcServiceTagResolution:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir)

    @pytest.mark.asyncio
    async def test_resolve_tag(self):
        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            from ivy_lsp.core.rfc.fetcher import FetchResult

            mock_fetch.return_value = FetchResult(
                text=SAMPLE_RFC_TEXT,
                source="https://rfc-editor.org/rfc/rfc4271.txt",
                content_hash="abc123",
            )
            section = await self.service.resolve_tag_to_section("rfc4271:2")

        assert section is not None
        assert section.number == "2"

    @pytest.mark.asyncio
    async def test_resolve_invalid_tag(self):
        section = await self.service.resolve_tag_to_section("not-a-tag")
        assert section is None


class TestRfcServiceLocalCache:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.local_dir = tempfile.mkdtemp()
        self.service = RfcService(cache_dir=self.tmpdir, local_dir=self.local_dir)

    @pytest.mark.asyncio
    async def test_local_file_used_first(self):
        local_path = os.path.join(self.local_dir, "rfc4271.txt")
        with open(local_path, "w") as f:
            f.write(SAMPLE_RFC_TEXT)

        with patch("ivy_lsp.core.rfc.service.fetch_rfc") as mock_fetch:
            doc = await self.service.get_rfc("4271")
            mock_fetch.assert_not_called()

        assert doc.number == "rfc4271"
