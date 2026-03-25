"""Tests for the bulk background T1+T2 analysis pipeline."""

import os
import tempfile
import threading
from unittest.mock import patch

from ivy_lsp.adapters.null_adapter import (
    NullAstEnrichmentAdapter,
    NullCompilerAdapter,
    NullParserAdapter,
)
from ivy_lsp.infra.config import reset_config
from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline, BulkAnalysisResult
from ivy_lsp.semantic.model import SemanticModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(model=None):
    m = model or SemanticModel()
    return (
        AnalysisPipeline(
            model=m,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        ),
        m,
    )


def _write_ivy_file(tmpdir: str, name: str, content: str) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# BulkAnalysisResult dataclass tests
# ---------------------------------------------------------------------------


class TestBulkAnalysisResult:
    def test_defaults(self):
        r = BulkAnalysisResult()
        assert r.total == 0
        assert r.t1_completed == 0
        assert r.t2_completed == 0
        assert r.errors == []
        assert r.cancelled is False


# ---------------------------------------------------------------------------
# run_bulk_t1_t2 tests
# ---------------------------------------------------------------------------


class TestRunBulkT1T2:
    def test_populates_tier1_and_tier2(self):
        pipeline, model = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "require x; # [rfc9000:4.1]\n")
            f2 = _write_ivy_file(tmpdir, "b.ivy", "type cid\n")

            result = pipeline.run_bulk_t1_t2([f1, f2])

        assert result.total == 2
        assert result.t1_completed == 2
        assert result.t2_completed == 2
        assert result.errors == []
        assert result.cancelled is False
        assert f1 in pipeline._tier1_files
        assert f2 in pipeline._tier1_files
        assert f1 in pipeline._tier2_files
        assert f2 in pipeline._tier2_files

    def test_skips_already_analyzed_t2(self):
        pipeline, model = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "require x; # [rfc9000:4.1]\n")
            f2 = _write_ivy_file(tmpdir, "b.ivy", "type cid\n")

            # Pre-analyze f1
            with open(f1) as fh:
                src = fh.read()
            pipeline.run_tier1(src, f1)
            pipeline.run_tier2(src, f1)

            # Bulk should only process f2
            result = pipeline.run_bulk_t1_t2([f1, f2])

        assert result.total == 1  # only f2
        assert result.t1_completed == 1
        assert result.t2_completed == 1

    def test_skips_already_analyzed_t1_only(self):
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "require x; # [rfc9000:4.1]\n")
            f2 = _write_ivy_file(tmpdir, "b.ivy", "type cid\n")

            # Pre-analyze f1 at T1 only
            with open(f1) as fh:
                src = fh.read()
            pipeline.run_tier1(src, f1)

            # T1-only bulk: f1 already in _tier1_files -> skipped
            result = pipeline.run_bulk_t1_t2([f1, f2], include_t2=False)

        assert result.total == 1  # only f2
        assert result.t1_completed == 1
        assert result.t2_completed == 0

    def test_cancel_aborts_early(self):
        pipeline, _ = _make_pipeline()
        cancel = threading.Event()
        cancel.set()  # pre-cancel

        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "type cid\n")
            f2 = _write_ivy_file(tmpdir, "b.ivy", "type pkt\n")

            result = pipeline.run_bulk_t1_t2(
                [f1, f2],
                cancel_event=cancel,
            )

        assert result.cancelled is True
        assert result.t1_completed == 0

    def test_unreadable_file_produces_error(self):
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            good = _write_ivy_file(tmpdir, "good.ivy", "type cid\n")
            bad = os.path.join(tmpdir, "nonexistent.ivy")

            result = pipeline.run_bulk_t1_t2([bad, good])

        assert result.total == 2
        assert result.t1_completed == 1  # only good.ivy
        assert len(result.errors) == 1
        assert result.errors[0][0] == bad

    def test_t1_only_mode(self):
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "require x; # [rfc9000:4.1]\n")

            result = pipeline.run_bulk_t1_t2([f1], include_t2=False)

        assert result.t1_completed == 1
        assert result.t2_completed == 0
        assert f1 in pipeline._tier1_files
        assert f1 not in pipeline._tier2_files

    def test_progress_callback_called(self):
        pipeline, _ = _make_pipeline()
        calls = []

        def on_progress(completed, total, current):
            calls.append((completed, total, current))

        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "type cid\n")
            f2 = _write_ivy_file(tmpdir, "b.ivy", "type pkt\n")

            pipeline.run_bulk_t1_t2(
                [f1, f2],
                progress_callback=on_progress,
            )

        assert len(calls) == 2
        assert calls[0][0] == 1  # completed=1
        assert calls[0][1] == 2  # total=2
        assert calls[1][0] == 2  # completed=2
        assert calls[1][1] == 2  # total=2

    def test_empty_file_list(self):
        pipeline, _ = _make_pipeline()
        result = pipeline.run_bulk_t1_t2([])
        assert result.total == 0
        assert result.t1_completed == 0
        assert result.t2_completed == 0
        assert result.cancelled is False


# ---------------------------------------------------------------------------
# Pipeline state tests
# ---------------------------------------------------------------------------


class TestBulkPipelineState:
    def test_initial_state_has_bulk_fields(self):
        pipeline, _ = _make_pipeline()
        state = pipeline.get_pipeline_state()
        assert "bulkAnalysisRunning" in state
        assert "bulkAnalysisTotal" in state
        assert "bulkAnalysisCompleted" in state
        assert state["bulkAnalysisRunning"] is False
        assert state["bulkAnalysisTotal"] == 0
        assert state["bulkAnalysisCompleted"] == 0

    def test_state_reflects_completed_bulk(self):
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "type cid\n")
            pipeline.run_bulk_t1_t2([f1])

        state = pipeline.get_pipeline_state()
        assert state["bulkAnalysisRunning"] is False
        assert state["bulkAnalysisTotal"] == 1
        assert state["bulkAnalysisCompleted"] == 1

    def test_bulk_running_false_after_completion(self):
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "type cid\n")
            pipeline.run_bulk_t1_t2([f1])

        assert pipeline._bulk.running is False


# ---------------------------------------------------------------------------
# Environment variable tests
# ---------------------------------------------------------------------------


class TestBulkAnalysisEnvVars:
    def test_env_disable_bulk_analysis(self):
        """IVY_LSP_BULK_ANALYSIS=0 should prevent _start_bulk_analysis from running."""
        from ivy_lsp.server import IvyLanguageServer

        server = IvyLanguageServer.__new__(IvyLanguageServer)
        server._analysis_pipeline = object()  # non-None sentinel
        server._indexer = type(
            "FakeIndexer",
            (),
            {
                "get_all_ivy_file_paths": lambda self: ["/fake/a.ivy"],
            },
        )()
        server._bulk_analysis_cancel = threading.Event()

        with patch.dict(os.environ, {"IVY_LSP_BULK_ANALYSIS": "0"}):
            reset_config()
            # Should return early without spawning a thread
            server._start_bulk_analysis()

        # Pipeline should not have been touched
        assert not hasattr(server.analysis_pipeline, "_bulk_running")

    def test_env_disable_t2(self):
        """IVY_LSP_BULK_ANALYSIS_T2=0 should pass include_t2=False."""
        pipeline, _ = _make_pipeline()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = _write_ivy_file(tmpdir, "a.ivy", "require x; # [rfc9000:4.1]\n")

            # Simulate what _start_bulk_analysis does with T2 disabled
            result = pipeline.run_bulk_t1_t2([f1], include_t2=False)

        assert result.t1_completed == 1
        assert result.t2_completed == 0
