"""Tests for SymbolReference extraction from Ivy source."""

from __future__ import annotations

import pytest

from ivy_lsp.parsing.symbols import SymbolReference


class TestSymbolReference:
    @pytest.mark.unit
    def test_create_call_reference(self):
        ref = SymbolReference(
            source_name="process",
            target_name="connect",
            kind="call",
            line=7,
        )
        assert ref.source_name == "process"
        assert ref.target_name == "connect"
        assert ref.kind == "call"
        assert ref.line == 7
        assert ref.col == 0
        assert ref.file_path is None

    @pytest.mark.unit
    def test_create_instance_reference(self):
        ref = SymbolReference(
            source_name="net",
            target_name="tcp_endpoint",
            kind="instance",
            line=12,
            file_path="proto.ivy",
        )
        assert ref.kind == "instance"
        assert ref.file_path == "proto.ivy"

    @pytest.mark.unit
    def test_create_monitor_reference(self):
        ref = SymbolReference(
            source_name="before connect",
            target_name="connect",
            kind="monitor",
            line=10,
        )
        assert ref.kind == "monitor"


class TestEdgeTypes:
    @pytest.mark.unit
    def test_calls_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.CALLS.value == "calls"

    @pytest.mark.unit
    def test_uses_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.USES.value == "uses"

    @pytest.mark.unit
    def test_monitors_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.MONITORS.value == "monitors"

    @pytest.mark.unit
    def test_contains_edge_type_exists(self):
        from ivy_lsp.semantic.edges import SemanticEdgeType

        assert SemanticEdgeType.CONTAINS.value == "contains"
