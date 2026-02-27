"""Tests for incremental dirty-only re-indexing."""
from unittest.mock import MagicMock, patch

from ivy_lsp.analysis.test_scope import ExportImportInfo
from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer


class TestReindexFileWithDependents:
    def _make_indexer(self, tmp_path, files_dict):
        for name, content in files_dict.items():
            (tmp_path / name).write_text(content)
        parser = MagicMock()
        parser.parse.return_value = MagicMock(success=False, ast=None, errors=[])
        resolver = MagicMock()
        resolver.find_all_ivy_files.return_value = [
            str(tmp_path / n) for n in files_dict
        ]
        resolver.resolve.return_value = None
        return WorkspaceIndexer(str(tmp_path), parser, resolver)

    def test_reindex_propagates_to_direct_dependents(self, tmp_path):
        idx = self._make_indexer(tmp_path, {
            "base.ivy": "type t",
            "uses_base.ivy": "include base\ntype u",
            "unrelated.ivy": "type v",
        })
        idx._include_graph.add_edge(
            str(tmp_path / "uses_base.ivy"), str(tmp_path / "base.ivy"),
        )
        idx._fast_index_all_files()

        with patch.object(
            idx, "_index_single_file", wraps=idx._index_single_file,
        ) as mock_index:
            idx.reindex_file_with_dependents(str(tmp_path / "base.ivy"))

        indexed_files = {c.args[0] for c in mock_index.call_args_list}
        assert str(tmp_path / "base.ivy") in indexed_files
        assert str(tmp_path / "uses_base.ivy") in indexed_files
        assert str(tmp_path / "unrelated.ivy") not in indexed_files

    def test_reindex_transitive_dependents(self, tmp_path):
        idx = self._make_indexer(tmp_path, {
            "a.ivy": "type a",
            "b.ivy": "include a\ntype b",
            "c.ivy": "include b\ntype c",
        })
        idx._include_graph.add_edge(str(tmp_path / "b.ivy"), str(tmp_path / "a.ivy"))
        idx._include_graph.add_edge(str(tmp_path / "c.ivy"), str(tmp_path / "b.ivy"))
        idx._fast_index_all_files()

        with patch.object(
            idx, "_index_single_file", wraps=idx._index_single_file,
        ) as mock_index:
            idx.reindex_file_with_dependents(str(tmp_path / "a.ivy"))

        indexed_files = {c.args[0] for c in mock_index.call_args_list}
        assert str(tmp_path / "a.ivy") in indexed_files
        assert str(tmp_path / "b.ivy") in indexed_files
        assert str(tmp_path / "c.ivy") in indexed_files
