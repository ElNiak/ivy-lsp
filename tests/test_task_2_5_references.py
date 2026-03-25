"""Tests for Task 2.5: Find References."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import Position

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestReferencesImport:
    def test_import(self):
        from ivy_lsp.features.references import find_references

        assert find_references is not None


class TestFindReferences:
    def test_finds_references_in_same_file(self, tmp_path):
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.features.references import find_references

        source = "#lang ivy1.7\n\ntype cid\nrelation uses(X:cid, Y:cid)\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()
        lines = source.split("\n")
        pos = Position(line=2, character=5)
        results = find_references(indexer, str(tmp_path / "a.ivy"), pos, lines)
        assert len(results) >= 1

    def test_finds_references_across_files(self, tmp_path):
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.features.references import find_references

        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (tmp_path / "user.ivy").write_text(
            "#lang ivy1.7\ninclude types\nrelation r(X:cid)\n"
        )
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()
        lines = (tmp_path / "types.ivy").read_text().split("\n")
        pos = Position(line=1, character=5)
        results = find_references(indexer, str(tmp_path / "types.ivy"), pos, lines)
        uris = [r.uri for r in results]
        assert any("types.ivy" in u for u in uris)
        assert any("user.ivy" in u for u in uris)

    def test_unknown_symbol_returns_empty(self, tmp_path):
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.features.references import find_references

        (tmp_path / "a.ivy").write_text("#lang ivy1.7\ntype t\n")
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()
        lines = ["# xyznonexistent"]
        pos = Position(line=0, character=3)
        results = find_references(indexer, str(tmp_path / "a.ivy"), pos, lines)
        assert results == []


class TestLayerScopedReferences:
    """References should be scoped to the queried file's layer + upstream deps."""

    def _make_layered_workspace(self, tmp_path):
        """Create a workspace with quic and apt layers (apt depends_on quic)."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.core.workspace.detection import WorkspaceLayer
        from ivy_lsp.features.references import find_references

        quic_dir = tmp_path / "quic"
        quic_dir.mkdir()
        apt_dir = tmp_path / "apt"
        apt_dir.mkdir()

        (quic_dir / "types.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (quic_dir / "frame.ivy").write_text(
            "#lang ivy1.7\ninclude types\nrelation uses_cid(X:cid)\n"
        )
        (apt_dir / "attack.ivy").write_text(
            "#lang ivy1.7\ninclude types\nrelation forges_cid(X:cid)\n"
        )

        layers = [
            WorkspaceLayer(id="quic", include_paths=["quic"], priority=1),
            WorkspaceLayer(
                id="apt", include_paths=["apt"], priority=2, depends_on=["quic"]
            ),
        ]
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        # Build staging so _file_to_layer and _layer_by_id get populated.
        resolver.create_staging_directory()
        resolver.build_layered_staging()
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()
        return indexer, find_references

    def test_quic_layer_excludes_apt_references(self, tmp_path):
        indexer, find_references = self._make_layered_workspace(tmp_path)
        quic_types = str(tmp_path / "quic" / "types.ivy")
        lines = Path(quic_types).read_text().split("\n")
        pos = Position(line=1, character=5)  # "cid"
        results = find_references(indexer, quic_types, pos, lines)
        uris = [r.uri for r in results]
        assert any("types.ivy" in u for u in uris)
        assert any("frame.ivy" in u for u in uris)
        assert not any(
            "attack.ivy" in u for u in uris
        ), "References from downstream apt layer should be excluded"

    def test_apt_layer_includes_quic_references(self, tmp_path):
        indexer, find_references = self._make_layered_workspace(tmp_path)
        apt_attack = str(tmp_path / "apt" / "attack.ivy")
        lines = Path(apt_attack).read_text().split("\n")
        pos = Position(line=2, character=22)  # "cid" in "forges_cid(X:cid)"
        results = find_references(indexer, apt_attack, pos, lines)
        uris = [r.uri for r in results]
        assert any("attack.ivy" in u for u in uris)
        # apt depends_on quic, so quic files should be visible
        assert any("types.ivy" in u for u in uris)
        assert any("frame.ivy" in u for u in uris)

    def test_no_layers_returns_all_files(self, tmp_path):
        """Without workspace layers, all files are searched (backward compat)."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
        from ivy_lsp.features.references import find_references

        d1 = tmp_path / "d1"
        d1.mkdir()
        d2 = tmp_path / "d2"
        d2.mkdir()
        (d1 / "a.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (d2 / "b.ivy").write_text("#lang ivy1.7\nrelation r(X:cid)\n")
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()
        lines = (d1 / "a.ivy").read_text().split("\n")
        pos = Position(line=1, character=5)
        results = find_references(indexer, str(d1 / "a.ivy"), pos, lines)
        uris = [r.uri for r in results]
        assert any("a.ivy" in u for u in uris)
        assert any("b.ivy" in u for u in uris)


class TestReferencesNotBlocking:
    """H4: references handler must use run_in_executor for file I/O."""

    def test_references_handler_uses_executor(self):
        """Structural: verify the handler uses run_in_executor."""
        import inspect

        from ivy_lsp.features import references as refs_mod

        source = inspect.getsource(refs_mod.register)
        assert (
            "run_in_executor" in source
        ), "references handler must use run_in_executor to avoid blocking event loop"
