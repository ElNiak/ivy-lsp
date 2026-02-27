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
