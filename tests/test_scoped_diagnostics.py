# tests/test_scoped_diagnostics.py
"""Tests for scoped requirement diagnostics (diagnostics + ScopedRequirementModel)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol import types as lsp
from lsprotocol.types import DiagnosticSeverity

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import (
    EdgeType,
    RequirementGraph,
    RequirementNode,
)
from ivy_lsp.analysis.test_scope import ScopedRequirementModel, TestScope
from ivy_lsp.features.diagnostics import compute_requirement_diagnostics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _abs(name: str) -> str:
    return os.path.abspath(name)


def _make_indexer(graph=None, include_graph=None):
    indexer = MagicMock()
    indexer.requirement_graph = graph
    indexer.include_graph = include_graph
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


def _unmonitored_diags(diags):
    """Filter diagnostics to only unmonitored-action hints."""
    return [d for d in diags if "no before/after" in d.message.lower()]


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
# Tests: Backward compatibility (no active scope)
# ---------------------------------------------------------------------------


class TestNoActiveScopeFallback:
    """When no active scope is set, behavior matches unscoped RequirementGraph."""

    def test_no_active_scope_flags_all_unmonitored(self, scoped_graph):
        """Both quic.send and quic.recv without monitors produce hints (no scope)."""
        # Use an empty graph so ALL actions are "unmonitored"
        model = ScopedRequirementModel()
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n    action quic.recv(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("test.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 2

    def test_plain_requirement_graph_ignores_scoping(self):
        """A plain RequirementGraph (not ScopedRequirementModel) works unchanged."""
        graph = RequirementGraph()
        indexer = _make_indexer(graph=graph)
        source = "    action foo.step(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("test.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 1
        assert "foo.step" in hints[0].message


# ---------------------------------------------------------------------------
# Tests: Active scope filters unmonitored diagnostics
# ---------------------------------------------------------------------------


class TestActiveScopeFiltersUnmonitored:
    """When an active scope is set, only exported actions are checked."""

    def test_non_exported_action_skipped(self, scoped_graph):
        """A non-exported action produces no diagnostic when scope is active."""
        scoped_graph.set_active_test(_abs("test_a.ivy"))  # exports quic.send
        # quic.recv is NOT exported by scope A -> should be skipped
        # Use a model where quic.recv has NO monitors to prove it's skipped
        model = ScopedRequirementModel()
        scope_a = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope_a)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        # quic.recv has no monitors AND is not exported -> no diagnostic
        source = "    action quic.recv(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 0

    def test_exported_action_without_monitors_produces_hint(self):
        """An exported action with no monitors in scope produces a Hint."""
        model = ScopedRequirementModel()
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 1
        assert "quic.send" in hints[0].message

    def test_exported_action_with_monitors_no_hint(self, scoped_graph):
        """An exported action that HAS monitors in scope produces no diagnostic."""
        scoped_graph.set_active_test(_abs("test_a.ivy"))  # exports quic.send
        indexer = _make_indexer(graph=scoped_graph)
        # quic.send has 2 requirements (req_a1 + req_s1) in scope A
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("shared.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 0

    def test_mixed_source_only_exported_flagged(self):
        """Source with both actions: only exported unmonitored one is flagged."""
        model = ScopedRequirementModel()
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        # Both unmonitored, but only quic.send is exported
        source = "    action quic.send(x:t)\n    action quic.recv(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 1
        assert "quic.send" in hints[0].message

    def test_switching_scope_changes_diagnostics(self):
        """Switching scope changes which actions are flagged."""
        model = ScopedRequirementModel()
        fp = _abs("f.ivy")
        scope_a = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), fp}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        scope_b = TestScope(
            test_file=_abs("test_b.ivy"),
            include_closure=frozenset({_abs("test_b.ivy"), fp}),
            exported_actions=frozenset({"quic.recv"}),
            imported_actions=frozenset(),
            tester_role="server",
        )
        model.register_test_scope(scope_a)
        model.register_test_scope(scope_b)
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n    action quic.recv(x:t)\n"

        # Scope A: only quic.send flagged
        model.set_active_test(_abs("test_a.ivy"))
        diags_a = compute_requirement_diagnostics(source, fp, indexer)
        hints_a = _unmonitored_diags(diags_a)
        assert len(hints_a) == 1
        assert "quic.send" in hints_a[0].message

        # Scope B: only quic.recv flagged
        model.set_active_test(_abs("test_b.ivy"))
        diags_b = compute_requirement_diagnostics(source, fp, indexer)
        hints_b = _unmonitored_diags(diags_b)
        assert len(hints_b) == 1
        assert "quic.recv" in hints_b[0].message

    def test_clearing_scope_restores_global(self):
        """set_active_test(None) restores unscoped behavior."""
        model = ScopedRequirementModel()
        fp = _abs("f.ivy")
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), fp}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n    action quic.recv(x:t)\n"

        # Scoped: only 1 hint
        model.set_active_test(_abs("test_a.ivy"))
        diags_scoped = compute_requirement_diagnostics(source, fp, indexer)
        assert len(_unmonitored_diags(diags_scoped)) == 1

        # Cleared: both flagged again
        model.set_active_test(None)
        diags_global = compute_requirement_diagnostics(source, fp, indexer)
        assert len(_unmonitored_diags(diags_global)) == 2


# ---------------------------------------------------------------------------
# Tests: Diagnostic message and metadata
# ---------------------------------------------------------------------------


class TestScopedDiagnosticMessage:
    """Message wording differs between scoped and unscoped paths."""

    def test_scoped_message_says_active_test_scope(self):
        """When scope is active, message contains 'active test scope'."""
        model = ScopedRequirementModel()
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 1
        assert "active test scope" in hints[0].message

    def test_unscoped_message_says_in_scope(self):
        """When no scope, message contains 'in scope' (original wording)."""
        graph = RequirementGraph()
        indexer = _make_indexer(graph=graph)
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert len(hints) == 1
        assert "in scope" in hints[0].message
        assert "active test" not in hints[0].message


class TestScopedDiagnosticProperties:
    """Scoped diagnostics retain correct LSP properties."""

    def test_severity_is_hint(self):
        model = ScopedRequirementModel()
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert hints[0].severity == DiagnosticSeverity.Hint

    def test_source_is_ivy_lsp_reqs(self):
        model = ScopedRequirementModel()
        scope = TestScope(
            test_file=_abs("test_a.ivy"),
            include_closure=frozenset({_abs("test_a.ivy"), _abs("f.ivy")}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        model.register_test_scope(scope)
        model.set_active_test(_abs("test_a.ivy"))
        indexer = _make_indexer(graph=model)
        source = "    action quic.send(x:t)\n"
        diags = compute_requirement_diagnostics(source, _abs("f.ivy"), indexer)
        hints = _unmonitored_diags(diags)
        assert hints[0].source == "ivy-lsp-reqs"
