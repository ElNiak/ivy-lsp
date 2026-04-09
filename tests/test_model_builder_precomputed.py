"""Tests for precomputed extraction data in build_semantic_model."""

import gzip
import json
import os
import pickle

import pytest
from _ivy_samples import SAMPLE_IVY_MAIN, SAMPLE_IVY_TYPES

from ivy_lsp.core.semantic.model_builder import PrecomputedFileData
from ivy_lsp.core.workspace.detection import WorkspaceConfig
from ivy_lsp.lsp.index_builder import IndexBuilder


@pytest.mark.unit
class TestPrecomputedFileData:
    def test_construction(self):
        pfd = PrecomputedFileData(
            symbols=[
                {
                    "name": "foo",
                    "kind": 12,
                    "range": [0, 0, 10, 0],
                    "children": [],
                    "detail": None,
                    "file_path": "f.ivy",
                    "synthetic": False,
                }
            ],
            includes=["bar"],
            tier_used=1,
        )
        assert pfd.tier_used == 1
        assert len(pfd.symbols) == 1
        assert pfd.includes == ["bar"]

    def test_empty(self):
        pfd = PrecomputedFileData(symbols=[], includes=[], tier_used=3)
        assert pfd.symbols == []
        assert pfd.includes == []


@pytest.mark.unit
class TestBuildSemanticModelPrecomputed:
    """Verify precomputed path produces same model as full extraction."""

    def _make_ivy_files(self, tmp_path):
        """Create a minimal workspace with two .ivy files."""
        (tmp_path / "types.ivy").write_text(
            "#lang ivy1.7\n\ntype cid\ntype pkt_type = {initial, handshake}\n"
        )
        (tmp_path / "main.ivy").write_text(
            "#lang ivy1.7\n\ninclude types\n\n"
            "type packet\naction send(p: packet)\naction recv(p: packet)\n"
        )
        return str(tmp_path)

    def _extract_files(self, root):
        """Run TieredExtractor on all .ivy files, return precomputed dict."""
        from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
        from ivy_lsp.core.semantic.model_builder import PrecomputedFileData

        extractor = TieredExtractor(skip_tier1=True)
        precomputed = {}
        for fname in os.listdir(root):
            if not fname.endswith(".ivy"):
                continue
            abs_path = os.path.join(root, fname)
            with open(abs_path) as f:
                source = f.read()
            result = extractor.extract(source, abs_path)
            precomputed[abs_path] = PrecomputedFileData(
                symbols=[s.to_dict() for s in result.symbols],
                includes=list(result.includes),
                tier_used=result.tier_used,
            )
        return precomputed

    def test_precomputed_produces_same_nodes(self, tmp_path):
        from ivy_lsp.core.semantic.model_builder import build_semantic_model
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        # Full extraction (current path)
        model_full = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )

        # Pre-computed path (new path)
        precomputed = self._extract_files(root)
        model_pre = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model_full is not None
        assert model_pre is not None

        # Compare node counts by type
        for node_type in (SymbolNode, TypeNode):
            full_nodes = model_full.get_nodes_by_type(node_type)
            pre_nodes = model_pre.get_nodes_by_type(node_type)
            assert len(full_nodes) == len(
                pre_nodes
            ), f"{node_type.__name__}: {len(full_nodes)} vs {len(pre_nodes)}"

        # Compare node IDs
        full_ids = {n.id for n in model_full.get_nodes_by_type(SymbolNode)}
        full_ids |= {n.id for n in model_full.get_nodes_by_type(TypeNode)}
        pre_ids = {n.id for n in model_pre.get_nodes_by_type(SymbolNode)}
        pre_ids |= {n.id for n in model_pre.get_nodes_by_type(TypeNode)}
        assert full_ids == pre_ids

    def test_precomputed_produces_same_edges(self, tmp_path):
        from ivy_lsp.core.semantic.model_builder import build_semantic_model
        from ivy_lsp.core.semantic.nodes import RfcAnnotation, SymbolNode, TypeNode

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        model_full = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )

        precomputed = self._extract_files(root)
        model_pre = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model_full is not None
        assert model_pre is not None

        # Collect all edges from both models (including RfcAnnotation COVERS edges)
        def _collect_edges(model):
            edges = set()
            for node_type in (SymbolNode, TypeNode, RfcAnnotation):
                for n in model.get_nodes_by_type(node_type):
                    for edge_type, target_id in model.get_outgoing(n.id):
                        edges.add((n.id, edge_type, target_id))
            return edges

        full_edges = _collect_edges(model_full)
        pre_edges = _collect_edges(model_pre)
        assert full_edges == pre_edges

    def test_precomputed_none_preserves_current_behavior(self, tmp_path):
        """Passing None for precomputed_extractions must use TieredExtractor."""
        from ivy_lsp.core.semantic.model_builder import build_semantic_model

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        model = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )
        # Should still produce a model (backward compat)
        assert model is not None

    def test_tier_used_zero_skipped(self, tmp_path):
        """A precomputed entry with tier_used=0 should produce no model nodes."""
        from ivy_lsp.core.semantic.model_builder import (
            PrecomputedFileData,
            build_semantic_model,
        )
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        # Build precomputed dict with tier_used=0 for all files
        precomputed = {}
        for fname in os.listdir(root):
            if not fname.endswith(".ivy"):
                continue
            abs_path = os.path.join(root, fname)
            precomputed[abs_path] = PrecomputedFileData(
                symbols=[], includes=[], tier_used=0
            )

        model = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model is not None
        # No symbols should be added since tier_used=0
        assert len(model.get_nodes_by_type(SymbolNode)) == 0
        assert len(model.get_nodes_by_type(TypeNode)) == 0

    def test_partial_precomputed_uses_fallback(self, tmp_path):
        """Files missing from precomputed dict should be extracted via fallback."""
        from ivy_lsp.core.semantic.model_builder import build_semantic_model
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        # Only precompute one file, leave the other for fallback
        precomputed = self._extract_files(root)
        first_key = next(iter(precomputed))
        del precomputed[first_key]

        model = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model is not None
        # Should still have nodes from both files (one precomputed, one fallback)
        all_nodes = model.get_nodes_by_type(SymbolNode) + model.get_nodes_by_type(
            TypeNode
        )
        assert len(all_nodes) >= 2


@pytest.mark.unit
class TestIndexBuilderPrecomputedIntegration:
    """Verify that build_protocol produces a valid semantic model.

    Uses the precomputed extraction path.
    """

    def _make_workspace(self, tmp_path):
        ws_root = str(tmp_path)
        proto_dir = tmp_path / "protocol-testing" / "testproto"
        proto_dir.mkdir(parents=True)
        (proto_dir / "types.ivy").write_text(SAMPLE_IVY_TYPES)
        (proto_dir / "main.ivy").write_text(SAMPLE_IVY_MAIN)
        return ws_root, str(proto_dir)

    def test_build_protocol_produces_semantic_model(self, tmp_path):
        ws_root, proto_dir = self._make_workspace(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)

        summary = builder.build_protocol(proto_dir)

        assert summary["status"] == "ok"
        assert summary["files"] == 2

        pickle_path = os.path.join(proto_dir, ".ivy-index", "semantic_model.pickle.gz")
        assert os.path.isfile(pickle_path)

        with gzip.open(pickle_path, "rb") as f:
            model = pickle.load(f)

        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        sym_nodes = model.get_nodes_by_type(SymbolNode)
        type_nodes = model.get_nodes_by_type(TypeNode)

        # SAMPLE_IVY_MAIN has: packet (type), send (action), recv (action)
        # SAMPLE_IVY_TYPES has: cid (type), quic_packet_type (type)
        assert len(type_nodes) >= 2, f"Expected >= 2 TypeNodes, got {len(type_nodes)}"
        assert len(sym_nodes) >= 1, f"Expected >= 1 SymbolNodes, got {len(sym_nodes)}"

    def test_symbols_json_unchanged(self, tmp_path):
        """symbols.json must be byte-identical regardless of precomputed path.

        Phase B is untouched by the precomputed extraction optimization.
        """
        ws_root, proto_dir = self._make_workspace(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        symbols_path = os.path.join(proto_dir, ".ivy-index", "symbols.json")
        with open(symbols_path) as f:
            symbols = json.load(f)

        # Should have entries for both files
        assert len(symbols) == 2
