# tests/test_rfc_analyzer.py
"""Tests for RFC structured analyzer."""

from ivy_lsp.core.rfc.analyzer import RfcAnalyzer
from ivy_lsp.core.rfc.parser import RfcSection
from ivy_lsp.core.rfc.types import CrossReference, NormativeStatement

RFC_SECTION_TEXT = """\
   A BGP speaker MUST NOT allow a TCP connection to be configured for a
   remote peer if it already has an established connection to that peer.

   If a BGP speaker receives a connection request and it is not in the
   Idle state, it SHOULD reject the connection attempt.

   Implementations MAY impose additional restrictions on the use of
   this field.  See Section 6.3 for more details.

   As described in [RFC1771], the Hold Time MUST be either zero or at
   least three seconds.
"""

CROSS_REF_TEXT = """\
   The UPDATE message (see Section 4.3) is used to transfer routing
   information between BGP peers.  As defined in RFC 1771, the message
   format has changed.  Refer to [RFC4456] Section 2.1 for route
   reflection procedures.
"""


class TestNormativeExtraction:
    def setup_method(self):
        self.analyzer = RfcAnalyzer()

    def test_extracts_must(self):
        section = RfcSection(
            number="6.2", title="Test", start_line=0, text=RFC_SECTION_TEXT
        )
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc4271")
        keywords = [s.keyword for s in stmts]
        assert "MUST NOT" in keywords
        assert "MUST" in keywords

    def test_extracts_should(self):
        section = RfcSection(
            number="6.2", title="Test", start_line=0, text=RFC_SECTION_TEXT
        )
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc4271")
        keywords = [s.keyword for s in stmts]
        assert "SHOULD" in keywords

    def test_extracts_may(self):
        section = RfcSection(
            number="6.2", title="Test", start_line=0, text=RFC_SECTION_TEXT
        )
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc4271")
        keywords = [s.keyword for s in stmts]
        assert "MAY" in keywords

    def test_tag_matches_section(self):
        section = RfcSection(
            number="6.2", title="Test", start_line=0, text=RFC_SECTION_TEXT
        )
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc4271")
        for stmt in stmts:
            assert stmt.tag == "rfc4271:6.2"

    def test_deduplicates_multi_keyword_sentence(self):
        text = "A sender MUST NOT send and SHOULD NOT accept invalid frames."
        section = RfcSection(number="3.1", title="Test", start_line=0, text=text)
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc9000")
        texts = [s.text for s in stmts]
        assert len([t for t in texts if "invalid frames" in t]) == 1

    def test_handles_line_wrapped_sentences(self):
        text = "   The implementation\n   MUST handle this\n   correctly."
        section = RfcSection(number="2.0", title="Test", start_line=0, text=text)
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc9000")
        assert len(stmts) == 1
        assert "MUST" in stmts[0].keyword

    def test_empty_section(self):
        section = RfcSection(number="1.0", title="Test", start_line=0, text="")
        stmts = self.analyzer.extract_normative_statements(section, rfc="rfc9000")
        assert stmts == []


class TestCrossReferenceExtraction:
    def setup_method(self):
        self.analyzer = RfcAnalyzer()

    def test_same_document_section_ref(self):
        section = RfcSection(
            number="4.1", title="Test", start_line=0, text=CROSS_REF_TEXT
        )
        refs = self.analyzer.extract_cross_references(section)
        section_refs = [
            r for r in refs if r.target_rfc is None and r.target_section == "4.3"
        ]
        assert len(section_refs) == 1

    def test_bare_rfc_mention(self):
        section = RfcSection(
            number="4.1", title="Test", start_line=0, text=CROSS_REF_TEXT
        )
        refs = self.analyzer.extract_cross_references(section)
        bare_refs = [
            r for r in refs if r.target_rfc == "rfc1771" and r.target_section is None
        ]
        assert len(bare_refs) >= 1

    def test_rfc_with_section(self):
        section = RfcSection(
            number="4.1", title="Test", start_line=0, text=CROSS_REF_TEXT
        )
        refs = self.analyzer.extract_cross_references(section)
        full_refs = [
            r for r in refs if r.target_rfc == "rfc4456" and r.target_section == "2.1"
        ]
        assert len(full_refs) == 1

    def test_empty_section(self):
        section = RfcSection(number="1.0", title="Test", start_line=0, text="")
        refs = self.analyzer.extract_cross_references(section)
        assert refs == []
