"""Tests for RFC fetcher module."""

import os
import tempfile

import pytest

from ivy_lsp.core.rfc.fetcher import (
    FetchError,
    FetchResult,
    _compute_hash,
    _resolve_source,
    clear_cache,
    fetch_rfc,
)


class TestResolveSource:
    def test_rfc_number(self):
        kind, url = _resolve_source("RFC9000")
        assert kind == "rfc"
        assert "9000" in url

    def test_bare_number(self):
        kind, url = _resolve_source("9000")
        assert kind == "rfc"
        assert "9000" in url

    def test_draft(self):
        kind, url = _resolve_source("draft-ietf-quic-transport-34")
        assert kind == "draft"
        assert "draft-ietf-quic-transport-34" in url

    def test_local_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test content")
            path = f.name
        try:
            kind, resolved = _resolve_source(path)
            assert kind == "local"
            assert resolved == os.path.abspath(path)
        finally:
            os.unlink(path)

    def test_url(self):
        kind, url = _resolve_source("https://example.com/rfc.txt")
        assert kind == "url"
        assert url == "https://example.com/rfc.txt"

    def test_invalid_source(self):
        with pytest.raises(FetchError):
            _resolve_source("not-a-valid-source")


class TestComputeHash:
    def test_consistent_hash(self):
        h1 = _compute_hash("hello world")
        h2 = _compute_hash("hello world")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_content_different_hash(self):
        assert _compute_hash("hello") != _compute_hash("world")


class TestFetchRfcLocal:
    @pytest.mark.asyncio
    async def test_fetch_local_file(self):
        content = "Section 4.  Streams\n\nEndpoints MUST accept frames.\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            clear_cache()
            result = await fetch_rfc(path, use_cache=False)
            assert isinstance(result, FetchResult)
            assert result.text == content
            assert result.content_hash == _compute_hash(content)
            assert not result.cached
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        content = "cached content"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            clear_cache()
            result1 = await fetch_rfc(path, use_cache=True)
            assert not result1.cached
            result2 = await fetch_rfc(path, use_cache=True)
            assert result2.cached
            assert result2.text == content
        finally:
            os.unlink(path)
            clear_cache()

    @pytest.mark.asyncio
    async def test_fetch_nonexistent_file_error(self):
        with pytest.raises(FetchError):
            # This won't match as local file, so _resolve_source will fail
            await fetch_rfc("/nonexistent/path/to/rfc.txt")
