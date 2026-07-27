"""Tests for tag disambiguation: bare numeric filtering and is_tag_covered."""

from ivy_lsp.core.semantic.rfc_annotations import (
    TagResolution,
    is_tag_covered,
    normalize_tag_with_diagnostics,
    parse_file_rfc_annotations,
)


class TestBareNumericRejection:
    """Step 1.1: _is_rfc_annotation filters bare numeric tags on code lines."""

    def test_bare_numeric_on_code_line_rejected(self):
        """Bare numeric on code line (payload : frame.arr # [8]) rejected."""
        source = "payload : frame.arr # [8]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 0

    def test_bare_numeric_on_require_line_rejected(self):
        """Bare numeric on require line (require x > 0; # [1]) rejected."""
        source = "require x > 0; # [1]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 0

    def test_standalone_bare_numeric_comment_rejected(self):
        """# [8] as a standalone bare numeric is rejected (field marker, not RFC)."""
        source = "# [8]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 0

    def test_standalone_bare_numeric_indented_rejected(self):
        """# [8] (indented standalone bare numeric) is rejected."""
        source = "    # [8]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 0

    def test_qualified_tag_on_code_line_accepted(self):
        """rfc9000:4.1 is not a bare numeric — always accepted."""
        source = "require x > 0; # [rfc9000:4.1]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 1
        assert anns[0].tags == ["rfc9000:4.1"]

    def test_dotted_tag_on_code_line_accepted(self):
        """4.1 is not bare numeric (has dot) — accepted on code lines."""
        source = "require x > 0; # [4.1]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 1
        assert anns[0].tags == ["4.1"]

    def test_mixed_bare_and_qualified_accepted(self):
        """If any tag is non-bare-numeric, all are kept."""
        source = "require x; # [8, rfc9000:4.1]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 1
        assert "8" in anns[0].tags
        assert "rfc9000:4.1" in anns[0].tags

    def test_multi_file_lines(self):
        """Multiple lines: only genuine annotations survive."""
        source = "\n".join(
            [
                "payload : frame.arr # [1]",  # rejected: bare numeric on code
                "# [rfc9000:4.1]",  # accepted: qualified
                "require x; # [42]",  # rejected: bare numeric on code
                "    # [7]",  # rejected: standalone bare numeric
            ]
        )
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 1
        assert anns[0].tags == ["rfc9000:4.1"]


class TestIsTagCovered:
    """Step 1.2: is_tag_covered wraps normalize_tag_to_manifest_ids."""

    def test_exact_match_covered(self):
        assert is_tag_covered("rfc9000:4.1", {"rfc9000:4.1", "rfc9000:8.1"})

    def test_prefix_match_covered(self):
        """Tag '4' matches 'rfc9000:4.1' via prefix expansion."""
        assert is_tag_covered("4", {"rfc9000:4.1", "rfc9000:8.1"})

    def test_no_match_not_covered(self):
        assert not is_tag_covered("99", {"rfc9000:4.1"})

    def test_bare_section_covered(self):
        assert is_tag_covered("4.1", {"rfc9000:4.1"})

    def test_empty_manifest_not_covered(self):
        assert not is_tag_covered("4", set())


class TestNormalizeTagWithDiagnostics:
    """Step 1.6: normalize_tag_with_diagnostics detects ambiguity."""

    def test_unambiguous_bare_tag(self):
        keys = {"rfc9000:4.1", "rfc9000:4.6"}
        result = normalize_tag_with_diagnostics("4", keys)
        assert isinstance(result, TagResolution)
        assert result.matched_ids == {"rfc9000:4.1", "rfc9000:4.6"}
        assert result.warnings == []

    def test_ambiguous_bare_tag_multiple_rfcs(self):
        """Bare [4] matching rfc9000:4.x and rfc9001:4.x is ambiguous."""
        keys = {"rfc9000:4.1", "rfc9001:4.2"}
        result = normalize_tag_with_diagnostics("4", keys)
        assert len(result.matched_ids) == 2
        assert len(result.warnings) == 1
        assert "ambiguous" in result.warnings[0].lower()

    def test_qualified_tag_no_ambiguity(self):
        keys = {"rfc9000:4.1", "rfc9001:4.2"}
        result = normalize_tag_with_diagnostics("rfc9000:4.1", keys)
        assert result.matched_ids == {"rfc9000:4.1"}
        assert result.warnings == []

    def test_no_match_no_warnings(self):
        result = normalize_tag_with_diagnostics("99", {"rfc9000:4.1"})
        assert result.matched_ids == set()
        assert result.warnings == []
