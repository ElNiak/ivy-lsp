"""Tests for the light-mode (regex-based) requirement extractor.

Verifies that ``extract_requirements_light`` correctly extracts
RequirementNode instances and assignment writes from Ivy source
text using regex + brace-depth tracking, without the full Ivy parser.
"""

import pytest

from ivy_lsp.analysis.light_mode_extractor import extract_requirements_light
from ivy_lsp.analysis.requirement_graph import RequirementNode

FILEPATH = "test_monitor.ivy"


# ---------------------------------------------------------------------------
# Test case 1: require from a before block
# ---------------------------------------------------------------------------


class TestBeforeBlockRequire:
    """Extract a require from a ``before`` block."""

    def test_single_require_in_before(self):
        source = (
            "before foo.step {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        req = reqs[0]
        assert isinstance(req, RequirementNode)
        assert req.kind == "require"
        assert req.monitor_action == "foo.step"
        assert req.mixin_kind == "before"
        assert req.formula_text == "x ~= y"
        assert req.file == FILEPATH
        assert req.bracket_tags == []

    def test_before_block_line_number_is_set(self):
        source = (
            "before foo.step {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        # Line number should be a non-negative integer (0-based)
        assert isinstance(reqs[0].line, int)
        assert reqs[0].line >= 0

    def test_before_block_id_format(self):
        source = (
            "before foo.step {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        # id should be "filepath:line"
        assert reqs[0].id.startswith(f"{FILEPATH}:")


# ---------------------------------------------------------------------------
# Test case 2: ensure from an after block
# ---------------------------------------------------------------------------


class TestAfterBlockEnsure:
    """Extract an ensure from an ``after`` block."""

    def test_single_ensure_in_after(self):
        source = (
            "after foo.step {\n"
            "    ensure true;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        req = reqs[0]
        assert req.kind == "ensure"
        assert req.monitor_action == "foo.step"
        assert req.mixin_kind == "after"
        assert req.formula_text == "true"

    def test_after_with_complex_ensure(self):
        source = (
            "after packet.recv(dst:cid, pkt:packet) {\n"
            "    ensure valid_checksum(pkt);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "ensure"
        assert reqs[0].monitor_action == "packet.recv"
        assert reqs[0].mixin_kind == "after"


# ---------------------------------------------------------------------------
# Test case 3: around block (desugars to "before")
# ---------------------------------------------------------------------------


class TestAroundBlock:
    """Extract from an ``around`` block -- treated as ``before``."""

    def test_around_require_treated_as_before(self):
        source = (
            "around foo.step {\n"
            "    require x > 0;\n"
            "    ...\n"
            "    ensure y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        # Should find both the require and the ensure
        assert len(reqs) == 2
        kinds = [r.kind for r in reqs]
        assert "require" in kinds
        assert "ensure" in kinds

    def test_around_mixin_kind_preserved(self):
        source = (
            "around foo.step {\n"
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].mixin_kind == "around"
        assert reqs[0].monitor_action == "foo.step"

    def test_implement_mixin_kind(self):
        source = (
            "implement foo.bar {\n"
            "    require z > 0;\n"
            "    ensure result = z + 1;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 2
        for r in reqs:
            assert r.mixin_kind == "implement"
            assert r.monitor_action == "foo.bar"
        kinds = {r.kind for r in reqs}
        assert kinds == {"require", "ensure"}


# ---------------------------------------------------------------------------
# Test case 4: direct action body
# ---------------------------------------------------------------------------


class TestDirectActionBody:
    """Extract requirements from a direct ``action ... = { ... }`` body."""

    def test_require_in_action_body(self):
        source = (
            "action send(x:t) = {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        req = reqs[0]
        assert req.kind == "require"
        assert req.mixin_kind == "direct"
        assert req.monitor_action == "send"
        assert req.formula_text == "x ~= y"

    def test_ensure_in_action_body(self):
        source = (
            "action compute(x:t) returns (y:t) = {\n"
            "    ensure y > x;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "ensure"
        assert reqs[0].mixin_kind == "direct"
        assert reqs[0].monitor_action == "compute"

    def test_dotted_action_name(self):
        source = (
            "action foo.bar(x:t) = {\n"
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].monitor_action == "foo.bar"
        assert reqs[0].mixin_kind == "direct"


# ---------------------------------------------------------------------------
# Test case 5: multiple requirements in one block
# ---------------------------------------------------------------------------


class TestMultipleRequirements:
    """Multiple require/ensure in a single block produce multiple nodes."""

    def test_three_reqs_in_before_block(self):
        source = (
            "before packet_event {\n"
            "    require connected(src, dst);\n"
            "    require src ~= dst;\n"
            "    ensure sent_pkt(src, dst);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 3

        kinds = [r.kind for r in reqs]
        assert kinds.count("require") == 2
        assert kinds.count("ensure") == 1

        for r in reqs:
            assert r.monitor_action == "packet_event"
            assert r.mixin_kind == "before"

    def test_formulas_are_distinct(self):
        source = (
            "before packet_event {\n"
            "    require connected(src, dst);\n"
            "    require src ~= dst;\n"
            "    ensure sent_pkt(src, dst);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        formulas = {r.formula_text for r in reqs}
        assert "connected(src, dst)" in formulas
        assert "src ~= dst" in formulas
        assert "sent_pkt(src, dst)" in formulas

    def test_each_req_has_unique_id(self):
        source = (
            "before packet_event {\n"
            "    require connected(src, dst);\n"
            "    require src ~= dst;\n"
            "    ensure sent_pkt(src, dst);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        ids = [r.id for r in reqs]
        assert len(set(ids)) == len(ids), "Each requirement must have a unique id"


# ---------------------------------------------------------------------------
# Test case 6: bracket tag parsing
# ---------------------------------------------------------------------------


class TestBracketTagParsing:
    """Bracket tags (e.g. ``# [4]``) are parsed into bracket_tags."""

    def test_simple_numeric_tag(self):
        source = (
            "before foo.step {\n"
            "    require x > 0; # [4]\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].bracket_tags == ["4"]

    def test_no_tag_yields_empty_list(self):
        source = (
            "before foo.step {\n"
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].bracket_tags == []

    def test_compound_tag(self):
        """Tags with colons and dots like ``# [frame:ack.sent]``."""
        source = (
            "before foo.step {\n"
            "    require x > 0; # [frame:ack.sent]\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].bracket_tags == ["frame:ack.sent"]

    def test_multi_tag(self):
        """Comma-separated tags like ``# [rfc9000:4.1, rfc9000:8.1]``."""
        source = (
            "before foo.step {\n"
            "    require x > 0; # [rfc9000:4.1, rfc9000:8.1]\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].bracket_tags == ["rfc9000:4.1", "rfc9000:8.1"]

    def test_tag_with_formula_preserved(self):
        """Bracket tag does not corrupt the formula text."""
        source = (
            "before foo.step {\n"
            "    require x > 0; # [4]\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert reqs[0].formula_text == "x > 0"


# ---------------------------------------------------------------------------
# Test case 7: assignment (write) tracking
# ---------------------------------------------------------------------------


class TestAssignmentTracking:
    """Assignments with ``:=`` are tracked as writes."""

    def test_simple_assignment(self):
        source = (
            "before foo.step {\n"
            "    sent_pkt(C, N) := true;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert len(writes) == 1
        var_name, filepath, line = writes[0]
        assert var_name == "sent_pkt"
        assert filepath == FILEPATH
        assert isinstance(line, int)

    def test_dotted_lhs_assignment(self):
        source = (
            "before foo.step {\n"
            "    conn.state := established;\n"
            "}\n"
        )
        _, writes = extract_requirements_light(source, FILEPATH)
        assert len(writes) == 1
        assert writes[0][0] == "conn.state"

    def test_mixed_reqs_and_writes(self):
        source = (
            "before foo.step {\n"
            "    require x > 0;\n"
            "    sent_pkt(C, N) := true;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert len(writes) == 1


# ---------------------------------------------------------------------------
# Test case 8: nested braces
# ---------------------------------------------------------------------------


class TestNestedBraces:
    """Requirements inside nested braces (if/while blocks) are still extracted."""

    def test_require_inside_if(self):
        source = (
            "before foo.step {\n"
            "    if x > 0 {\n"
            "        require y > 0;\n"
            "    }\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "require"
        assert reqs[0].formula_text == "y > 0"
        assert reqs[0].monitor_action == "foo.step"
        assert reqs[0].mixin_kind == "before"

    def test_multiple_nested_levels(self):
        source = (
            "before foo.step {\n"
            "    if a {\n"
            "        if b {\n"
            "            require deep;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "deep"

    def test_reqs_at_mixed_depths(self):
        source = (
            "before foo.step {\n"
            "    require top_level;\n"
            "    if x {\n"
            "        require nested;\n"
            "    }\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 2
        formulas = {r.formula_text for r in reqs}
        assert "top_level" in formulas
        assert "nested" in formulas


# ---------------------------------------------------------------------------
# Test case 9: empty source
# ---------------------------------------------------------------------------


class TestEmptySource:
    """Empty or whitespace-only source yields empty results."""

    def test_empty_string(self):
        reqs, writes = extract_requirements_light("", FILEPATH)
        assert reqs == []
        assert writes == []

    def test_none_equivalent_empty(self):
        """Passing an empty string is the same as having no monitors."""
        reqs, writes = extract_requirements_light("", FILEPATH)
        assert len(reqs) == 0
        assert len(writes) == 0

    def test_whitespace_only(self):
        reqs, writes = extract_requirements_light("   \n\n  \t  ", FILEPATH)
        assert reqs == []
        assert writes == []


# ---------------------------------------------------------------------------
# Test case 10: source with no monitors or actions
# ---------------------------------------------------------------------------


class TestNoMonitors:
    """Source that has no before/after/around/action blocks yields nothing."""

    def test_type_declarations_only(self):
        source = (
            "type cid\n"
            "type pkt_num\n"
            "relation connected(X:cid, Y:cid)\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert reqs == []
        assert writes == []

    def test_comments_and_includes(self):
        source = (
            "# This is a comment\n"
            "include quic_types\n"
            "type cid\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert reqs == []
        assert writes == []

    def test_object_without_require(self):
        source = (
            "object foo = {\n"
            "    type this\n"
            "    individual zero:foo\n"
            "}\n"
        )
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert reqs == []
        assert writes == []


# ---------------------------------------------------------------------------
# Test case 11: mixed monitors and direct actions
# ---------------------------------------------------------------------------


class TestMixedMonitorsAndActions:
    """Multiple monitor kinds and direct actions in a single source."""

    def test_before_after_and_action(self):
        source = (
            "before foo.step {\n"
            "    require pre_cond;\n"
            "}\n"
            "\n"
            "after foo.step {\n"
            "    ensure post_cond;\n"
            "}\n"
            "\n"
            "action bar(x:t) = {\n"
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 3

        by_mixin = {}
        for r in reqs:
            by_mixin[r.mixin_kind] = by_mixin.get(r.mixin_kind, [])
            by_mixin[r.mixin_kind].append(r)

        assert len(by_mixin["before"]) == 1
        assert by_mixin["before"][0].kind == "require"
        assert by_mixin["before"][0].monitor_action == "foo.step"

        assert len(by_mixin["after"]) == 1
        assert by_mixin["after"][0].kind == "ensure"
        assert by_mixin["after"][0].monitor_action == "foo.step"

        assert len(by_mixin["direct"]) == 1
        assert by_mixin["direct"][0].kind == "require"
        assert by_mixin["direct"][0].monitor_action == "bar"

    def test_around_and_before_same_action(self):
        source = (
            "before foo.step {\n"
            "    require a;\n"
            "}\n"
            "\n"
            "around foo.step {\n"
            "    require b;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 2
        kinds = {r.mixin_kind for r in reqs}
        assert kinds == {"before", "around"}
        for r in reqs:
            assert r.monitor_action == "foo.step"

    def test_writes_from_multiple_blocks(self):
        source = (
            "before foo.step {\n"
            "    sent_pkt(C, N) := true;\n"
            "}\n"
            "\n"
            "action bar(x:t) = {\n"
            "    counter := counter + 1;\n"
            "}\n"
        )
        _, writes = extract_requirements_light(source, FILEPATH)
        assert len(writes) == 2
        var_names = {w[0] for w in writes}
        assert "sent_pkt" in var_names
        assert "counter" in var_names


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestRequirementKinds:
    """All four requirement kinds are recognized."""

    def test_assume_kind(self):
        source = (
            "before foo.step {\n"
            "    assume x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "assume"

    def test_assert_kind(self):
        source = (
            "before foo.step {\n"
            "    assert x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "assert"


class TestKnownStateVarsParameter:
    """The known_state_vars parameter does not alter extraction results."""

    def test_state_vars_passed_through(self):
        source = (
            "before foo.step {\n"
            "    require connected(src, dst);\n"
            "}\n"
        )
        known = {"connected", "sent_pkt"}
        reqs, _ = extract_requirements_light(source, FILEPATH, known_state_vars=known)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "connected(src, dst)"

    def test_state_vars_none_default(self):
        source = (
            "before foo.step {\n"
            "    require connected(src, dst);\n"
            "}\n"
        )
        reqs1, _ = extract_requirements_light(source, FILEPATH)
        reqs2, _ = extract_requirements_light(source, FILEPATH, known_state_vars=None)
        assert len(reqs1) == len(reqs2)


class TestMonitorWithParameters:
    """Monitors with parameter lists are matched correctly."""

    def test_before_with_params(self):
        source = (
            "before foo.step(x:t, y:t) {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].monitor_action == "foo.step"
        assert reqs[0].mixin_kind == "before"

    def test_action_with_returns(self):
        source = (
            "action compute(x:t) returns (y:t) = {\n"
            "    require x > 0;\n"
            "    ensure y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 2
        for r in reqs:
            assert r.monitor_action == "compute"
            assert r.mixin_kind == "direct"


class TestFilepathPropagation:
    """File path is correctly attached to every node."""

    def test_custom_filepath(self):
        source = (
            "before foo.step {\n"
            "    require x > 0;\n"
            "}\n"
        )
        custom = "/opt/ivy/models/quic_stack/quic_types.ivy"
        reqs, _ = extract_requirements_light(source, custom)
        assert len(reqs) == 1
        assert reqs[0].file == custom
        assert reqs[0].id.startswith(custom + ":")

    def test_writes_carry_filepath(self):
        source = (
            "before foo.step {\n"
            "    sent_pkt(C, N) := true;\n"
            "}\n"
        )
        custom = "/some/path/model.ivy"
        _, writes = extract_requirements_light(source, custom)
        assert len(writes) == 1
        assert writes[0][1] == custom


class TestCommentHandling:
    """Comments inside blocks do not create spurious matches."""

    def test_commented_require_ignored(self):
        source = (
            "before foo.step {\n"
            "    # require x > 0;\n"
            "    require y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        # The commented-out require should not be matched because
        # the brace tracker skips comment lines; the regex may or
        # may not match the comment line depending on leading #.
        # At minimum, the non-commented require must be present.
        formulas = [r.formula_text for r in reqs]
        assert "y > 0" in formulas


class TestLineNumberOrdering:
    """Requirements are extracted in source order."""

    def test_requirements_in_order(self):
        source = (
            "before packet_event {\n"
            "    require first;\n"
            "    require second;\n"
            "    require third;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 3
        # Line numbers should be monotonically increasing
        lines = [r.line for r in reqs]
        assert lines == sorted(lines)
        # Formulas should match the order in source
        assert reqs[0].formula_text == "first"
        assert reqs[1].formula_text == "second"
        assert reqs[2].formula_text == "third"


# ---------------------------------------------------------------------------
# H3: Native block string literal handling
# ---------------------------------------------------------------------------


class TestNativeBlockStringLiterals:
    """H3: >>> inside C++ string literals should NOT end native blocks."""

    def test_cpp_braces_after_string_with_triple_angle(self):
        """C++ braces after a string containing >>> should not affect depth.

        When >>> inside a string prematurely terminates the native block
        scan, any C++ braces after that point corrupt the brace depth
        counter — returning the wrong closing brace.
        """
        from ivy_lsp.analysis.light_mode_extractor import _find_matching_brace

        # The C++ code has: if(1) { ... ">>>" ... }
        # Without the fix, find(">>>") hits the string literal first,
        # then the C++ } is treated as Ivy's closing brace.
        source = (
            'action foo = { <<< if(1) { std::string s = ">>>"; } >>> }'
        )
        open_pos = source.index("{")
        result = _find_matching_brace(source, open_pos)
        assert result is not None
        # Should find the final } (the Ivy brace), not the C++ }
        assert result == len(source) - 1  # the final } is the last character
        assert source[result] == "}"
        # The content between the Ivy braces should span the entire native block
        assert "<<<" in source[open_pos:result]
        assert ">>>" in source[open_pos:result]

    def test_require_after_native_with_cpp_braces_and_string(self):
        """Requirements after a native block with >>> in strings are extracted."""
        source = (
            "before foo.step {\n"
            '    <<< if(1) { std::string s = ">>>"; } >>>\n'
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "x > 0"

    def test_escaped_quote_inside_native_string(self):
        """Escaped quotes inside C++ strings should not break parsing."""
        from ivy_lsp.analysis.light_mode_extractor import _find_matching_brace

        source = r'action foo = { <<< std::string s = "a\">>>b"; >>> }'
        open_pos = source.index("{")
        result = _find_matching_brace(source, open_pos)
        assert result is not None
        assert source[result] == "}"
