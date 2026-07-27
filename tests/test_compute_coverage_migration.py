"""Migration tests for compute_coverage_hints.

Asserts the function returns List[IvyDiagnostic] with registry-validated
codes and registry-matching source strings.

I2 note: vacuous empty-graph tests replaced with a real RequirementGraph
fixture (same pattern as test_coverage_hints.py). Behavioral correctness
for all 6 emit paths is covered by test_coverage_hints.py; this file
focuses on the migration contract and the C1 data["tags"] fix.
"""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.analysis.requirement_graph import (
    ActionNode,
    EdgeType,
    RequirementGraph,
    StateVarNode,
)
from ivy_lsp.core.coverage_hints import compute_coverage_hints
from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic

pytestmark = pytest.mark.unit

FILEPATH = "/fake/migration_test.ivy"


def _make_graph_with_unguarded_action() -> RequirementGraph:
    """Graph: action 'send' writes 'sent_pkt' (unguarded). Triggers emit path 2b."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="send",
            name="send",
            qualified_name="send",
            file=FILEPATH,
            line=10,
        )
    )
    g.add_state_var(
        StateVarNode(
            id="sent_pkt",
            name="sent_pkt",
            qualified_name="sent_pkt",
            file=FILEPATH,
            line=5,
            is_relation=True,
        )
    )
    g.add_edge(f"{FILEPATH}:12:write:sent_pkt", EdgeType.WRITES, "sent_pkt")
    return g


class TestReturnType:
    def test_returns_empty_list_for_none_graph(self):
        result = compute_coverage_hints(None, FILEPATH)
        assert result == []

    def test_every_returned_item_is_ivydiagnostic(self):
        """Populated graph — all returned items must be IvyDiagnostic instances."""
        graph = _make_graph_with_unguarded_action()
        result = compute_coverage_hints(graph, FILEPATH)
        assert result, "Expected at least one diagnostic from the populated graph"
        for d in result:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_tags_include_unnecessary_on_all_hints(self):
        """C1 fix: every coverage hint must carry DiagnosticTag.Unnecessary in tags."""
        graph = _make_graph_with_unguarded_action()
        result = compute_coverage_hints(graph, FILEPATH)
        assert result, "Expected at least one diagnostic to check tags"
        for d in result:
            tags = d.tags or []
            assert lsp.DiagnosticTag.Unnecessary in tags, (
                f"DiagnosticTag.Unnecessary missing from tags on {d.code!r}; "
                f"got: {tags!r}"
            )


class TestSourceConsistency:
    """Every emitted diagnostic's source must match the registry descriptor.

    This is the lesson from Task 5 and 6 source-mismatch findings:
    ivy.action.noMonitor and ivy.action.unguardedWrite are registered with
    source='ivy-semantic' (not 'ivy-lsp-coverage'), while ivy.require.deadGuard,
    ivy.monitor.orphanedHook, and ivy.state.unusedStateVar use 'ivy-lsp-coverage'.
    """

    def test_emitted_source_matches_descriptor(self):
        graph = _make_graph_with_unguarded_action()
        result = compute_coverage_hints(graph, FILEPATH)
        for d in result:
            descriptor = DIAGNOSTIC_REGISTRY[d.code]
            assert d.source == descriptor.source, (
                f"emit-site source {d.source!r} != descriptor source "
                f"{descriptor.source!r} for code {d.code}"
            )


class TestCodeRegistration:
    """Per-code source strings must match registry expectations."""

    def test_known_sources(self):
        """Confirm the per-code sources match expectations established by Task 8."""
        semantic_codes = {"ivy.action.noMonitor", "ivy.action.unguardedWrite"}
        coverage_codes = {
            "ivy.require.deadGuard",
            "ivy.monitor.orphanedHook",
            "ivy.state.unusedStateVar",
        }
        for code in semantic_codes:
            assert DIAGNOSTIC_REGISTRY[code].source == "ivy-semantic", (
                f"Expected {code!r} source='ivy-semantic', "
                f"got {DIAGNOSTIC_REGISTRY[code].source!r}"
            )
        for code in coverage_codes:
            assert DIAGNOSTIC_REGISTRY[code].source == "ivy-lsp-coverage", (
                f"Expected {code!r} source='ivy-lsp-coverage', "
                f"got {DIAGNOSTIC_REGISTRY[code].source!r}"
            )
