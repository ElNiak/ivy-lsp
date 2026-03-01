"""Tests for NCT classification enums and functions."""
import pytest
from ivy_lsp.analysis.requirement_graph import RequirementNode
from ivy_lsp.analysis.test_scope import (
    ActionClassification,
    NctClassification,
    TestScope,
    classify_action_direction,
    classify_requirement,
)


def _req(kind="require", mixin_kind="before", formula="true"):
    """Build a RequirementNode for classification testing."""
    return RequirementNode(
        id="/test:1", kind=kind, formula_text=formula, line=1, col=0,
        file="/test", monitor_action="act", mixin_kind=mixin_kind,
    )


class TestNctClassification:
    """Test classify_requirement() for NCT assume/guarantee semantics."""

    def test_require_before_is_assumption(self):
        assert classify_requirement(_req("require", "before")) == NctClassification.ASSUMPTION

    def test_require_after_is_guarantee(self):
        assert classify_requirement(_req("require", "after")) == NctClassification.GUARANTEE

    def test_ensure_after_is_guarantee(self):
        assert classify_requirement(_req("ensure", "after")) == NctClassification.GUARANTEE

    def test_assert_after_is_guarantee(self):
        assert classify_requirement(_req("assert", "after")) == NctClassification.GUARANTEE

    def test_assume_before_is_assumption(self):
        assert classify_requirement(_req("assume", "before")) == NctClassification.ASSUMPTION

    def test_generating_flag_is_tester_only(self):
        req = _req("require", "before", formula="connected & _generating")
        assert classify_requirement(req) == NctClassification.TESTER_ONLY

    def test_require_direct_is_assumption(self):
        assert classify_requirement(_req("require", "direct")) == NctClassification.ASSUMPTION

    def test_ensure_direct_is_guarantee(self):
        assert classify_requirement(_req("ensure", "direct")) == NctClassification.GUARANTEE

    def test_require_around_is_guarantee(self):
        """around mixins are conservatively classified as GUARANTEE."""
        assert classify_requirement(_req("require", "around")) == NctClassification.GUARANTEE

    def test_ensure_around_is_guarantee(self):
        assert classify_requirement(_req("ensure", "around")) == NctClassification.GUARANTEE

    def test_assume_around_is_guarantee(self):
        assert classify_requirement(_req("assume", "around")) == NctClassification.GUARANTEE


class TestActionClassification:
    """Test classify_action_direction() for action direction."""

    def test_exported_is_generated(self):
        scope = TestScope(
            test_file="/test.ivy", include_closure=frozenset(),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(), tester_role="client",
        )
        assert classify_action_direction("quic.send", scope) == ActionClassification.GENERATED

    def test_imported_is_received(self):
        scope = TestScope(
            test_file="/test.ivy", include_closure=frozenset(),
            exported_actions=frozenset(),
            imported_actions=frozenset({"tls.handshake"}), tester_role="client",
        )
        assert classify_action_direction("tls.handshake", scope) == ActionClassification.RECEIVED

    def test_neither_is_internal(self):
        scope = TestScope(
            test_file="/test.ivy", include_closure=frozenset(),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(), tester_role="client",
        )
        assert classify_action_direction("helper.compute", scope) == ActionClassification.INTERNAL
