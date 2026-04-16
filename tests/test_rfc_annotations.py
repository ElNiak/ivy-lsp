"""Tests for RFC annotation parsing and manifest loading."""

import os
import tempfile

from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
from ivy_lsp.core.semantic.rfc_annotations import (
    CoverageStats,
    compute_coverage,
    find_manifests,
    load_requirement_manifest,
    normalize_tag_to_manifest_ids,
    parse_file_rfc_annotations,
    parse_rfc_tags,
)
from ivy_lsp.mcp.tools._helpers import infer_protocol_from_path


class TestParseRfcTags:
    def test_single_numeric_tag(self):
        assert parse_rfc_tags("    require x > 0; # [4]") == ["4"]

    def test_single_rfc_tag(self):
        assert parse_rfc_tags("    require x > 0; # [rfc9000:4.1]") == ["rfc9000:4.1"]

    def test_multi_tag(self):
        result = parse_rfc_tags("    require x > 0; # [rfc9000:4.1, rfc9000:8.1]")
        assert result == ["rfc9000:4.1", "rfc9000:8.1"]

    def test_no_tag(self):
        assert parse_rfc_tags("    require x > 0;") == []

    def test_empty_line(self):
        assert parse_rfc_tags("") == []

    def test_tag_with_trailing_whitespace(self):
        assert parse_rfc_tags("    require x > 0; # [7]   ") == ["7"]

    def test_compound_dotted_tag(self):
        assert parse_rfc_tags("    require x > 0; # [frame:ack.sent]") == [
            "frame:ack.sent"
        ]

    def test_invalid_tag_ignored(self):
        # Tags with special chars fail the outer bracket regex entirely
        # since @ is not in [\w:.,\s]
        result = parse_rfc_tags("    require x > 0; # [valid, inv@lid]")
        assert result == []  # outer regex rejects entire bracket group

    def test_mixed_valid_and_empty_tags(self):
        # Empty entries from extra commas are filtered out
        result = parse_rfc_tags("    require x > 0; # [a, , b]")
        assert result == ["a", "b"]

    def test_three_tags(self):
        result = parse_rfc_tags("    require x; # [a, b, c]")
        assert result == ["a", "b", "c"]

    # -- C2: commented-out code filtering --

    def test_commented_out_code_skipped(self):
        """C2: Tags on commented-out code lines must not be extracted."""
        assert parse_rfc_tags("#require foo # [11]") == []

    def test_commented_out_code_with_leading_space(self):
        assert parse_rfc_tags("    #require foo # [8]") == []

    def test_pure_tag_comment_parsed(self):
        """C2: A standalone tag comment like '# [8]' should still be parsed."""
        assert parse_rfc_tags("# [8]") == ["8"]

    def test_pure_tag_comment_with_leading_space(self):
        assert parse_rfc_tags("    # [rfc9000:4.1]") == ["rfc9000:4.1"]

    def test_pure_tag_comment_multi(self):
        assert parse_rfc_tags("  # [a, b]") == ["a", "b"]

    def test_struct_field_tag_still_parsed(self):
        """C2: Tags on struct fields are parsed (filtering happens elsewhere)."""
        assert parse_rfc_tags("payload : frame.arr # [8]") == ["8"]

    def test_live_code_tag_still_parsed(self):
        assert parse_rfc_tags("require foo # [8]") == ["8"]

    # -- Descriptive comment annotations --

    def test_descriptive_comment_with_qualified_tag(self):
        """Descriptive comments like '# Description [rfc4271:8]' are parsed."""
        assert parse_rfc_tags("# BGP Connection FSM [rfc4271:8]") == ["rfc4271:8"]

    def test_descriptive_comment_with_subsection_tag(self):
        assert parse_rfc_tags("# Per-speaker state [rfc4271:8.2.1]") == [
            "rfc4271:8.2.1"
        ]

    def test_descriptive_comment_with_multi_tags(self):
        result = parse_rfc_tags("# Error handling [rfc4271:6.1, rfc4271:6.2]")
        assert result == ["rfc4271:6.1", "rfc4271:6.2"]

    def test_descriptive_comment_bare_numeric_still_extracted(self):
        """parse_rfc_tags extracts bare numerics; _is_rfc_annotation filters them."""
        assert parse_rfc_tags("# Some description [8]") == ["8"]

    def test_markdown_heading_with_tag_rejected(self):
        """## headings have a second '#' which triggers the commented-out code guard."""
        assert parse_rfc_tags("## Section [rfc4271:8]") == []


class TestParseFileRfcAnnotations:
    def test_single_annotation(self):
        source = "require x > 0; # [rfc9000:4.1]\nrequire y > 0;"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 1
        assert anns[0].file == "test.ivy"
        assert anns[0].line == 0
        assert anns[0].tags == ["rfc9000:4.1"]

    def test_multi_annotations(self):
        source = "require x; # [a]\ncode\nrequire y; # [b, c]"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 2
        assert anns[0].tags == ["a"]
        assert anns[1].tags == ["b", "c"]

    def test_descriptive_comment_annotations(self):
        """Descriptive comment annotations are parsed into RfcAnnotation nodes."""
        source = (
            "# BGP Connection FSM [rfc4271:8]\n"
            "# Per-speaker state [rfc4271:8.2.1]\n"
            "action connect = {\n"
            "    require connected # [rfc4271:4.2]\n"
            "}\n"
        )
        anns = parse_file_rfc_annotations(source, "bgp.ivy")
        assert len(anns) == 3
        assert anns[0].tags == ["rfc4271:8"]
        assert anns[1].tags == ["rfc4271:8.2.1"]
        assert anns[2].tags == ["rfc4271:4.2"]

    def test_no_annotations(self):
        source = "require x > 0;\nrequire y > 0;"
        anns = parse_file_rfc_annotations(source, "test.ivy")
        assert len(anns) == 0


class TestInferProtocolFromPath:
    def test_protocol_testing_prefix(self):
        assert (
            infer_protocol_from_path("protocol-testing/bgp/bgp_stack/foo.ivy") == "bgp"
        )

    def test_protocol_testing_quic(self):
        assert (
            infer_protocol_from_path("protocol-testing/quic/quic_stack/bar.ivy")
            == "quic"
        )

    def test_relative_known_protocol(self):
        assert infer_protocol_from_path("bgp/bgp_tests/speaker_tests/test.ivy") == "bgp"

    def test_unknown_first_component_returns_none(self):
        assert infer_protocol_from_path("tests/unit/foo.ivy") is None

    def test_empty_path_returns_none(self):
        assert infer_protocol_from_path("") is None

    def test_dot_prefixed_returns_none(self):
        assert infer_protocol_from_path(".hidden/something") is None


class TestLoadRequirementManifest:
    def test_load_valid_manifest(self):
        try:
            import yaml
        except ImportError:
            return  # skip if PyYAML not installed

        content = """\
rfc: "RFC9000"
requirements:
  rfc9000:4.1:
    text: "senders MUST NOT send data"
    section: "4.1"
    level: MUST
    layer: frame
    testable: true
  rfc9000:8.1:
    text: "SHOULD validate tokens"
    section: "8.1"
    level: SHOULD
    layer: connection
    testable: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            reqs = load_requirement_manifest(path)
            assert len(reqs) == 2
            assert "rfc9000:4.1" in reqs
            assert reqs["rfc9000:4.1"].level == "MUST"
            assert reqs["rfc9000:4.1"].rfc == "RFC9000"
            assert reqs["rfc9000:8.1"].level == "SHOULD"
        finally:
            os.unlink(path)

    def test_load_nonexistent_returns_empty(self):
        reqs = load_requirement_manifest("/nonexistent/path.yaml")
        assert reqs == {}


class TestFindManifests:
    def test_finds_manifests_in_protocol_testing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pt = os.path.join(tmpdir, "protocol-testing", "quic")
            os.makedirs(pt)
            manifest = os.path.join(pt, "quic_requirements.yaml")
            with open(manifest, "w") as f:
                f.write("rfc: test\n")
            results = find_manifests(tmpdir)
            assert len(results) == 1
            assert results[0] == manifest

    def test_no_protocol_testing_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert find_manifests(tmpdir) == []

    def test_finds_manifests_in_protocol_dir_directly(self):
        """IndexBuilder passes protocol_dir (e.g. protocol-testing/quic).

        Which has no protocol-testing/ child.  find_manifests should fall
        back to searching the root itself.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = os.path.join(tmpdir, "rfc9000_requirements.yaml")
            with open(manifest, "w") as f:
                f.write("rfc: RFC9000\nrequirements: {}\n")
            results = find_manifests(tmpdir)
            assert len(results) == 1
            assert results[0] == manifest


class TestComputeCoverage:
    def test_full_coverage(self):
        reqs = {
            "rfc9000:4.1": RfcRequirement(
                id="rfc9000:4.1",
                rfc="RFC9000",
                section="4.1",
                text="must",
                level="MUST",
                layer="frame",
            ),
        }
        anns = [RfcAnnotation(id="a:0:0", file="a.ivy", line=0, tags=["rfc9000:4.1"])]
        stats = compute_coverage(anns, reqs)
        assert stats.total == 1
        assert stats.covered == 1
        assert stats.uncovered == 0

    def test_partial_coverage(self):
        reqs = {
            "a": RfcRequirement(id="a", rfc="X", section="1", text="t", level="MUST"),
            "b": RfcRequirement(id="b", rfc="X", section="2", text="t", level="SHOULD"),
        }
        anns = [RfcAnnotation(id="x:0:0", file="x.ivy", line=0, tags=["a"])]
        stats = compute_coverage(anns, reqs)
        assert stats.total == 2
        assert stats.covered == 1
        assert stats.uncovered == 1
        assert stats.by_level["MUST"]["covered"] == 1
        assert stats.by_level["SHOULD"]["covered"] == 0

    def test_empty_coverage(self):
        stats = compute_coverage([], {})
        assert stats.total == 0
        assert stats.covered == 0

    def test_bare_tag_matches_manifest_via_normalization(self):
        """C4: bare [4] in source should match rfc9000:4.1 in manifest."""
        reqs = {
            "rfc9000:4.1": RfcRequirement(
                id="rfc9000:4.1",
                rfc="RFC9000",
                section="4.1",
                text="test",
                level="MUST",
                layer="frame",
            ),
        }
        anns = [RfcAnnotation(id="t:10:0", file="t.ivy", line=10, tags=["4"])]
        stats = compute_coverage(anns, reqs)
        assert stats.covered == 1
        assert stats.total == 1

    def test_bare_section_tag_matches(self):
        """C4: bare [4.1] should match rfc9000:4.1."""
        reqs = {
            "rfc9000:4.1": RfcRequirement(
                id="rfc9000:4.1",
                rfc="RFC9000",
                section="4.1",
                text="test",
                level="MUST",
                layer="",
            ),
            "rfc9000:4.6": RfcRequirement(
                id="rfc9000:4.6",
                rfc="RFC9000",
                section="4.6",
                text="test2",
                level="SHOULD",
                layer="",
            ),
        }
        anns = [RfcAnnotation(id="t:5:0", file="t.ivy", line=5, tags=["4.1"])]
        stats = compute_coverage(anns, reqs)
        assert stats.covered == 1  # only 4.1, not 4.6


class TestNormalizeTagToManifestIds:
    def test_exact_match(self):
        keys = {"rfc9000:4.1", "rfc9000:8.1"}
        assert normalize_tag_to_manifest_ids("rfc9000:4.1", keys) == {"rfc9000:4.1"}

    def test_bare_numeric_matches_prefix(self):
        keys = {"rfc9000:4.1", "rfc9000:4.6", "rfc9000:8.1"}
        result = normalize_tag_to_manifest_ids("4", keys)
        assert result == {"rfc9000:4.1", "rfc9000:4.6"}

    def test_bare_section_matches_exact(self):
        keys = {"rfc9000:4.1", "rfc9000:4.6"}
        result = normalize_tag_to_manifest_ids("4.1", keys)
        assert result == {"rfc9000:4.1"}

    def test_no_match_returns_empty(self):
        keys = {"rfc9000:4.1"}
        assert normalize_tag_to_manifest_ids("99", keys) == set()

    def test_qualified_prefix_match(self):
        keys = {"rfc9000:4.1", "rfc9000:4.1.1"}
        result = normalize_tag_to_manifest_ids("rfc9000:4", keys)
        assert result == {"rfc9000:4.1", "rfc9000:4.1.1"}
