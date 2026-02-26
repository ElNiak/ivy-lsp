"""Tests for scoped code lenses (code_lens + ScopedRequirementModel)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import (
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.analysis.test_scope import ScopedRequirementModel, TestScope
from ivy_lsp.features.code_lens import compute_code_lenses


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _abs(name: str) -> str:
    return os.path.abspath(name)


def _make_indexer(graph=None, include_graph=None):
    indexer = MagicMock()
    indexer._requirement_graph = graph
    indexer._include_graph = include_graph
    return indexer


def _make_req(file, line, kind="require", formula="true", action="", mixin_kind="before"):
    return RequirementNode(
        id=f"{file}:{line}",
        kind=kind,
        formula_text=formula,
        line=line,
        col=0,
        file=file,
        monitor_action=action,
        mixin_kind=mixin_kind,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped_graph():
    """ScopedRequirementModel with two test scopes sharing a file.

    Scope A (client): exports quic.send, includes file_a + shared
    Scope B (server): exports quic.recv, includes file_b + shared

    shared.ivy has requirements on BOTH quic.send and quic.recv.
    """
    fp_a = _abs("file_a.ivy")
    fp_b = _abs("file_b.ivy")
    fp_shared = _abs("shared.ivy")
    fp_test_a = _abs("test_a.ivy")
    fp_test_b = _abs("test_b.ivy")

    model = ScopedRequirementModel()

    # file_a: one require on quic.send
    req_a1 = _make_req(fp_a, 10, "require", "x > 0", "quic.send")
    model.add_requirement(req_a1)
    model.add_edge(req_a1.id, EdgeType.CONSTRAINS, "quic.send")

    # file_b: one require on quic.recv
    req_b1 = _make_req(fp_b, 10, "require", "y > 0", "quic.recv")
    model.add_requirement(req_b1)
    model.add_edge(req_b1.id, EdgeType.CONSTRAINS, "quic.recv")

    # shared: one require on quic.send, one ensure on quic.recv
    req_s1 = _make_req(fp_shared, 5, "require", "connected", "quic.send")
    req_s2 = _make_req(fp_shared, 10, "ensure", "acked", "quic.recv")
    model.add_requirement(req_s1)
    model.add_requirement(req_s2)
    model.add_edge(req_s1.id, EdgeType.CONSTRAINS, "quic.send")
    model.add_edge(req_s2.id, EdgeType.CONSTRAINS, "quic.recv")

    # Register scopes
    scope_a = TestScope(
        test_file=fp_test_a,
        include_closure=frozenset({fp_test_a, fp_a, fp_shared}),
        exported_actions=frozenset({"quic.send"}),
        imported_actions=frozenset(),
        tester_role="client",
    )
    scope_b = TestScope(
        test_file=fp_test_b,
        include_closure=frozenset({fp_test_b, fp_b, fp_shared}),
        exported_actions=frozenset({"quic.recv"}),
        imported_actions=frozenset(),
        tester_role="server",
    )
    model.register_test_scope(scope_a)
    model.register_test_scope(scope_b)

    return model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoActiveScopeFallsBack:
    """When no active scope is set, behavior matches unscoped RequirementGraph."""

    def test_no_active_scope_shows_all_actions(self, scoped_graph):
        """Both quic.send and quic.recv lenses appear when no scope is active."""
        source = "before quic.send {\n    require x > 0;\n}\nbefore quic.recv {\n    require y > 0;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)
        lenses = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles = [l.command.title for l in lenses if l.command]
        assert len(titles) == 2

    def test_plain_requirement_graph_ignores_scoping(self):
        """A plain RequirementGraph (not ScopedRequirementModel) works unchanged."""
        fp = _abs("test.ivy")
        graph = RequirementGraph()
        req = _make_req(fp, 1, "require", "x > 0", "foo.step")
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "foo.step")
        indexer = _make_indexer(graph=graph)
        source = "before foo.step {\n    require x > 0;\n}\n"
        lenses = compute_code_lenses(indexer, fp, source)
        assert len(lenses) >= 1
        assert "1 require" in lenses[0].command.title


class TestActiveScopeFiltersLenses:
    """When an active scope is set, only exported actions produce lenses."""

    def test_exported_action_produces_lens(self, scoped_graph):
        """quic.send lens appears when scope A (exports quic.send) is active."""
        scoped_graph.set_active_test(_abs("test_a.ivy"))
        source = "before quic.send {\n    require connected;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)
        lenses = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles = [l.command.title for l in lenses if l.command]
        assert len(titles) == 1
        assert "2 require" in titles[0]  # req_a1 + req_s1

    def test_non_exported_action_produces_no_lens(self, scoped_graph):
        """quic.recv lens does NOT appear when scope A is active."""
        scoped_graph.set_active_test(_abs("test_a.ivy"))
        source = "before quic.recv {\n    ensure acked;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)
        lenses = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles = [l.command.title for l in lenses if l.command]
        assert len(titles) == 0

    def test_mixed_source_only_exported_gets_lens(self, scoped_graph):
        """Source with both actions: only exported action gets a lens."""
        scoped_graph.set_active_test(_abs("test_a.ivy"))
        source = "before quic.send {\n    require x > 0;\n}\nbefore quic.recv {\n    ensure acked;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)
        lenses = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles = [l.command.title for l in lenses if l.command]
        assert len(titles) == 1
        assert "require" in titles[0]

    def test_switching_scope_changes_lenses(self, scoped_graph):
        """Switching from scope A to scope B changes which actions get lenses."""
        source = "before quic.send {\n    require x > 0;\n}\nbefore quic.recv {\n    ensure acked;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)

        # Scope A: only quic.send
        scoped_graph.set_active_test(_abs("test_a.ivy"))
        lenses_a = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles_a = [l.command.title for l in lenses_a if l.command]
        assert len(titles_a) == 1
        assert "require" in titles_a[0]

        # Scope B: only quic.recv
        scoped_graph.set_active_test(_abs("test_b.ivy"))
        lenses_b = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        titles_b = [l.command.title for l in lenses_b if l.command]
        assert len(titles_b) == 1
        assert "ensure" in titles_b[0]

    def test_clearing_scope_restores_global_lenses(self, scoped_graph):
        """set_active_test(None) restores unscoped behavior."""
        source = "before quic.send {\n    require x > 0;\n}\nbefore quic.recv {\n    ensure acked;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)

        scoped_graph.set_active_test(_abs("test_a.ivy"))
        lenses_scoped = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        assert len(lenses_scoped) == 1

        scoped_graph.set_active_test(None)
        lenses_global = compute_code_lenses(indexer, _abs("shared.ivy"), source)
        assert len(lenses_global) == 2


class TestScopedStateVarReads:
    """State var read counts are also scoped to the active test."""

    def test_state_var_reads_scoped(self, scoped_graph):
        """Only state var reads from in-scope requirements appear in the title."""
        fp_shared = _abs("shared.ivy")

        # Add state var and READS edge from req_s1 (quic.send, in scope A)
        sv = StateVarNode(
            id="sv_conn", name="conn_state", qualified_name="conn_state",
            file=fp_shared, line=1, is_relation=True,
        )
        scoped_graph.add_state_var(sv)
        scoped_graph.add_edge(f"{fp_shared}:5", EdgeType.READS, "sv_conn")

        # Add another state var read from req_s2 (quic.recv, NOT in scope A)
        sv2 = StateVarNode(
            id="sv_ack", name="ack_state", qualified_name="ack_state",
            file=fp_shared, line=2, is_relation=True,
        )
        scoped_graph.add_state_var(sv2)
        scoped_graph.add_edge(f"{fp_shared}:10", EdgeType.READS, "sv_ack")

        scoped_graph.set_active_test(_abs("test_a.ivy"))

        source = "before quic.send {\n    require connected;\n}\nbefore quic.recv {\n    ensure acked;\n}\n"
        indexer = _make_indexer(graph=scoped_graph)
        lenses = compute_code_lenses(indexer, fp_shared, source)

        # Only quic.send lens should appear, reading 1 state var (sv_conn)
        titles = [l.command.title for l in lenses if l.command]
        assert len(titles) == 1
        assert "reads 1 state var" in titles[0]
