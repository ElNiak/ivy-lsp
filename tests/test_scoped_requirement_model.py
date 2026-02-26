"""Tests for ScopedRequirementModel scoped queries."""
import pytest
from ivy_lsp.analysis.requirement_graph import (
    ActionNode, EdgeType, RequirementGraph, RequirementNode, StateVarNode,
)
from ivy_lsp.analysis.test_scope import (
    ExportImportInfo, ScopedRequirementModel, TestScope,
)


def _make_req(file, line, kind="require", formula="true", action="", mixin_kind="before"):
    return RequirementNode(
        id=f"{file}:{line}", kind=kind, formula_text=formula,
        line=line, col=0, file=file, monitor_action=action, mixin_kind=mixin_kind,
    )


@pytest.fixture
def scoped_model():
    """Model with two test scopes sharing some files.

    Test A (client test): exports quic.send, includes file_a + shared
    Test B (server test): exports quic.recv, includes file_b + shared
    Shared file has requirements on both quic.send and quic.recv.
    """
    model = ScopedRequirementModel()

    req_a1 = _make_req("/test/file_a.ivy", 10, "require", "x > 0", "quic.send")
    model.add_requirement(req_a1)
    model.add_edge(req_a1.id, EdgeType.CONSTRAINS, "quic.send")

    req_b1 = _make_req("/test/file_b.ivy", 10, "require", "y > 0", "quic.recv")
    model.add_requirement(req_b1)
    model.add_edge(req_b1.id, EdgeType.CONSTRAINS, "quic.recv")

    req_s1 = _make_req("/test/shared.ivy", 5, "require", "connected", "quic.send")
    req_s2 = _make_req("/test/shared.ivy", 10, "ensure", "acked", "quic.recv")
    model.add_requirement(req_s1)
    model.add_requirement(req_s2)
    model.add_edge(req_s1.id, EdgeType.CONSTRAINS, "quic.send")
    model.add_edge(req_s2.id, EdgeType.CONSTRAINS, "quic.recv")

    scope_a = TestScope(
        test_file="/test/test_a.ivy",
        include_closure=frozenset({"/test/test_a.ivy", "/test/file_a.ivy", "/test/shared.ivy"}),
        exported_actions=frozenset({"quic.send"}),
        imported_actions=frozenset(),
        tester_role="client",
    )
    scope_b = TestScope(
        test_file="/test/test_b.ivy",
        include_closure=frozenset({"/test/test_b.ivy", "/test/file_b.ivy", "/test/shared.ivy"}),
        exported_actions=frozenset({"quic.recv"}),
        imported_actions=frozenset(),
        tester_role="server",
    )
    model.register_test_scope(scope_a)
    model.register_test_scope(scope_b)
    return model


class TestBackwardCompatibility:
    def test_is_subclass(self):
        assert issubclass(ScopedRequirementModel, RequirementGraph)

    def test_unscoped_get_requirements_for_action(self, scoped_model):
        reqs = scoped_model.get_requirements_for_action("quic.send")
        assert len(reqs) == 2

    def test_unscoped_counts(self, scoped_model):
        counts = scoped_model.get_requirement_counts_for_action("quic.send")
        assert counts["require"] == 2


class TestScopedQueries:
    def test_scoped_requirements_test_a(self, scoped_model):
        reqs = scoped_model.get_scoped_requirements("/test/test_a.ivy")
        assert len(reqs) == 2
        ids = {r.id for r in reqs}
        assert "/test/file_a.ivy:10" in ids
        assert "/test/shared.ivy:5" in ids

    def test_scoped_requirements_test_b(self, scoped_model):
        reqs = scoped_model.get_scoped_requirements("/test/test_b.ivy")
        assert len(reqs) == 2
        ids = {r.id for r in reqs}
        assert "/test/file_b.ivy:10" in ids
        assert "/test/shared.ivy:10" in ids

    def test_scoped_counts(self, scoped_model):
        counts = scoped_model.get_scoped_counts("/test/test_a.ivy", "quic.send")
        assert counts == {"require": 2}

    def test_scoped_counts_excludes_other_test(self, scoped_model):
        counts = scoped_model.get_scoped_counts("/test/test_a.ivy", "quic.recv")
        assert counts == {}

    def test_get_tests_for_file(self, scoped_model):
        tests = scoped_model.get_tests_for_file("/test/shared.ivy")
        assert tests == {"/test/test_a.ivy", "/test/test_b.ivy"}

    def test_get_tests_for_exclusive_file(self, scoped_model):
        tests = scoped_model.get_tests_for_file("/test/file_a.ivy")
        assert tests == {"/test/test_a.ivy"}

    def test_get_tests_for_unknown_file(self, scoped_model):
        tests = scoped_model.get_tests_for_file("/unknown.ivy")
        assert tests == set()


class TestActiveTestSelector:
    def test_no_active_test_by_default(self, scoped_model):
        assert scoped_model.get_active_scope() is None

    def test_set_active_test(self, scoped_model):
        scoped_model.set_active_test("/test/test_a.ivy")
        scope = scoped_model.get_active_scope()
        assert scope is not None
        assert scope.test_file == "/test/test_a.ivy"

    def test_clear_active_test(self, scoped_model):
        scoped_model.set_active_test("/test/test_a.ivy")
        scoped_model.set_active_test(None)
        assert scoped_model.get_active_scope() is None

    def test_set_unknown_test_is_noop(self, scoped_model):
        scoped_model.set_active_test("/nonexistent.ivy")
        assert scoped_model.get_active_scope() is None


class TestCacheInvalidation:
    def test_invalidate_file_clears_scope_cache(self, scoped_model):
        scoped_model.get_scoped_requirements("/test/test_a.ivy")
        assert "/test/test_a.ivy" in scoped_model._scope_cache
        scoped_model.invalidate_file("/test/shared.ivy")
        assert "/test/test_a.ivy" not in scoped_model._scope_cache

    def test_invalidate_unrelated_file_preserves_cache(self, scoped_model):
        scoped_model.get_scoped_requirements("/test/test_a.ivy")
        scoped_model.invalidate_file("/test/file_b.ivy")
        assert "/test/test_a.ivy" in scoped_model._scope_cache
