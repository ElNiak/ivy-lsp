"""Tests for parallel deep indexing worker functions."""
from ivy_lsp.indexer.parallel_indexer import WorkerResult, worker_parse_file


class TestWorkerResult:
    def test_dataclass_fields(self):
        r = WorkerResult(
            filepath="/tmp/a.ivy", success=True,
            symbols=[{"name": "t"}], errors=[], includes=["base"],
        )
        assert r.filepath == "/tmp/a.ivy"
        assert r.success is True
        assert r.symbols == [{"name": "t"}]


class TestWorkerParseFile:
    def test_fallback_on_parse_failure(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        # Without Ivy installed, worker falls back to fallback_scan
        result = worker_parse_file(str(f))
        assert isinstance(result, WorkerResult)
        assert result.filepath == str(f)
        assert len(result.symbols) >= 1


class TestParallelDeepIndexer:
    def test_parse_files_returns_results(self, tmp_path):
        from ivy_lsp.indexer.parallel_indexer import ParallelDeepIndexer

        files = []
        for i in range(3):
            f = tmp_path / f"test{i}.ivy"
            f.write_text(f"#lang ivy1.7\ntype t{i}\n")
            files.append(str(f))

        indexer = ParallelDeepIndexer(num_workers=2)
        results = indexer.parse_files(files)
        assert len(results) == 3
        for filepath, result in results.items():
            assert result.filepath == filepath
            assert len(result.symbols) >= 1

    def test_serial_fallback_for_single_file(self, tmp_path):
        from ivy_lsp.indexer.parallel_indexer import ParallelDeepIndexer

        f = tmp_path / "single.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        indexer = ParallelDeepIndexer(num_workers=2)
        results = indexer.parse_files([str(f)])
        assert len(results) == 1
