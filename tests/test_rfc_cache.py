# tests/test_rfc_cache.py
"""Tests for RFC two-tier cache."""

import json
import os
import tempfile
import time

import pytest

from ivy_lsp.core.rfc.cache import RfcCache


class TestMemoryTier:
    def setup_method(self):
        self.cache = RfcCache(cache_dir=None, cache_ttl=3600)

    def test_miss_returns_none(self):
        assert self.cache.get("rfc9000") is None

    def test_put_and_get(self):
        self.cache.put("rfc9000", "some text", "abc123", "https://example.com")
        entry = self.cache.get("rfc9000")
        assert entry is not None
        assert entry["text"] == "some text"
        assert entry["content_hash"] == "abc123"

    def test_ttl_expiry(self):
        self.cache = RfcCache(cache_dir=None, cache_ttl=0)
        self.cache.put("rfc9000", "text", "hash", "source")
        # TTL=0 means immediately stale
        time.sleep(0.01)
        assert self.cache.get("rfc9000") is None

    def test_clear(self):
        self.cache.put("rfc9000", "text", "hash", "source")
        self.cache.clear()
        assert self.cache.get("rfc9000") is None


class TestDiskTier:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache = RfcCache(cache_dir=self.tmpdir, cache_ttl=3600)

    def test_put_creates_files(self):
        self.cache.put("rfc4271", "BGP text", "hash123", "https://example.com")
        rfc_dir = os.path.join(self.tmpdir, "rfc4271")
        assert os.path.isfile(os.path.join(rfc_dir, "raw.txt"))
        assert os.path.isfile(os.path.join(rfc_dir, "meta.json"))

    def test_disk_fallback_on_memory_miss(self):
        self.cache.put("rfc4271", "BGP text", "hash123", "https://example.com")
        # Create a fresh cache instance pointing at same dir (empty memory)
        fresh = RfcCache(cache_dir=self.tmpdir, cache_ttl=3600)
        entry = fresh.get("rfc4271")
        assert entry is not None
        assert entry["text"] == "BGP text"

    def test_meta_json_content(self):
        self.cache.put("rfc4271", "text", "hash123", "https://src.com")
        meta_path = os.path.join(self.tmpdir, "rfc4271", "meta.json")
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["content_hash"] == "hash123"
        assert meta["source"] == "https://src.com"
        assert "fetch_time" in meta


class TestLocalFiles:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.local_dir = tempfile.mkdtemp()
        self.cache = RfcCache(
            cache_dir=self.tmpdir, cache_ttl=3600, local_dir=self.local_dir
        )

    def test_local_file_found(self):
        local_path = os.path.join(self.local_dir, "rfc4271.txt")
        with open(local_path, "w") as f:
            f.write("Local BGP content")
        entry = self.cache.get_local("rfc4271")
        assert entry is not None
        assert entry["text"] == "Local BGP content"

    def test_local_file_not_found(self):
        assert self.cache.get_local("rfc9999") is None
