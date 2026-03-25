"""Tests for WorkspaceIndexer scoping integration (Task 06)."""

import logging
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from ivy_lsp.analysis.requirement_graph import RequirementGraph
from ivy_lsp.analysis.test_scope import (
    ExportImportInfo,
    ScopedRequirementModel,
    TestScope,
)
from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(workspace_root="/fake/workspace"):
    """Create a WorkspaceIndexer with mocked parser and resolver."""
    parser = MagicMock()
    resolver = MagicMock()
    resolver.find_all_ivy_files.return_value = []
    resolver.resolve.return_value = None
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    return indexer, parser, resolver


def _make_parse_result(success=True, ast=None):
    """Create a mock parse result."""
    result = MagicMock()
    result.success = success
    result.ast = ast
    return result


def _make_export_import_info(filepath, exports=None, imports=None):
    """Create an ExportImportInfo with given exports/imports."""
    exports = exports or []
    imports = imports or []
    return ExportImportInfo(
        file=filepath,
        exports=exports,
        imports=imports,
        export_lines={e: i for i, e in enumerate(exports)},
        import_lines={im: i for i, im in enumerate(imports)},
    )


# ===================================================================
# TestInitializationWithScopedModel
# ===================================================================


class TestInitializationWithScopedModel:
    """Verify WorkspaceIndexer uses ScopedRequirementModel."""

    def test_requirement_graph_is_scoped_model(self):
        indexer, _, _ = _make_indexer()
        assert isinstance(indexer.requirement_graph, ScopedRequirementModel)

    def test_requirement_graph_is_still_a_requirement_graph(self):
        indexer, _, _ = _make_indexer()
        assert isinstance(indexer.requirement_graph, RequirementGraph)

    def test_file_export_imports_dict_exists(self):
        indexer, _, _ = _make_indexer()
        assert hasattr(indexer, "_file_export_imports")
        assert indexer._file_export_imports == {}

    def test_index_workspace_resets_to_fresh_scoped_model(self):
        indexer, _, resolver = _make_indexer()
        resolver.find_all_ivy_files.return_value = []
        indexer.requirement_graph._test_scopes["stale"] = "data"
        indexer.index_workspace()
        assert isinstance(indexer.requirement_graph, ScopedRequirementModel)
        assert indexer.requirement_graph.list_test_files() == []

    def test_index_workspace_clears_file_export_imports(self):
        indexer, _, resolver = _make_indexer()
        resolver.find_all_ivy_files.return_value = []
        indexer._file_export_imports["/old.ivy"] = _make_export_import_info("/old.ivy")
        indexer.index_workspace()
        assert indexer._file_export_imports == {}


# ===================================================================
# TestExportImportExtraction
# ===================================================================


class TestExportImportExtraction:
    """Verify _extract_file_exports_imports() delegates and stores correctly."""

    def test_full_mode_on_successful_parse(self):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=True, ast=MagicMock())
        filepath = "/fake/test.ivy"
        source = "export quic.send\n"
        expected = _make_export_import_info(filepath, exports=["quic.send"])

        with patch(
            "ivy_lsp.indexer.scope_manager.extract_exports_imports_full",
            return_value=expected,
        ) as mock_full:
            indexer._extract_file_exports_imports(filepath, result, source)

        mock_full.assert_called_once_with(result.ast, filepath, source)
        assert indexer._file_export_imports[filepath] is expected

    def test_light_mode_on_failed_parse(self):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=False)
        filepath = "/fake/test.ivy"
        source = "export quic.send\n"
        expected = _make_export_import_info(filepath, exports=["quic.send"])

        with patch(
            "ivy_lsp.indexer.scope_manager.extract_exports_imports_light",
            return_value=expected,
        ) as mock_light:
            indexer._extract_file_exports_imports(filepath, result, source)

        mock_light.assert_called_once_with(source, filepath)
        assert indexer._file_export_imports[filepath] is expected

    def test_exception_logged_not_raised(self, caplog):
        indexer, _, _ = _make_indexer()
        result = _make_parse_result(success=False)

        with patch(
            "ivy_lsp.indexer.scope_manager.extract_exports_imports_light",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING):
                indexer._extract_file_exports_imports("/bad.ivy", result, "bad")

        assert "/bad.ivy" not in indexer._file_export_imports
        assert "Export/import extraction failed" in caplog.text

    def test_called_during_index_single_file(self, tmp_path):
        """After _index_single_file, the file's ExportImportInfo is stored."""
        source = "export quic_send_event\ntype t\n"
        f = tmp_path / "test.ivy"
        f.write_text(source)
        filepath = str(f)

        indexer, parser, _ = _make_indexer(str(tmp_path))
        parser.parse.return_value = _make_parse_result(success=False)

        indexer._index_single_file(filepath)

        assert filepath in indexer._file_export_imports
        info = indexer._file_export_imports[filepath]
        assert "quic_send_event" in info.exports


# ===================================================================
# TestScopeComputation
# ===================================================================


class TestScopeComputation:
    """Verify _compute_test_scopes() builds correct TestScope objects."""

    def _setup_indexer_with_graph(self, file_infos, include_edges):
        """Set up indexer with pre-populated export info and include graph."""
        indexer, _, _ = _make_indexer()
        indexer._file_export_imports = dict(file_infos)
        for from_file, to_file in include_edges:
            indexer.include_graph.add_edge(from_file, to_file)
        return indexer

    def test_file_with_exports_gets_scope(self):
        test_file = "/ws/test.ivy"
        info = _make_export_import_info(test_file, exports=["quic.send"])
        indexer = self._setup_indexer_with_graph({test_file: info}, [])

        indexer._compute_test_scopes()

        assert indexer.requirement_graph.has_test_scope(test_file)

    def test_file_without_exports_gets_no_scope(self):
        lib_file = "/ws/lib.ivy"
        info = _make_export_import_info(lib_file)  # no exports
        indexer = self._setup_indexer_with_graph({lib_file: info}, [])

        indexer._compute_test_scopes()

        assert not indexer.requirement_graph.has_test_scope(lib_file)

    def test_include_closure_contains_self_and_transitive(self):
        test_f = "/ws/test.ivy"
        mid_f = "/ws/mid.ivy"
        base_f = "/ws/base.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_f: _make_export_import_info(test_f, exports=["quic.send"]),
                mid_f: _make_export_import_info(mid_f),
                base_f: _make_export_import_info(base_f),
            },
            [(test_f, mid_f), (mid_f, base_f)],
        )

        indexer._compute_test_scopes()

        scope = indexer.requirement_graph.get_test_scope(test_f)
        assert scope.include_closure == frozenset({test_f, mid_f, base_f})

    def test_exports_unioned_across_closure(self):
        test_f = "/ws/test.ivy"
        helper_f = "/ws/helper.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_f: _make_export_import_info(test_f, exports=["quic.send"]),
                helper_f: _make_export_import_info(helper_f, exports=["quic.recv"]),
            },
            [(test_f, helper_f)],
        )

        indexer._compute_test_scopes()

        scope = indexer.requirement_graph.get_test_scope(test_f)
        assert scope.exported_actions == frozenset({"quic.send", "quic.recv"})

    def test_imports_unioned_across_closure(self):
        test_f = "/ws/test.ivy"
        helper_f = "/ws/helper.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_f: _make_export_import_info(
                    test_f, exports=["quic.send"], imports=["tls.handshake"]
                ),
                helper_f: _make_export_import_info(
                    helper_f, imports=["quic.connection"]
                ),
            },
            [(test_f, helper_f)],
        )

        indexer._compute_test_scopes()

        scope = indexer.requirement_graph.get_test_scope(test_f)
        assert scope.imported_actions == frozenset({"tls.handshake", "quic.connection"})

    def test_role_from_server_behavior_file(self):
        test_f = "/ws/test.ivy"
        behavior_f = "/ws/quic_server_behavior.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_f: _make_export_import_info(test_f, exports=["quic.send"]),
                behavior_f: _make_export_import_info(behavior_f),
            },
            [(test_f, behavior_f)],
        )

        indexer._compute_test_scopes()

        assert indexer.requirement_graph.get_test_scope(test_f).tester_role == "client"

    def test_unknown_role_without_behavior_file(self):
        test_f = "/ws/test.ivy"
        indexer = self._setup_indexer_with_graph(
            {test_f: _make_export_import_info(test_f, exports=["quic.send"])},
            [],
        )

        indexer._compute_test_scopes()

        assert indexer.requirement_graph.get_test_scope(test_f).tester_role == "unknown"

    def test_diamond_include_shape(self):
        """Test -> A, test -> B, A -> shared, B -> shared."""
        test_f = "/ws/test.ivy"
        a_f = "/ws/a.ivy"
        b_f = "/ws/b.ivy"
        shared_f = "/ws/shared.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_f: _make_export_import_info(test_f, exports=["quic.send"]),
                a_f: _make_export_import_info(a_f, exports=["quic.recv"]),
                b_f: _make_export_import_info(b_f, imports=["tls.hs"]),
                shared_f: _make_export_import_info(
                    shared_f, exports=["quic.close"], imports=["quic.open"]
                ),
            },
            [(test_f, a_f), (test_f, b_f), (a_f, shared_f), (b_f, shared_f)],
        )

        indexer._compute_test_scopes()

        scope = indexer.requirement_graph.get_test_scope(test_f)
        assert scope.include_closure == frozenset({test_f, a_f, b_f, shared_f})
        assert scope.exported_actions == frozenset(
            {"quic.send", "quic.recv", "quic.close"}
        )
        assert scope.imported_actions == frozenset({"tls.hs", "quic.open"})

    def test_empty_workspace_no_scopes(self):
        indexer = self._setup_indexer_with_graph({}, [])
        indexer._compute_test_scopes()
        assert indexer.requirement_graph.list_test_files() == []

    def test_file_in_closure_but_not_in_export_map(self):
        """File in include graph but missing from _file_export_imports (e.g. deleted)."""
        test_f = "/ws/test.ivy"
        missing_f = "/ws/deleted.ivy"
        indexer = self._setup_indexer_with_graph(
            {test_f: _make_export_import_info(test_f, exports=["quic.send"])},
            [(test_f, missing_f)],
        )

        indexer._compute_test_scopes()

        scope = indexer.requirement_graph.get_test_scope(test_f)
        assert missing_f in scope.include_closure
        assert scope.exported_actions == frozenset({"quic.send"})

    def test_multiple_test_files_each_get_scope(self):
        test_a = "/ws/test_a.ivy"
        test_b = "/ws/test_b.ivy"
        shared = "/ws/shared.ivy"
        indexer = self._setup_indexer_with_graph(
            {
                test_a: _make_export_import_info(test_a, exports=["quic.send"]),
                test_b: _make_export_import_info(test_b, exports=["quic.recv"]),
                shared: _make_export_import_info(shared),
            },
            [(test_a, shared), (test_b, shared)],
        )

        indexer._compute_test_scopes()

        test_files = indexer.requirement_graph.list_test_files()
        assert test_a in test_files
        assert test_b in test_files
        assert shared not in test_files

    def test_called_after_wire_coverage_in_index_workspace(self):
        indexer, _, resolver = _make_indexer()
        resolver.find_all_ivy_files.return_value = []
        call_order = []

        orig_wire = indexer._wire_coverage_edges

        def track_wire():
            call_order.append("wire_coverage")
            orig_wire()

        def track_compute():
            call_order.append("compute_scopes")

        with patch.object(indexer, "_wire_coverage_edges", track_wire):
            with patch.object(indexer, "_compute_test_scopes", track_compute):
                indexer.index_workspace()

        assert call_order.index("wire_coverage") < call_order.index("compute_scopes")


# ===================================================================
# TestReindexInvalidation
# ===================================================================


class TestReindexInvalidation:
    """Verify reindex_file invalidates caches and recomputes scopes."""

    def test_reindex_clears_scope_cache_for_affected_tests(self):
        """Scope cache entries for tests including the reindexed file are cleared."""
        indexer, _, _ = _make_indexer()
        target = os.path.abspath("/fake/workspace/shared.ivy")
        test_f = os.path.abspath("/fake/workspace/test.ivy")

        scope = TestScope(
            test_file=test_f,
            include_closure=frozenset({test_f, target}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        indexer.requirement_graph.register_test_scope(scope)
        indexer.requirement_graph.get_scoped_requirements(test_f)
        assert (test_f, False) in indexer.requirement_graph._scope_cache

        with patch.object(indexer, "_index_single_file", return_value=[]):
            indexer.reindex_file(target)

        assert (test_f, False) not in indexer.requirement_graph._scope_cache

    def test_reindex_removes_old_export_import_info(self):
        indexer, _, _ = _make_indexer()
        target = os.path.abspath("/fake/workspace/test.ivy")
        indexer._file_export_imports[target] = _make_export_import_info(
            target, exports=["old.action"]
        )

        with patch.object(indexer, "_index_single_file", return_value=[]):
            indexer.reindex_file(target)

        assert target not in indexer._file_export_imports

    def test_reindex_preserves_other_file_export_imports(self):
        indexer, _, _ = _make_indexer()
        target = os.path.abspath("/fake/workspace/target.ivy")
        other = os.path.abspath("/fake/workspace/other.ivy")
        indexer._file_export_imports[target] = _make_export_import_info(target)
        indexer._file_export_imports[other] = _make_export_import_info(
            other, exports=["keep.this"]
        )

        with patch.object(indexer, "_index_single_file", return_value=[]):
            indexer.reindex_file(target)

        assert other in indexer._file_export_imports
        assert indexer._file_export_imports[other].exports == ["keep.this"]

    def test_reindex_calls_compute_test_scopes(self):
        indexer, _, _ = _make_indexer()

        with patch.object(indexer, "_index_single_file", return_value=[]):
            with patch.object(indexer, "_compute_test_scopes") as mock_compute:
                indexer.reindex_file("/fake/workspace/file.ivy")

        mock_compute.assert_called_once()


# ===================================================================
# TestInlineT1T2DuringDeepIndex
# ===================================================================


class TestInlineT1T2DuringDeepIndex:
    """Verify inline T1/T2 analysis during _deep_index_serial()."""

    def test_set_analysis_pipeline_stored(self):
        """set_analysis_pipeline() stores the pipeline reference."""
        indexer, _, _ = _make_indexer()
        assert indexer._analysis_pipeline is None
        mock_pipeline = MagicMock()
        indexer.set_analysis_pipeline(mock_pipeline)
        assert indexer._analysis_pipeline is mock_pipeline

    def test_inline_t1_t2_t3_called_on_successful_parse(self, tmp_path):
        """When pipeline is set and parse succeeds, T1+T2+T3 are called inline."""
        source = "type cid\nexport quic_send_event\n"
        f = tmp_path / "test.ivy"
        f.write_text(source)

        indexer, parser, _ = _make_indexer(str(tmp_path))

        result = _make_parse_result(success=True, ast=MagicMock())
        result.errors = []
        parser.parse.return_value = result

        mock_pipeline = MagicMock()
        indexer.set_analysis_pipeline(mock_pipeline)

        indexer._deep_index_serial([str(f)])

        mock_pipeline.run_tier1.assert_called_once_with(source, str(f))
        mock_pipeline.run_tier2.assert_called_once_with(
            source,
            str(f),
            parse_result=result,
            rfc_annotations=mock_pipeline.run_tier1.return_value,
        )
        mock_pipeline.run_tier3_background.assert_called_once_with(
            source, str(f), track_state=False
        )

    def test_inline_t1_only_on_failed_parse(self, tmp_path):
        """When parse fails but pipeline is set, only T1 (regex) is called."""
        source = "broken syntax !!!\n"
        f = tmp_path / "bad.ivy"
        f.write_text(source)

        indexer, parser, _ = _make_indexer(str(tmp_path))

        result = _make_parse_result(success=False)
        result.ast = None
        result.errors = []
        parser.parse.return_value = result

        mock_pipeline = MagicMock()
        indexer.set_analysis_pipeline(mock_pipeline)

        indexer._deep_index_serial([str(f)])

        mock_pipeline.run_tier1.assert_called_once_with(source, str(f))
        mock_pipeline.run_tier2.assert_not_called()
        mock_pipeline.run_tier3_background.assert_not_called()

    def test_no_pipeline_no_inline_calls(self, tmp_path):
        """When no pipeline is set, _deep_index_serial works without T1/T2."""
        source = "type cid\nexport quic_send_event\n"
        f = tmp_path / "test.ivy"
        f.write_text(source)

        indexer, parser, _ = _make_indexer(str(tmp_path))
        result = _make_parse_result(success=True, ast=MagicMock())
        result.errors = []
        parser.parse.return_value = result

        # No pipeline set — should not raise
        indexer._deep_index_serial([str(f)])
        assert indexer._analysis_pipeline is None

    def test_inline_t1_t2_exception_does_not_crash_deep_index(self, tmp_path):
        """If T1/T2 raises, deep indexing continues without crashing."""
        source = "type cid\nexport quic_send_event\n"
        f = tmp_path / "test.ivy"
        f.write_text(source)

        indexer, parser, _ = _make_indexer(str(tmp_path))
        result = _make_parse_result(success=True, ast=MagicMock())
        result.errors = []
        parser.parse.return_value = result

        mock_pipeline = MagicMock()
        mock_pipeline.run_tier1.side_effect = RuntimeError("T1 boom")
        indexer.set_analysis_pipeline(mock_pipeline)

        # Should not raise
        indexer._deep_index_serial([str(f)])

    def test_inline_t1_exception_on_failed_parse_does_not_crash(self, tmp_path):
        """If T1 raises on a failed-parse file, deep indexing continues."""
        source = "broken\n"
        f = tmp_path / "bad.ivy"
        f.write_text(source)

        indexer, parser, _ = _make_indexer(str(tmp_path))
        result = _make_parse_result(success=False)
        result.ast = None
        result.errors = []
        parser.parse.return_value = result

        mock_pipeline = MagicMock()
        mock_pipeline.run_tier1.side_effect = RuntimeError("T1 boom")
        indexer.set_analysis_pipeline(mock_pipeline)

        # Should not raise
        indexer._deep_index_serial([str(f)])
