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
