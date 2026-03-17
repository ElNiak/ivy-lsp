"""Tests for RFC text parser."""

from ivy_lsp.rfc.parser import get_section_text, parse_rfc_text

SAMPLE_RFC = """\
RFC 9000  QUIC: A UDP-Based Multiplexed Transport

Abstract

   This document defines QUIC transport protocol.

1.  Introduction

   QUIC is a new transport protocol.

2.  Streams

   Streams in QUIC provide a lightweight, ordered byte-stream
   abstraction to an application.

2.1.  Stream Types and Identifiers

   Endpoints MUST NOT exceed the limit set by its peer.

2.2.  Sending and Receiving Data

   Endpoints SHOULD use flow control mechanisms.

3.  Flow Control

   A sender MAY choose to limit the rate at which it sends data.
"""


class TestParseRfcText:
    def test_detects_rfc_number(self):
        result = parse_rfc_text(SAMPLE_RFC)
        assert result.rfc_number == "9000"

    def test_detects_sections(self):
        result = parse_rfc_text(SAMPLE_RFC)
        numbers = [s.number for s in result.sections]
        assert "1" in numbers
        assert "2" in numbers
        assert "2.1" in numbers
        assert "2.2" in numbers
        assert "3" in numbers

    def test_section_titles(self):
        result = parse_rfc_text(SAMPLE_RFC)
        by_num = {s.number: s for s in result.sections}
        assert "Introduction" in by_num["1"].title
        assert "Streams" in by_num["2"].title
        assert "Stream Types" in by_num["2.1"].title

    def test_section_text_content(self):
        result = parse_rfc_text(SAMPLE_RFC)
        by_num = {s.number: s for s in result.sections}
        assert "MUST NOT exceed" in by_num["2.1"].text

    def test_empty_input(self):
        result = parse_rfc_text("")
        assert result.sections == []
        assert result.rfc_number == ""

    def test_no_sections(self):
        result = parse_rfc_text("Just some plain text\nwith no sections.")
        assert result.sections == []


class TestGetSectionText:
    def test_exact_section(self):
        parsed = parse_rfc_text(SAMPLE_RFC)
        text = get_section_text(parsed, ["2.1"])
        assert "MUST NOT exceed" in text
        assert "flow control" not in text.lower()

    def test_prefix_section(self):
        """Requesting section "2" should include 2, 2.1, and 2.2."""
        parsed = parse_rfc_text(SAMPLE_RFC)
        text = get_section_text(parsed, ["2"])
        assert "Streams" in text or "lightweight" in text
        assert "MUST NOT exceed" in text
        assert "flow control" in text.lower()

    def test_multiple_sections(self):
        parsed = parse_rfc_text(SAMPLE_RFC)
        text = get_section_text(parsed, ["1", "3"])
        assert "new transport" in text.lower()
        assert "MAY choose" in text

    def test_nonexistent_section(self):
        parsed = parse_rfc_text(SAMPLE_RFC)
        text = get_section_text(parsed, ["99"])
        assert text == ""


class TestAsciiArtRejection:
    def test_table_borders_not_sections(self):
        text = """\
1.  Introduction

   Some text here.

+--------+--------+--------+
| Header | Header | Header |
+--------+--------+--------+

2.  Next Section

   More text.
"""
        result = parse_rfc_text(text)
        numbers = [s.number for s in result.sections]
        assert "1" in numbers
        assert "2" in numbers
        # No spurious sections from table borders
        assert len(result.sections) == 2
