"""Migration tests for compute_coverage_hints.

Asserts the function returns List[IvyDiagnostic] with registry-validated
codes and registry-matching source strings.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from ivy_lsp.core.coverage_hints import compute_coverage_hints
from ivy_lsp.core.diagnostics.codes import DIAGNOSTIC_REGISTRY
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic

pytestmark = pytest.mark.unit


def _make_minimal_graph() -> Any:
    """Stub a RequirementGraph with no actions, requirements, or state vars.

    The function should return an empty list for an empty graph.
    """
    graph = MagicMock()
    graph.actions = {}
    graph.requirements = {}
    graph.properties = {}
    graph.state_vars = {}
    graph.edges = []
    graph.get_requirements_for_action = MagicMock(return_value=[])
    graph.get_outgoing_edges = MagicMock(return_value=[])
    return graph


class TestReturnType:
    def test_returns_list_on_empty_graph(self):
        graph = _make_minimal_graph()
        result = compute_coverage_hints(graph, "/tmp/x.ivy")
        assert isinstance(result, list)

    def test_every_returned_item_is_ivydiagnostic(self):
        graph = _make_minimal_graph()
        result = compute_coverage_hints(graph, "/tmp/x.ivy")
        for d in result:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_returns_none_for_none_graph(self):
        result = compute_coverage_hints(None, "/tmp/x.ivy")
        assert result == []


class TestSourceConsistency:
    """Every emitted diagnostic's source must match the registry descriptor.

    This is the lesson from Task 5 and 6 source-mismatch findings:
    ivy.action.noMonitor and ivy.action.unguardedWrite are registered with
    source='ivy-semantic' (not 'ivy-lsp-coverage'), while ivy.require.deadGuard,
    ivy.monitor.orphanedHook, and ivy.state.unusedStateVar use 'ivy-lsp-coverage'.
    """

    def test_emitted_source_matches_descriptor(self):
        graph = _make_minimal_graph()
        result = compute_coverage_hints(graph, "/tmp/x.ivy")
        for d in result:
            descriptor = DIAGNOSTIC_REGISTRY[d.code]
            assert d.source == descriptor.source, (
                f"emit-site source {d.source!r} != descriptor source "
                f"{descriptor.source!r} for code {d.code}"
            )


class TestCodeRegistration:
    """All codes used by coverage_hints are registered in DIAGNOSTIC_REGISTRY."""

    COVERAGE_CODES = (
        "ivy.action.noMonitor",
        "ivy.action.unguardedWrite",
        "ivy.require.deadGuard",
        "ivy.monitor.orphanedHook",
        "ivy.state.unusedStateVar",
    )

    def test_all_codes_registered(self):
        for code in self.COVERAGE_CODES:
            assert code in DIAGNOSTIC_REGISTRY, (
                f"Code {code!r} is used by compute_coverage_hints "
                f"but not registered in DIAGNOSTIC_REGISTRY"
            )

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
