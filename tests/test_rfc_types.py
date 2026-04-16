# tests/test_rfc_types.py
"""Tests for RFC service data types."""

from ivy_lsp.core.rfc.parser import RfcSection
from ivy_lsp.core.rfc.types import (
    CrossReference,
    NormativeStatement,
    RfcDocument,
    RfcMetadata,
    RfcSearchResult,
)


class TestNormativeStatement:
    def test_create(self):
        stmt = NormativeStatement(
            keyword="MUST",
            text="Endpoints MUST accept frames.",
            section="4.1",
            rfc="rfc9000",
        )
        assert stmt.tag == "rfc9000:4.1"

    def test_tag_generation(self):
        stmt = NormativeStatement(
            keyword="SHOULD NOT",
            text="Implementations SHOULD NOT send data.",
            section="6.2.1",
            rfc="rfc4271",
        )
        assert stmt.tag == "rfc4271:6.2.1"


class TestCrossReference:
    def test_same_document(self):
        ref = CrossReference(
            source_section="4.1",
            target_rfc=None,
            target_section="8.2",
            context="See Section 8.2 for details.",
        )
        assert ref.target_rfc is None
        assert ref.target_section == "8.2"

    def test_bare_rfc_mention(self):
        ref = CrossReference(
            source_section="3.0",
            target_rfc="rfc4271",
            target_section=None,
            context="As defined in RFC 4271.",
        )
        assert ref.target_section is None


class TestRfcMetadata:
    def test_create(self):
        meta = RfcMetadata(
            authors=["J. Doe"],
            date="2020-01",
            status="Standards Track",
            obsoletes=["rfc2616"],
            updates=[],
        )
        assert meta.authors == ["J. Doe"]
        assert meta.obsoletes == ["rfc2616"]


class TestRfcDocument:
    def test_create(self):
        doc = RfcDocument(
            number="rfc9000",
            title="QUIC: A UDP-Based Multiplexed and Secure Transport",
            sections=[
                RfcSection(number="1", title="Introduction", start_line=0, text="..."),
            ],
            metadata=RfcMetadata(
                authors=[],
                date="2021-05",
                status="Standards Track",
            ),
        )
        assert doc.number == "rfc9000"
        assert len(doc.sections) == 1


class TestRfcSearchResult:
    def test_create(self):
        result = RfcSearchResult(
            number="rfc4271",
            title="A Border Gateway Protocol 4 (BGP-4)",
            date="2006-01",
            status="Standards Track",
            abstract="This document discusses...",
        )
        assert result.number == "rfc4271"
