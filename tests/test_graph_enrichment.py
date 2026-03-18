"""Tests for graph enrichment from CompiledModuleIR."""

from __future__ import annotations

import pytest

from ivy_lsp.compilation.ir import (
    ActionIR,
    CompiledModuleIR,
    MixinIR,
    RequirementIR,
    SortIR,
    SymbolIR,
)


class TestEnrichSemanticModel:
    def test_creates_tier3_type_nodes_from_sorts(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            sorts={
                "pkt_type": SortIR(
                    name="pkt_type",
                    is_enumerated=True,
                    constructors=["initial", "handshake"],
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        # Use the correct API: get_nodes_in_file
        nodes = model.get_nodes_in_file("test.ivy")
        type_nodes = [n for n in nodes if hasattr(n, "is_enum")]
        assert len(type_nodes) == 1
        assert type_nodes[0].is_enum is True
        assert type_nodes[0].variants == ["initial", "handshake"]
        assert type_nodes[0].tier == "tier3"

    def test_creates_tier3_symbol_nodes_from_symbols(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            symbols={
                "connected": SymbolIR(
                    name="connected",
                    sort_str="cid -> bool",
                    domain_sorts=["cid"],
                    range_sort="bool",
                    is_relation=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        sym_nodes = [n for n in nodes if hasattr(n, "kind")]
        assert len(sym_nodes) == 1
        assert sym_nodes[0].kind == "relation"
        assert sym_nodes[0].sort_name == "cid -> bool"

    def test_creates_symbol_nodes_from_actions(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            actions={
                "ext:send": ActionIR(
                    name="ext:send",
                    formal_params=["dst:cid"],
                    formal_returns=["ok:bool"],
                    is_exported=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        action_nodes = [n for n in nodes if hasattr(n, "kind") and n.kind == "action"]
        assert len(action_nodes) == 1
        assert action_nodes[0].qualified_name == "ext:send"
        assert action_nodes[0].arity == 1
        assert action_nodes[0].params == ["dst:cid"]
        assert action_nodes[0].return_sort == "ok:bool"

    def test_creates_destructor_kind(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            symbols={
                "stream_data": SymbolIR(
                    name="stream_data",
                    sort_str="stream_id -> stream_data_t",
                    domain_sorts=["stream_id"],
                    range_sort="stream_data_t",
                    is_destructor=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        sym_nodes = [n for n in nodes if hasattr(n, "kind")]
        assert len(sym_nodes) == 1
        assert sym_nodes[0].kind == "destructor"

    def test_creates_constructor_kind(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            symbols={
                "mk_pair": SymbolIR(
                    name="mk_pair",
                    sort_str="a * b -> pair",
                    domain_sorts=["a", "b"],
                    range_sort="pair",
                    is_constructor=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        sym_nodes = [n for n in nodes if hasattr(n, "kind")]
        assert len(sym_nodes) == 1
        assert sym_nodes[0].kind == "constructor"

    def test_creates_edges_for_domain_sorts(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.edges import SemanticEdgeType
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            symbols={
                "connected": SymbolIR(
                    name="connected",
                    sort_str="cid -> bool",
                    domain_sorts=["cid"],
                    range_sort="bool",
                    is_relation=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        assert model.edge_count() == 1
        outgoing = model.get_outgoing("compiled:test.ivy:connected")
        assert len(outgoing) == 1
        assert outgoing[0] == (SemanticEdgeType.HAS_PARAM, "cid")

    def test_skips_failed_ir(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR.empty("test.ivy", errors=["fail"])
        enrich_semantic_model(model, ir, "test.ivy")
        assert model.node_count() == 0

    def test_multiple_sorts_and_symbols(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            sorts={
                "pkt_type": SortIR(
                    name="pkt_type",
                    is_enumerated=True,
                    constructors=["initial", "handshake"],
                ),
                "cid": SortIR(name="cid", is_uninterpreted=True),
            },
            symbols={
                "conn_seen": SymbolIR(
                    name="conn_seen",
                    sort_str="cid -> bool",
                    domain_sorts=["cid"],
                    range_sort="bool",
                    is_relation=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        # 2 TypeNodes + 1 SymbolNode = 3 nodes
        assert model.node_count() == 3

    def test_action_with_no_returns(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            actions={
                "init": ActionIR(name="init", formal_params=[], formal_returns=[]),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        action_nodes = [n for n in nodes if hasattr(n, "kind") and n.kind == "action"]
        assert len(action_nodes) == 1
        assert action_nodes[0].return_sort is None
        assert action_nodes[0].arity == 0

    def test_creates_monitors_edges_from_mixins(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.edges import SemanticEdgeType
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            actions={
                "connect": ActionIR(name="connect"),
                "connect[before1]": ActionIR(name="connect[before1]"),
            },
            mixins={
                "connect": (
                    MixinIR(mixer="connect[before1]", mixee="connect", kind="before"),
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        outgoing = model.get_outgoing("compiled:test.ivy:connect[before1]")
        monitors_edges = [
            (t, tgt) for t, tgt in outgoing if t == SemanticEdgeType.MONITORS
        ]
        assert len(monitors_edges) == 1
        assert monitors_edges[0][1] == "compiled:test.ivy:connect"

    def test_creates_contains_edges_from_hierarchy(self):
        from ivy_lsp.compilation.graph_enrichment import enrich_semantic_model
        from ivy_lsp.semantic.edges import SemanticEdgeType
        from ivy_lsp.semantic.model import SemanticModel

        model = SemanticModel()
        ir = CompiledModuleIR(
            actions={
                "frame": ActionIR(name="frame"),
                "frame.ack": ActionIR(name="frame.ack"),
            },
            hierarchy={"frame": frozenset(["frame.ack"])},
            success=True,
            source_file="test.ivy",
        )
        enrich_semantic_model(model, ir, "test.ivy")
        outgoing = model.get_outgoing("compiled:test.ivy:frame")
        contains_edges = [
            (t, tgt) for t, tgt in outgoing if t == SemanticEdgeType.CONTAINS
        ]
        assert len(contains_edges) == 1
        assert contains_edges[0][1] == "compiled:test.ivy:frame.ack"


class TestEnrichRequirementGraph:
    def test_adds_action_nodes_from_ir(self):
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel
        from ivy_lsp.compilation.graph_enrichment import enrich_requirement_graph

        graph = ScopedRequirementModel()
        ir = CompiledModuleIR(
            actions={
                "ext:send": ActionIR(name="ext:send", is_exported=True),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_requirement_graph(graph, ir)
        assert "ext:send" in graph.actions

    def test_action_node_fields(self):
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel
        from ivy_lsp.compilation.graph_enrichment import enrich_requirement_graph

        graph = ScopedRequirementModel()
        ir = CompiledModuleIR(
            actions={
                "quic_server.send_packet": ActionIR(
                    name="quic_server.send_packet",
                    is_exported=True,
                ),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_requirement_graph(graph, ir)
        node = graph.actions["quic_server.send_packet"]
        assert node.id == "quic_server.send_packet"
        assert node.name == "send_packet"
        assert node.qualified_name == "quic_server.send_packet"
        assert node.file == "test.ivy"
        assert node.line == 0

    def test_skips_failed_ir(self):
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel
        from ivy_lsp.compilation.graph_enrichment import enrich_requirement_graph

        graph = ScopedRequirementModel()
        ir = CompiledModuleIR.empty("test.ivy")
        enrich_requirement_graph(graph, ir)
        assert len(graph.actions) == 0

    def test_does_not_overwrite_existing_action(self):
        from ivy_lsp.analysis.requirement_graph import ActionNode
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel
        from ivy_lsp.compilation.graph_enrichment import enrich_requirement_graph

        graph = ScopedRequirementModel()
        # Pre-populate with an existing action at line 42
        graph.add_action(
            ActionNode(
                id="ext:send",
                name="send",
                qualified_name="ext:send",
                file="original.ivy",
                line=42,
            )
        )
        ir = CompiledModuleIR(
            actions={
                "ext:send": ActionIR(name="ext:send", is_exported=True),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_requirement_graph(graph, ir)
        # Should keep the original, not overwrite
        assert graph.actions["ext:send"].file == "original.ivy"
        assert graph.actions["ext:send"].line == 42

    def test_multiple_actions(self):
        from ivy_lsp.analysis.test_scope import ScopedRequirementModel
        from ivy_lsp.compilation.graph_enrichment import enrich_requirement_graph

        graph = ScopedRequirementModel()
        ir = CompiledModuleIR(
            actions={
                "ext:send": ActionIR(name="ext:send", is_exported=True),
                "ext:recv": ActionIR(name="ext:recv", is_imported=True),
                "internal.init": ActionIR(name="internal.init"),
            },
            success=True,
            source_file="test.ivy",
        )
        enrich_requirement_graph(graph, ir)
        assert len(graph.actions) == 3
        assert "ext:send" in graph.actions
        assert "ext:recv" in graph.actions
        assert "internal.init" in graph.actions
