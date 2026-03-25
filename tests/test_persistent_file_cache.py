"""Tests for PersistentFileCache with SQLite backend."""

import time

from lsprotocol.types import SymbolKind

from ivy_lsp.core.indexer.file_cache import PersistentFileCache
from ivy_lsp.core.parsing.symbols import IvySymbol


class TestPersistentFileCacheBasic:
    def test_put_and_get(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t\n")
        sym = IvySymbol(
            name="t",
            kind=SymbolKind.Class,
            range=(1, 0, 1, 6),
            file_path=str(f),
        )
        cache = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        cache.put(str(f), result=None, symbols=[sym], includes=["quic_types"])
        entry = cache.get(str(f))
        assert entry is not None
        assert len(entry.symbols) == 1
        assert entry.symbols[0].name == "t"
        assert entry.includes == ["quic_types"]
        cache.close()

    def test_get_nonexistent_returns_none(self, tmp_path):
        cache = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        assert cache.get(str(tmp_path / "nope.ivy")) is None
        cache.close()


class TestPersistentFileCachePersistence:
    def test_survives_new_instance(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("v1")
        sym = IvySymbol(
            name="x",
            kind=SymbolKind.Variable,
            range=(0, 0, 0, 5),
            file_path=str(f),
        )
        cache1 = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        cache1.put(str(f), result=None, symbols=[sym], includes=[])
        cache1.close()

        cache2 = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        entry = cache2.get(str(f))
        assert entry is not None
        assert entry.symbols[0].name == "x"
        cache2.close()

    def test_stale_mtime_invalidates(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("v1")
        cache = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        cache.put(str(f), result=None, symbols=[], includes=[])
        time.sleep(0.05)
        f.write_text("v2")
        assert cache.get(str(f)) is None
        cache.close()


class TestPersistentFileCacheInvalidation:
    def test_invalidate_removes_entry(self, tmp_path):
        f = tmp_path / "a.ivy"
        f.write_text("v1")
        cache = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        cache.put(str(f), result=None, symbols=[], includes=[])
        cache.invalidate(str(f))
        assert cache.get(str(f)) is None
        cache.close()

    def test_clear_all_removes_everything(self, tmp_path):
        files = []
        for i in range(5):
            f = tmp_path / f"{i}.ivy"
            f.write_text(f"v{i}")
            files.append(f)
        cache = PersistentFileCache(str(tmp_path), cache_dir=str(tmp_path / ".cache"))
        for f in files:
            cache.put(str(f), result=None, symbols=[], includes=[])
        cache.clear_all()
        for f in files:
            assert cache.get(str(f)) is None
        cache.close()
