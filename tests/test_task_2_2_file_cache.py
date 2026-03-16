"""Tests for Task 2.2: File Cache."""

import sys
import time
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestFileCacheImport:
    def test_import(self):
        from ivy_lsp.indexer.file_cache import CachedFile, FileCache

        assert FileCache is not None
        assert CachedFile is not None


class TestFileCachePutGet:
    def test_put_and_get_returns_cached(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        cache = FileCache()
        cache.put(str(f), result="ast_a", symbols=["sym_a"])
        entry = cache.get(str(f))
        assert entry is not None
        assert entry.parse_result == "ast_a"
        assert entry.symbols == ["sym_a"]

    def test_get_nonexistent_returns_none(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        cache = FileCache()
        assert cache.get(str(tmp_path / "nope.ivy")) is None


class TestFileCacheMtimeInvalidation:
    def test_stale_mtime_returns_none(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("v1")
        cache = FileCache()
        cache.put(str(f), result="v1", symbols=[])
        assert cache.get(str(f)) is not None
        time.sleep(0.05)
        f.write_text("v2")
        assert cache.get(str(f)) is None


class TestFileCacheInvalidate:
    def test_invalidate_removes_entry(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("v1")
        cache = FileCache()
        cache.put(str(f), result="v1", symbols=[])
        cache.invalidate(str(f))
        assert cache.get(str(f)) is None

    def test_invalidate_nonexistent_no_error(self):
        from ivy_lsp.indexer.file_cache import FileCache

        cache = FileCache()
        cache.invalidate("/no/such/file.ivy")


class TestFileCacheInvalidateDependents:
    def test_invalidate_cascades_to_dependents(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache
        from ivy_lsp.parsing.symbols import IncludeGraph

        a, b, c = (tmp_path / f for f in ("a.ivy", "b.ivy", "c.ivy"))
        for f in (a, b, c):
            f.write_text("#lang ivy1.7\n")
        cache = FileCache()
        for f, name in ((a, "a"), (b, "b"), (c, "c")):
            cache.put(str(f), result=name, symbols=[])
        graph = IncludeGraph()
        graph.add_edge(str(b), str(a))  # b includes a
        graph.add_edge(str(c), str(a))  # c includes a
        cache.invalidate_dependents(str(a), graph)
        assert cache.get(str(a)) is not None  # a itself NOT invalidated
        assert cache.get(str(b)) is None  # b invalidated
        assert cache.get(str(c)) is None  # c invalidated


class TestFileCacheLRU:
    def test_lru_eviction_at_max_size(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        cache = FileCache(max_size=3)
        files = []
        for i in range(4):
            f = tmp_path / f"{i}.ivy"
            f.write_text(f"v{i}")
            files.append(f)
            cache.put(str(f), result=f"r{i}", symbols=[])
        assert cache.get(str(files[0])) is None  # evicted (LRU)
        assert cache.get(str(files[1])) is not None
        assert cache.get(str(files[3])) is not None

    def test_get_refreshes_lru_order(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        cache = FileCache(max_size=3)
        files = []
        for i in range(3):
            f = tmp_path / f"{i}.ivy"
            f.write_text(f"v{i}")
            files.append(f)
            cache.put(str(f), result=f"r{i}", symbols=[])
        cache.get(str(files[0]))  # refresh file 0
        f3 = tmp_path / "3.ivy"
        f3.write_text("v3")
        cache.put(str(f3), result="r3", symbols=[])
        assert cache.get(str(files[0])) is not None  # refreshed, not evicted
        assert cache.get(str(files[1])) is None  # evicted


class TestFileCacheIncludes:
    def test_cached_file_stores_includes(self, tmp_path):
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\n")
        cache = FileCache()
        cache.put(
            str(f),
            result="ast",
            symbols=[],
            includes=["quic_types", "collections"],
        )
        entry = cache.get(str(f))
        assert entry is not None
        assert entry.includes == ["quic_types", "collections"]


class TestFileCacheStatUnderLock:
    """C3: os.path.getmtime must be called inside the lock."""

    def test_put_acquires_lock_before_stat(self, tmp_path):
        import threading
        import unittest.mock

        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "test.ivy"
        f.write_text("#lang ivy1.7")
        cache = FileCache(max_size=10)

        call_order = []

        class OrderTrackingLock:
            """Wrapper to track acquire/release ordering."""

            def __init__(self, real_lock):
                self._real = real_lock

            def acquire(self, *a, **kw):
                call_order.append("lock_acquire")
                return self._real.acquire(*a, **kw)

            def release(self):
                call_order.append("lock_release")
                return self._real.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

        import os

        original_getmtime = os.path.getmtime

        def tracked_getmtime(path):
            call_order.append("getmtime")
            return original_getmtime(path)

        cache._lock = OrderTrackingLock(threading.Lock())
        with unittest.mock.patch(
            "ivy_lsp.indexer.file_cache.os.path.getmtime", tracked_getmtime
        ):
            cache.put(str(f), None, [])

        # getmtime must appear AFTER lock_acquire
        lock_idx = call_order.index("lock_acquire")
        mtime_idx = call_order.index("getmtime")
        assert mtime_idx > lock_idx, (
            f"getmtime at index {mtime_idx} must come after lock_acquire "
            f"at {lock_idx}; order was: {call_order}"
        )


class TestCachedFileExtended:
    """CachedFile should store requirements, writes, and export_import_info."""

    def test_cache_stores_requirements(self):
        """CachedFile should store requirements alongside symbols."""
        from ivy_lsp.analysis.requirement_graph import RequirementNode
        from ivy_lsp.indexer.file_cache import CachedFile

        req = RequirementNode(
            id="test:5",
            kind="require",
            formula_text="x>0",
            line=5,
            col=0,
            file="test.ivy",
            monitor_action="act",
            mixin_kind="before",
        )
        entry = CachedFile(
            filepath="test.ivy",
            mtime=1234.0,
            parse_result=None,
            symbols=[],
            includes=[],
            requirements=[req],
            writes=[("act", "x", 5)],
            export_import_info={"exports": [], "imports": []},
        )
        assert len(entry.requirements) == 1
        assert entry.requirements[0].kind == "require"
        assert len(entry.writes) == 1
        assert entry.export_import_info is not None

    def test_cache_defaults_empty(self):
        """New fields default to empty when not provided."""
        from ivy_lsp.indexer.file_cache import CachedFile

        entry = CachedFile(
            filepath="test.ivy",
            mtime=1234.0,
            parse_result=None,
            symbols=[],
        )
        assert entry.requirements == []
        assert entry.writes == []
        assert entry.export_import_info is None

    def test_put_stores_requirements(self, tmp_path):
        """FileCache.put should accept and store requirement data."""
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        cache = FileCache()
        reqs = [{"kind": "require", "formula": "x>0"}]
        writes = [("act", "x", 5)]
        info = {"exports": ["foo"], "imports": ["bar"]}
        cache.put(
            str(f),
            result=None,
            symbols=[],
            requirements=reqs,
            writes=writes,
            export_import_info=info,
        )
        entry = cache.get(str(f))
        assert entry is not None
        assert entry.requirements == reqs
        assert entry.writes == writes
        assert entry.export_import_info == info

    def test_put_defaults_requirements_empty(self, tmp_path):
        """FileCache.put without new params still works, defaults empty."""
        from ivy_lsp.indexer.file_cache import FileCache

        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\n")
        cache = FileCache()
        cache.put(str(f), result=None, symbols=[])
        entry = cache.get(str(f))
        assert entry is not None
        assert entry.requirements == []
        assert entry.writes == []
        assert entry.export_import_info is None
