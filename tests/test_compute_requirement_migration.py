"""Asserts compute_requirement_diagnostics returns List[IvyDiagnostic].

Verifies each returned item uses registry-validated codes.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.requirement_graph import (
    EdgeType,
    RequirementGraph,
    RequirementNode,
)
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic, RelatedLocation
from ivy_lsp.lsp.diagnostics.compute import compute_requirement_diagnostics

pytestmark = pytest.mark.unit


def _abs(name: str) -> str:
    return os.path.abspath(name)


def _make_indexer_with_graph(graph, include_graph=None, resolver=None):
    indexer = MagicMock()
    indexer.requirement_graph = graph
    indexer.include_graph = include_graph
    if resolver is None:
        resolver = MagicMock()
        resolver.resolve.return_value = None
    indexer.resolver = resolver
    return indexer


class TestReturnType:
    """compute_requirement_diagnostics must return List[IvyDiagnostic]."""

    def test_returns_list_on_empty_graph(self, tmp_path):
        graph = RequirementGraph()
        indexer = _make_indexer_with_graph(graph)
        result = compute_requirement_diagnostics(
            source="", filepath=str(tmp_path / "x.ivy"), indexer=indexer
        )
        assert isinstance(result, list)

    def test_no_indexer_returns_empty_list(self, tmp_path):
        result = compute_requirement_diagnostics(
            source="action foo\n",
            filepath=str(tmp_path / "x.ivy"),
            indexer=None,
        )
        assert result == []

    def test_every_item_is_ivydiagnostic_for_unmonitored_action(self, tmp_path):
        """The unmonitored-action branch must emit IvyDiagnostic instances."""
        graph = RequirementGraph()
        indexer = _make_indexer_with_graph(graph)
        source = "#lang ivy1.7\n    action send(src:node, dst:node)\n"
        diags = compute_requirement_diagnostics(
            source=source,
            filepath=str(tmp_path / "x.ivy"),
            indexer=indexer,
        )
        assert len(diags) >= 1, "expected at least one unmonitored-action diagnostic"
        for d in diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_every_item_is_ivydiagnostic_for_high_impact_var(self, tmp_path):
        """The high-impact state variable branch must emit IvyDiagnostic."""
        filepath = str(tmp_path / "x.ivy")
        graph = RequirementGraph()
        for i in range(6):
            f = _abs(f"file_{i}.ivy")
            req = RequirementNode(
                id=f"{f}:{i}",
                kind="require",
                formula_text=f"connected(X{i},Y{i})",
                line=i,
                col=0,
                file=f,
                monitor_action="foo.step",
                mixin_kind="before",
            )
            graph.add_requirement(req)
            graph.add_edge(req.id, EdgeType.READS, "connected")

        indexer = _make_indexer_with_graph(graph)
        source = "#lang ivy1.7\n    relation connected(X:cid, Y:cid)\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        impact_diags = [d for d in diags if "high-impact" in d.message.lower()]
        assert len(impact_diags) >= 1
        for d in impact_diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"

    def test_every_item_is_ivydiagnostic_for_include_chain(self, tmp_path):
        """The include chain propagation branch must emit IvyDiagnostic."""
        filepath = str(tmp_path / "main.ivy")
        other_file = _abs("types.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{other_file}:5",
            kind="require",
            formula_text="x > 0",
            line=5,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()

        resolver = MagicMock()
        resolver.resolve.return_value = other_file

        indexer = _make_indexer_with_graph(
            graph, include_graph=include_graph, resolver=resolver
        )
        source = "#lang ivy1.7\ninclude types\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        include_diags = [d for d in diags if "brings" in d.message.lower()]
        assert len(include_diags) >= 1
        for d in include_diags:
            assert isinstance(
                d, IvyDiagnostic
            ), f"expected IvyDiagnostic, got {type(d).__name__}"


class TestRelatedLocationShape:
    """Related info must use RelatedLocation, not lsp.DiagnosticRelatedInformation."""

    def test_include_chain_related_uses_related_location(self, tmp_path):
        filepath = str(tmp_path / "main.ivy")
        other_file = _abs("mod.ivy")
        graph = RequirementGraph()
        req = RequirementNode(
            id=f"{other_file}:2",
            kind="require",
            formula_text="cond",
            line=2,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()
        resolver = MagicMock()
        resolver.resolve.return_value = other_file

        indexer = _make_indexer_with_graph(
            graph, include_graph=include_graph, resolver=resolver
        )
        source = "#lang ivy1.7\ninclude mod\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        include_diags = [d for d in diags if "brings" in d.message.lower()]
        assert len(include_diags) >= 1
        diag = include_diags[0]
        assert hasattr(diag, "related"), "IvyDiagnostic must have .related attribute"
        assert len(diag.related) >= 1
        rel = diag.related[0]
        assert isinstance(
            rel, RelatedLocation
        ), f"expected RelatedLocation, got {type(rel).__name__}"
        assert other_file in rel.file


class TestCanonicalCodes:
    """Spot-check that the three emitted codes are exactly the expected canonical ones."""

    def test_nomonitor_code_value(self, tmp_path):
        graph = RequirementGraph()
        indexer = _make_indexer_with_graph(graph)
        source = "#lang ivy1.7\n    action unguarded(x:t)\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=str(tmp_path / "x.ivy"), indexer=indexer
        )
        action_diags = [d for d in diags if "no before/after" in d.message]
        assert len(action_diags) >= 1
        assert action_diags[0].code == "ivy.action.noMonitor"

    def test_high_impact_code_value(self, tmp_path):
        filepath = str(tmp_path / "x.ivy")
        graph = RequirementGraph()
        for i in range(5):
            f = _abs(f"r_{i}.ivy")
            req = RequirementNode(
                id=f"{f}:{i}",
                kind="require",
                formula_text="st(X)",
                line=i,
                col=0,
                file=f,
                monitor_action="foo.step",
                mixin_kind="before",
            )
            graph.add_requirement(req)
            graph.add_edge(req.id, EdgeType.READS, "st")
        indexer = _make_indexer_with_graph(graph)
        source = "#lang ivy1.7\n    relation st(X:t)\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        impact_diags = [d for d in diags if "high-impact" in d.message.lower()]
        assert len(impact_diags) >= 1
        assert impact_diags[0].code == "ivy.invariant.highImpactVar"

    def test_inherited_requirements_code_value(self, tmp_path):
        filepath = str(tmp_path / "main.ivy")
        other_file = _abs("dep.ivy")
        graph = RequirementGraph()
        req = RequirementNode(
            id=f"{other_file}:1",
            kind="require",
            formula_text="true",
            line=1,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()
        resolver = MagicMock()
        resolver.resolve.return_value = other_file

        indexer = _make_indexer_with_graph(
            graph, include_graph=include_graph, resolver=resolver
        )
        source = "#lang ivy1.7\ninclude dep\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        include_diags = [d for d in diags if "brings" in d.message.lower()]
        assert len(include_diags) >= 1
        assert include_diags[0].code == "ivy.module.inheritedRequirements"


class TestRangePrecision:
    """Phase 5 cluster 5.3: token-precise spans on requirement diagnostics."""

    def test_nomonitor_spans_action_name_token(self, tmp_path):
        graph = RequirementGraph()
        indexer = _make_indexer_with_graph(graph)
        # Line 1 = "    action unguarded(x:t)"; "unguarded" starts at column 11.
        source = "#lang ivy1.7\n    action unguarded(x:t)\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=str(tmp_path / "x.ivy"), indexer=indexer
        )
        d = next(d for d in diags if d.code == "ivy.action.noMonitor")
        assert d.line == 1
        assert d.character == len("    action ")
        assert d.end_character == d.character + len("unguarded")

    def test_high_impact_spans_state_var_name(self, tmp_path):
        filepath = str(tmp_path / "x.ivy")
        graph = RequirementGraph()
        for i in range(5):
            f = _abs(f"r_{i}.ivy")
            req = RequirementNode(
                id=f"{f}:{i}",
                kind="require",
                formula_text="st(X)",
                line=i,
                col=0,
                file=f,
                monitor_action="foo.step",
                mixin_kind="before",
            )
            graph.add_requirement(req)
            graph.add_edge(req.id, EdgeType.READS, "st")
        indexer = _make_indexer_with_graph(graph)
        # Line 1 = "    relation st(X:t)"; "st" starts at column 13.
        source = "#lang ivy1.7\n    relation st(X:t)\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        d = next(d for d in diags if d.code == "ivy.invariant.highImpactVar")
        assert d.line == 1
        assert d.character == len("    relation ")
        assert d.end_character == d.character + len("st")

    def test_inherited_requirements_spans_include_name(self, tmp_path):
        filepath = str(tmp_path / "main.ivy")
        other_file = _abs("dep.ivy")
        graph = RequirementGraph()
        req = RequirementNode(
            id=f"{other_file}:1",
            kind="require",
            formula_text="true",
            line=1,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()
        resolver = MagicMock()
        resolver.resolve.return_value = other_file

        indexer = _make_indexer_with_graph(
            graph, include_graph=include_graph, resolver=resolver
        )
        # Line 1 = "include dep"; "dep" starts at column 8.
        source = "#lang ivy1.7\ninclude dep\n"
        diags = compute_requirement_diagnostics(
            source=source, filepath=filepath, indexer=indexer
        )
        d = next(d for d in diags if d.code == "ivy.module.inheritedRequirements")
        assert d.line == 1
        assert d.character == len("include ")
        assert d.end_character == d.character + len("dep")
