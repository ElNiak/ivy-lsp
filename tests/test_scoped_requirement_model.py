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
        assert ("/test/test_a.ivy", False) in scoped_model._scope_cache
        scoped_model.invalidate_file("/test/shared.ivy")
        assert ("/test/test_a.ivy", False) not in scoped_model._scope_cache

    def test_invalidate_unrelated_file_preserves_cache(self, scoped_model):
        scoped_model.get_scoped_requirements("/test/test_a.ivy")
        scoped_model.invalidate_file("/test/file_b.ivy")
        assert ("/test/test_a.ivy", False) in scoped_model._scope_cache


class TestScopedNctCounts:
    """Tests for get_scoped_nct_counts() method.

    Uses per-requirement NCT classification via classify_requirement().
    Default _make_req creates mixin_kind="before", so:
      require + before = ASSUMPTION
      ensure  + before = GUARANTEE  (kind in ensure/assert)
      require + after  = GUARANTEE  (mixin_kind after)
    """

    def test_exported_action_require_before_is_assumption(self, scoped_model):
        """require + before mixin on exported action -> ASSUMPTION."""
        entries = scoped_model.get_scoped_nct_counts(
            "/test/test_a.ivy", "quic.send"
        )
        assert len(entries) == 1
        assert entries[0] == {"kind": "require", "count": 2, "nct_tag": "ASSUMPTION"}

    def test_imported_action_tagged_assumption(self):
        """Requirements on imported actions are tagged ASSUMPTION."""
        model = ScopedRequirementModel()
        req = _make_req("/f.ivy", 10, "require", "x > 0", "tls.handshake")
        model.add_requirement(req)
        model.add_edge(req.id, EdgeType.CONSTRAINS, "tls.handshake")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset({"tls.handshake"}),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "tls.handshake")
        assert len(entries) == 1
        assert entries[0]["nct_tag"] == "ASSUMPTION"
        assert entries[0]["kind"] == "require"
        assert entries[0]["count"] == 1

    def test_internal_action_excluded(self):
        """Internal actions (neither exported nor imported) return empty list."""
        model = ScopedRequirementModel()
        req = _make_req("/f.ivy", 10, "require", "x > 0", "internal.helper")
        model.add_requirement(req)
        model.add_edge(req.id, EdgeType.CONSTRAINS, "internal.helper")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "internal.helper")
        assert entries == []

    def test_multiple_kinds_same_action(self):
        """An exported action with both require+before and ensure+before gets
        per-requirement NCT tags: require+before=ASSUMPTION, ensure+before=GUARANTEE."""
        model = ScopedRequirementModel()
        req1 = _make_req("/f.ivy", 10, "require", "x > 0", "quic.send")
        req2 = _make_req("/f.ivy", 20, "ensure", "y > 0", "quic.send")
        model.add_requirement(req1)
        model.add_requirement(req2)
        model.add_edge(req1.id, EdgeType.CONSTRAINS, "quic.send")
        model.add_edge(req2.id, EdgeType.CONSTRAINS, "quic.send")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "quic.send")
        by_kind = {e["kind"]: e for e in entries}
        assert "require" in by_kind
        assert "ensure" in by_kind
        assert by_kind["require"]["nct_tag"] == "ASSUMPTION"
        assert by_kind["ensure"]["nct_tag"] == "GUARANTEE"
        assert by_kind["require"]["count"] == 1
        assert by_kind["ensure"]["count"] == 1

    def test_after_mixin_require_is_guarantee(self):
        """require + after mixin on exported action -> GUARANTEE."""
        model = ScopedRequirementModel()
        req = _make_req(
            "/f.ivy", 10, "require", "x > 0", "quic.send", mixin_kind="after"
        )
        model.add_requirement(req)
        model.add_edge(req.id, EdgeType.CONSTRAINS, "quic.send")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "quic.send")
        assert len(entries) == 1
        assert entries[0] == {"kind": "require", "count": 1, "nct_tag": "GUARANTEE"}

    def test_mixed_mixin_kinds_produce_different_nct_tags(self):
        """Same kind (require) with different mixin_kinds gets split entries."""
        model = ScopedRequirementModel()
        req1 = _make_req(
            "/f.ivy", 10, "require", "pre > 0", "quic.send", mixin_kind="before"
        )
        req2 = _make_req(
            "/f.ivy", 20, "require", "post > 0", "quic.send", mixin_kind="after"
        )
        model.add_requirement(req1)
        model.add_requirement(req2)
        model.add_edge(req1.id, EdgeType.CONSTRAINS, "quic.send")
        model.add_edge(req2.id, EdgeType.CONSTRAINS, "quic.send")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "quic.send")
        by_tag = {e["nct_tag"]: e for e in entries}
        assert "ASSUMPTION" in by_tag
        assert "GUARANTEE" in by_tag
        assert by_tag["ASSUMPTION"]["kind"] == "require"
        assert by_tag["GUARANTEE"]["kind"] == "require"

    def test_unknown_test_returns_empty(self, scoped_model):
        """Unregistered test file returns empty list."""
        entries = scoped_model.get_scoped_nct_counts(
            "/nonexistent.ivy", "quic.send"
        )
        assert entries == []

    def test_unknown_action_returns_empty(self, scoped_model):
        """Action not in scope (internal) returns empty list."""
        entries = scoped_model.get_scoped_nct_counts(
            "/test/test_a.ivy", "nonexistent.action"
        )
        assert entries == []

    def test_imported_action_outside_include_closure_excluded(self):
        """Imported action requirements in files outside include_closure are excluded."""
        model = ScopedRequirementModel()
        req = _make_req("/outside.ivy", 10, "require", "x > 0", "tls.handshake")
        model.add_requirement(req)
        model.add_edge(req.id, EdgeType.CONSTRAINS, "tls.handshake")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset({"tls.handshake"}),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "tls.handshake")
        assert entries == []

    def test_entries_have_required_keys(self, scoped_model):
        """Each entry dict has kind, count, nct_tag with correct types."""
        entries = scoped_model.get_scoped_nct_counts(
            "/test/test_a.ivy", "quic.send"
        )
        for entry in entries:
            assert "kind" in entry
            assert "count" in entry
            assert "nct_tag" in entry
            assert isinstance(entry["count"], int)
            assert entry["count"] > 0

    def test_entries_sorted_by_kind_and_nct_tag(self):
        """Returned entries are sorted by (kind, nct_tag)."""
        model = ScopedRequirementModel()
        req1 = _make_req("/f.ivy", 10, "require", "x > 0", "quic.send")
        req2 = _make_req("/f.ivy", 20, "ensure", "y > 0", "quic.send")
        model.add_requirement(req1)
        model.add_requirement(req2)
        model.add_edge(req1.id, EdgeType.CONSTRAINS, "quic.send")
        model.add_edge(req2.id, EdgeType.CONSTRAINS, "quic.send")

        scope = TestScope(
            test_file="/test.ivy",
            include_closure=frozenset({"/test.ivy", "/f.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)

        entries = model.get_scoped_nct_counts("/test.ivy", "quic.send")
        keys = [(e["kind"], e["nct_tag"]) for e in entries]
        assert keys == sorted(keys)
