"""Tests for ivy_lsp.analysis.requirement_extractor module.

Tests the full AST-based requirement extraction and the helper functions
that do not depend on the ivy package.
"""

import re

import pytest

# ---------------------------------------------------------------------------
# Conditional import: ivy may not be available (requires Z3)
# ---------------------------------------------------------------------------

try:
    import ivy  # noqa: F401

    HAS_IVY = True
except ImportError:
    HAS_IVY = False

requires_ivy = pytest.mark.skipif(not HAS_IVY, reason="ivy not installed (requires Z3)")


# ===========================================================================
# Helper function tests (no ivy dependency)
# ===========================================================================


class TestExtractBracketTag:
    """Test _extract_bracket_tag with various comment patterns."""

    def test_numeric_tag(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0; # [4]"]
        assert _extract_bracket_tag(lines, 0) == "4"

    def test_rfc_tag(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0; # [rfc:4.1]"]
        assert _extract_bracket_tag(lines, 0) == "rfc:4.1"

    def test_no_tag(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0;"]
        assert _extract_bracket_tag(lines, 0) is None

    def test_line_out_of_range_negative(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0; # [4]"]
        assert _extract_bracket_tag(lines, -1) is None

    def test_line_out_of_range_beyond(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0; # [4]"]
        assert _extract_bracket_tag(lines, 5) is None

    def test_empty_lines(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        assert _extract_bracket_tag([], 0) is None

    def test_tag_with_trailing_whitespace(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = ["    require x > 0; # [7]   "]
        assert _extract_bracket_tag(lines, 0) == "7"

    def test_multiple_lines_correct_index(self):
        from ivy_lsp.analysis.requirement_extractor import _extract_bracket_tag

        lines = [
            "    require a;",
            "    require b; # [10]",
            "    require c; # [rfc:9.2]",
        ]
        assert _extract_bracket_tag(lines, 0) is None
        assert _extract_bracket_tag(lines, 1) == "10"
        assert _extract_bracket_tag(lines, 2) == "rfc:9.2"


class TestMixinNameRegex:
    """Test the _MIXIN_NAME_RE regex pattern."""

    def test_before_match(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("foo[before1]")
        assert m is not None
        assert m.group(1) == "foo"
        assert m.group(2) == "before"
        assert m.group(3) == "1"

    def test_after_match(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("bar[after2]")
        assert m is not None
        assert m.group(1) == "bar"
        assert m.group(2) == "after"
        assert m.group(3) == "2"

    def test_plain_name_no_match(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("plain_name")
        assert m is None

    def test_dotted_name_before(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("foo.step[before1]")
        assert m is not None
        assert m.group(1) == "foo.step"
        assert m.group(2) == "before"
        assert m.group(3) == "1"

    def test_multi_digit_index(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("action[after12]")
        assert m is not None
        assert m.group(1) == "action"
        assert m.group(2) == "after"
        assert m.group(3) == "12"

    def test_no_brackets(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("action_before1")
        assert m is None

    def test_empty_string(self):
        from ivy_lsp.analysis.requirement_extractor import _MIXIN_NAME_RE

        m = _MIXIN_NAME_RE.match("")
        assert m is None


# ===========================================================================
# Full extractor tests (require ivy + Z3)
# ===========================================================================


@requires_ivy
class TestExtractRequirementsFullBeforeBlock:
    """Parse source with `before foo.step { require x ~= y; }` and extract."""

    def test_require_in_before_block(self, ivy_source_mixin):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        wrapper = IvyParserWrapper()
        result = wrapper.parse(ivy_source_mixin, "mixin_test.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "mixin_test.ivy", ivy_source_mixin
        )

        # The mixin source contains: before foo.step { require x ~= x; }
        require_reqs = [r for r in reqs if r.kind == "require"]
        assert len(require_reqs) >= 1, (
            f"Expected at least one require, got {len(require_reqs)} "
            f"from {len(reqs)} total requirements"
        )

        req = require_reqs[0]
        assert req.kind == "require"
        assert req.file == "mixin_test.ivy"
        assert req.mixin_kind == "before"
        assert "foo.step" in req.monitor_action or "foo" in req.monitor_action


@requires_ivy
class TestExtractRequirementsFullAfterBlock:
    """Parse source with `after foo.step { ensure true; }` and extract."""

    def test_ensure_in_after_block(self, ivy_source_mixin):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        wrapper = IvyParserWrapper()
        result = wrapper.parse(ivy_source_mixin, "mixin_test.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "mixin_test.ivy", ivy_source_mixin
        )

        ensure_reqs = [r for r in reqs if r.kind == "ensure"]
        assert len(ensure_reqs) >= 1, (
            f"Expected at least one ensure, got {len(ensure_reqs)} "
            f"from {len(reqs)} total requirements"
        )

        ens = ensure_reqs[0]
        assert ens.kind == "ensure"
        assert ens.file == "mixin_test.ivy"
        assert ens.mixin_kind == "after"


@requires_ivy
class TestExtractRequirementsFullMultiple:
    """Parse source with multiple requirements in one before block."""

    def test_multiple_requires_in_before(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = """\
#lang ivy1.7

type t

object foo = {
    action step(x:t, y:t)
}

before foo.step {
    require x ~= y;
    require y ~= x;
}
"""
        wrapper = IvyParserWrapper()
        result = wrapper.parse(source, "multi_req.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "multi_req.ivy", source
        )

        require_reqs = [r for r in reqs if r.kind == "require"]
        assert len(require_reqs) >= 2, (
            f"Expected at least 2 require nodes, got {len(require_reqs)}"
        )


@requires_ivy
class TestExtractRequirementsFullDirectAction:
    """Parse source with direct action body: action send(x:t) = { require x > 0; }"""

    def test_require_in_direct_action(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = """\
#lang ivy1.7

type t
interpret t -> int

action send(x:t) = {
    require x > 0;
}
"""
        wrapper = IvyParserWrapper()
        result = wrapper.parse(source, "direct_action.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "direct_action.ivy", source
        )

        require_reqs = [r for r in reqs if r.kind == "require"]
        assert len(require_reqs) >= 1, (
            f"Expected at least one require in direct action, got {len(require_reqs)}"
        )

        req = require_reqs[0]
        assert req.kind == "require"
        assert req.mixin_kind == "direct"
        assert req.monitor_action == "send"


@requires_ivy
class TestExtractRequirementsFullAssignment:
    """Parse source with assignment: before foo { sent := true; } and check writes."""

    def test_assignment_produces_write(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = """\
#lang ivy1.7

type t

relation sent(X:t)

object foo = {
    action step(x:t)
}

before foo.step {
    sent(x) := true;
}
"""
        wrapper = IvyParserWrapper()
        result = wrapper.parse(source, "assign_test.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "assign_test.ivy", source
        )

        assert len(writes) >= 1, (
            f"Expected at least one write for assignment, got {len(writes)}"
        )

        # Each write is a (var_name, filepath, line) triple
        var_names = [w[0] for w in writes]
        assert any("sent" in v for v in var_names), (
            f"Expected 'sent' in write variable names, got {var_names}"
        )

        # Verify tuple structure
        for var_name, filepath, line in writes:
            assert isinstance(var_name, str)
            assert filepath == "assign_test.ivy"
            assert isinstance(line, int)


@requires_ivy
class TestExtractBracketTagFromParsedSource:
    """Test bracket tag extraction from a source line via full extraction."""

    def test_bracket_tag_extracted(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = """\
#lang ivy1.7

type t

object foo = {
    action step(x:t)
}

before foo.step {
    require x ~= x; # [42]
}
"""
        wrapper = IvyParserWrapper()
        result = wrapper.parse(source, "tag_test.ivy")
        assert result.success, f"Parse failed: {result.errors}"

        reqs, writes = extract_requirements_full(
            result.ast, "tag_test.ivy", source
        )

        require_reqs = [r for r in reqs if r.kind == "require"]
        assert len(require_reqs) >= 1

        # At least one requirement should have the bracket tag
        tags = [r.bracket_tag for r in require_reqs if r.bracket_tag is not None]
        assert "42" in tags, (
            f"Expected bracket tag '42' in tags, got {tags}"
        )


@requires_ivy
class TestExtractRequirementsFullNone:
    """Edge cases for extract_requirements_full."""

    def test_none_ast_returns_empty(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full

        reqs, writes = extract_requirements_full(None, "test.ivy", "")
        assert reqs == []
        assert writes == []

    def test_ast_without_decls_returns_empty(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full

        class FakeAst:
            pass

        reqs, writes = extract_requirements_full(FakeAst(), "test.ivy", "")
        assert reqs == []
        assert writes == []

    def test_empty_source_parses_clean(self):
        from ivy_lsp.analysis.requirement_extractor import extract_requirements_full
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        wrapper = IvyParserWrapper()
        result = wrapper.parse("#lang ivy1.7\n\ntype t\n", "empty.ivy")
        assert result.success

        reqs, writes = extract_requirements_full(
            result.ast, "empty.ivy", "#lang ivy1.7\n\ntype t\n"
        )
        assert reqs == []
        assert writes == []
