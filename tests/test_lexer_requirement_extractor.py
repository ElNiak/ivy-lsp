"""Tests for lexer-based requirement extraction.

Mirrors test_light_mode_extractor.py test cases to verify parity.
"""

import pytest

from ivy_lsp.analysis.lexer_requirement_extractor import extract_requirements_lexer
from ivy_lsp.analysis.requirement_graph import RequirementNode

FILEPATH = "test_monitor.ivy"


class TestBeforeBlockRequireLexer:
    """Parity: TestBeforeBlockRequire from test_light_mode_extractor."""

    def test_single_require_in_before(self):
        source = (
            "before foo.step {\n"
            "    require x ~= y;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_lexer(source, FILEPATH)
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
        source = "before foo.step {\n    require x ~= y;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert isinstance(reqs[0].line, int)
        assert reqs[0].line >= 0

    def test_before_block_id_format(self):
        source = "before foo.step {\n    require x ~= y;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].id.startswith(f"{FILEPATH}:")


class TestAfterBlockEnsureLexer:
    """Parity: TestAfterBlockEnsure."""

    def test_single_ensure_in_after(self):
        source = "after foo.step {\n    ensure true;\n}\n"
        reqs, writes = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "ensure"
        assert reqs[0].monitor_action == "foo.step"
        assert reqs[0].mixin_kind == "after"
        assert reqs[0].formula_text == "true"

    def test_after_with_complex_ensure(self):
        source = (
            "after packet.recv(dst:cid, pkt:packet) {\n"
            "    ensure valid_checksum(pkt);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].monitor_action == "packet.recv"
        assert reqs[0].mixin_kind == "after"


class TestAroundBlockLexer:
    """Parity: TestAroundBlock."""

    def test_around_require_and_ensure(self):
        source = (
            "around foo.step {\n"
            "    require x > 0;\n"
            "    ...\n"
            "    ensure y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 2
        kinds = [r.kind for r in reqs]
        assert "require" in kinds
        assert "ensure" in kinds

    def test_around_mixin_kind_preserved(self):
        source = "around foo.step {\n    require x > 0;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].mixin_kind == "around"
        assert reqs[0].monitor_action == "foo.step"

    def test_implement_mixin_kind(self):
        source = (
            "implement foo.bar {\n"
            "    require z > 0;\n"
            "    ensure result = z + 1;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 2
        for r in reqs:
            assert r.mixin_kind == "implement"
            assert r.monitor_action == "foo.bar"
        kinds = {r.kind for r in reqs}
        assert kinds == {"require", "ensure"}


class TestDirectActionBodyLexer:
    """Parity: TestDirectActionBody."""

    def test_require_in_action_body(self):
        source = "action send(x:t) = {\n    require x ~= y;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "require"
        assert reqs[0].mixin_kind == "direct"
        assert reqs[0].monitor_action == "send"
        assert reqs[0].formula_text == "x ~= y"

    def test_ensure_in_action_body(self):
        source = (
            "action compute(x:t) returns (y:t) = {\n"
            "    ensure y > x;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "ensure"
        assert reqs[0].mixin_kind == "direct"
        assert reqs[0].monitor_action == "compute"

    def test_dotted_action_name(self):
        source = "action foo.bar(x:t) = {\n    require x > 0;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].monitor_action == "foo.bar"
        assert reqs[0].mixin_kind == "direct"

    def test_action_with_returns(self):
        source = (
            "action compute(x:t) returns (y:t) = {\n"
            "    require x > 0;\n"
            "    ensure y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 2
        for r in reqs:
            assert r.monitor_action == "compute"
            assert r.mixin_kind == "direct"


class TestMultipleRequirementsLexer:
    def test_three_reqs_in_before_block(self):
        source = (
            "before packet_event {\n"
            "    require connected(src, dst);\n"
            "    require src ~= dst;\n"
            "    ensure sent_pkt(src, dst);\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 3
        assert [r.kind for r in reqs].count("require") == 2
        assert [r.kind for r in reqs].count("ensure") == 1
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
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
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
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        ids = [r.id for r in reqs]
        assert len(set(ids)) == len(ids)


class TestBracketTagParsingLexer:
    def test_simple_numeric_tag(self):
        source = "before foo.step {\n    require x > 0; # [4]\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].bracket_tags == ["4"]

    def test_no_tag_yields_empty_list(self):
        source = "before foo.step {\n    require x > 0;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].bracket_tags == []

    def test_compound_tag(self):
        source = "before foo.step {\n    require x > 0; # [frame:ack.sent]\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].bracket_tags == ["frame:ack.sent"]

    def test_multi_tag(self):
        source = "before foo.step {\n    require x > 0; # [rfc9000:4.1, rfc9000:8.1]\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].bracket_tags == ["rfc9000:4.1", "rfc9000:8.1"]

    def test_tag_with_formula_preserved(self):
        source = "before foo.step {\n    require x > 0; # [4]\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].formula_text == "x > 0"


class TestAssignmentTrackingLexer:
    def test_simple_assignment(self):
        source = "before foo.step {\n    sent_pkt(C, N) := true;\n}\n"
        _, writes = extract_requirements_lexer(source, FILEPATH)
        assert len(writes) == 1
        assert writes[0][0] == "sent_pkt"
        assert writes[0][1] == FILEPATH

    def test_dotted_lhs_assignment(self):
        source = "before foo.step {\n    conn.state := established;\n}\n"
        _, writes = extract_requirements_lexer(source, FILEPATH)
        assert len(writes) == 1
        assert writes[0][0] == "conn.state"

    def test_mixed_reqs_and_writes(self):
        source = (
            "before foo.step {\n"
            "    require x > 0;\n"
            "    sent_pkt(C, N) := true;\n"
            "}\n"
        )
        reqs, writes = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert len(writes) == 1


class TestNestedBracesLexer:
    def test_require_inside_if(self):
        source = (
            "before foo.step {\n"
            "    if x > 0 {\n"
            "        require y > 0;\n"
            "    }\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "y > 0"
        assert reqs[0].monitor_action == "foo.step"

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
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
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
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 2
        formulas = {r.formula_text for r in reqs}
        assert "top_level" in formulas
        assert "nested" in formulas


class TestEmptySourceLexer:
    def test_empty_string(self):
        assert extract_requirements_lexer("", FILEPATH) == ([], [])

    def test_whitespace_only(self):
        assert extract_requirements_lexer("   \n\n  \t  ", FILEPATH) == ([], [])


class TestNoMonitorsLexer:
    def test_type_declarations_only(self):
        source = "type cid\ntype pkt_num\nrelation connected(X:cid, Y:cid)\n"
        assert extract_requirements_lexer(source, FILEPATH) == ([], [])

    def test_comments_and_includes(self):
        source = "# This is a comment\ninclude quic_types\ntype cid\n"
        assert extract_requirements_lexer(source, FILEPATH) == ([], [])

    def test_object_without_require(self):
        source = "object foo = {\n    type this\n    individual zero:foo\n}\n"
        assert extract_requirements_lexer(source, FILEPATH) == ([], [])


class TestMixedMonitorsAndActionsLexer:
    def test_before_after_and_action(self):
        source = (
            "before foo.step {\n    require pre_cond;\n}\n\n"
            "after foo.step {\n    ensure post_cond;\n}\n\n"
            "action bar(x:t) = {\n    require x > 0;\n}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 3
        by_mixin = {}
        for r in reqs:
            by_mixin.setdefault(r.mixin_kind, []).append(r)
        assert len(by_mixin["before"]) == 1
        assert len(by_mixin["after"]) == 1
        assert len(by_mixin["direct"]) == 1

    def test_writes_from_multiple_blocks(self):
        source = (
            "before foo.step {\n    sent_pkt(C, N) := true;\n}\n\n"
            "action bar(x:t) = {\n    counter := counter + 1;\n}\n"
        )
        _, writes = extract_requirements_lexer(source, FILEPATH)
        assert len(writes) == 2
        var_names = {w[0] for w in writes}
        assert "sent_pkt" in var_names
        assert "counter" in var_names


class TestRequirementKindsLexer:
    def test_assume_kind(self):
        source = "before foo.step {\n    assume x > 0;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].kind == "assume"

    def test_assert_kind(self):
        source = "before foo.step {\n    assert x > 0;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert reqs[0].kind == "assert"


class TestMonitorWithParametersLexer:
    def test_before_with_params(self):
        source = "before foo.step(x:t, y:t) {\n    require x ~= y;\n}\n"
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].monitor_action == "foo.step"

    def test_action_with_returns(self):
        source = (
            "action compute(x:t) returns (y:t) = {\n"
            "    require x > 0;\n"
            "    ensure y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 2
        for r in reqs:
            assert r.monitor_action == "compute"
            assert r.mixin_kind == "direct"


class TestFilepathPropagationLexer:
    def test_custom_filepath(self):
        source = "before foo.step {\n    require x > 0;\n}\n"
        custom = "/opt/ivy/models/quic_stack/quic_types.ivy"
        reqs, _ = extract_requirements_lexer(source, custom)
        assert reqs[0].file == custom
        assert reqs[0].id.startswith(custom + ":")

    def test_writes_carry_filepath(self):
        source = "before foo.step {\n    sent_pkt(C, N) := true;\n}\n"
        custom = "/some/path/model.ivy"
        _, writes = extract_requirements_lexer(source, custom)
        assert writes[0][1] == custom


class TestLineNumberOrderingLexer:
    def test_requirements_in_order(self):
        source = (
            "before packet_event {\n"
            "    require first;\n"
            "    require second;\n"
            "    require third;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 3
        lines = [r.line for r in reqs]
        assert lines == sorted(lines)
        assert reqs[0].formula_text == "first"
        assert reqs[1].formula_text == "second"
        assert reqs[2].formula_text == "third"


class TestNativeBlockLexer:
    def test_require_after_native_block(self):
        # Note: C++ strings containing ">>>" inside native blocks break the
        # PLY lexer (known limitation).  Use a simpler native block here.
        source = (
            "before foo.step {\n"
            "    <<< std::cout << 42; >>>\n"
            "    require x > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "x > 0"

    def test_braces_inside_native_block_ignored(self):
        source = (
            "before foo.step {\n"
            "    <<< if(1) { int x = 0; } >>>\n"
            "    require y > 0;\n"
            "}\n"
        )
        reqs, _ = extract_requirements_lexer(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].formula_text == "y > 0"


class TestExportsImportsLexer:
    def test_export_extraction(self):
        from ivy_lsp.analysis.lexer_requirement_extractor import (
            extract_exports_imports_lexer,
        )
        source = "export foo\nexport bar.baz\n"
        info = extract_exports_imports_lexer(source, FILEPATH)
        assert "foo" in info.exports
        assert "bar.baz" in info.exports

    def test_import_extraction(self):
        from ivy_lsp.analysis.lexer_requirement_extractor import (
            extract_exports_imports_lexer,
        )
        source = "import recv\nimport packet.send\n"
        info = extract_exports_imports_lexer(source, FILEPATH)
        assert "recv" in info.imports
        assert "packet.send" in info.imports

    def test_empty_source(self):
        from ivy_lsp.analysis.lexer_requirement_extractor import (
            extract_exports_imports_lexer,
        )
        info = extract_exports_imports_lexer("", FILEPATH)
        assert info.exports == []
        assert info.imports == []
