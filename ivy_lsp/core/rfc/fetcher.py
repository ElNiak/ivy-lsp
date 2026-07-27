"""RFC text fetcher with caching and async support."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_RFC_NUMBER_RE = re.compile(r"^(?:RFC\s*)?(\d+)$", re.IGNORECASE)
_DRAFT_RE = re.compile(r"^draft-", re.IGNORECASE)
_MAX_SIZE = 2 * 1024 * 1024  # 2MB
_TIMEOUT = 15  # seconds


class FetchError(Exception):
    """Raised when RFC fetching fails."""


@dataclass
class FetchResult:
    """Result of fetching an RFC document."""

    text: str
    source: str  # URL, file path, or cache key
    content_hash: str  # SHA-256 hex digest
    cached: bool = False
    fetch_time: float = 0.0  # seconds


# Simple in-memory cache: source -> (FetchResult, expiry_time)
_cache: dict[str, tuple[FetchResult, float]] = {}
_DEFAULT_TTL = 3600  # 1 hour


def _compute_hash(text: str) -> str:
    """Compute SHA-256 hash of text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fetch_url(url: str) -> str:
    """Synchronous URL fetch with timeout and size limit."""
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ivy-lsp/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read(_MAX_SIZE + 1)
            if len(data) > _MAX_SIZE:
                raise FetchError(f"Response exceeds {_MAX_SIZE} bytes limit from {url}")
            return data.decode("utf-8", errors="replace")
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc


def _fetch_local(path: str) -> str:
    """Read a local file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read(_MAX_SIZE + 1)
            if len(data) > _MAX_SIZE:
                raise FetchError(f"File exceeds {_MAX_SIZE} bytes limit: {path}")
            return data
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"Failed to read {path}: {exc}") from exc


def _resolve_source(rfc_source: str) -> tuple[str, str]:
    """Resolve an RFC source string to (kind, identifier).

    Returns:
        ("local", absolute_path) for local files
        ("rfc", url) for RFC numbers
        ("draft", url) for internet drafts
        ("url", url) for direct URLs
    """
    # Local file
    if os.path.exists(rfc_source):
        return ("local", os.path.abspath(rfc_source))

    # RFC number: "RFC9000" or "9000"
    m = _RFC_NUMBER_RE.match(rfc_source.strip())
    if m:
        num = m.group(1)
        url = f"https://www.rfc-editor.org/rfc/rfc{num}.txt"
        return ("rfc", url)

    # Internet draft
    if _DRAFT_RE.match(rfc_source.strip()):
        name = rfc_source.strip()
        url = f"https://www.ietf.org/archive/id/{name}.txt"
        return ("draft", url)

    # Direct URL
    if rfc_source.startswith("http://") or rfc_source.startswith("https://"):
        return ("url", rfc_source)

    raise FetchError(f"Cannot resolve RFC source: {rfc_source!r}")


async def fetch_rfc(
    rfc_source: str,
    use_cache: bool = True,
    cache_ttl: float = _DEFAULT_TTL,
) -> FetchResult:
    """Fetch RFC text from a source (RFC number, draft name, URL, or local file).

    Args:
        rfc_source: RFC identifier. Accepts:
            - RFC number: "RFC9000" or "9000"
            - Internet draft: "draft-ietf-quic-transport-34"
            - Local file path: "/path/to/rfc.txt"
            - Direct URL: "https://example.com/rfc.txt"
        use_cache: Whether to use in-memory caching.
        cache_ttl: Cache time-to-live in seconds.

    Returns:
        FetchResult with the RFC text, source identifier, and content hash.
    """
    kind, identifier = _resolve_source(rfc_source)

    # Check cache
    if use_cache and identifier in _cache:
        result, expiry = _cache[identifier]
        if time.time() < expiry:
            return FetchResult(
                text=result.text,
                source=result.source,
                content_hash=result.content_hash,
                cached=True,
                fetch_time=0.0,
            )

    start = time.time()

    if kind == "local":
        text = _fetch_local(identifier)
    else:
        # Run blocking URL fetch in thread pool
        text = await asyncio.to_thread(_fetch_url, identifier)

    elapsed = time.time() - start
    content_hash = _compute_hash(text)

    result = FetchResult(
        text=text,
        source=identifier,
        content_hash=content_hash,
        cached=False,
        fetch_time=round(elapsed, 3),
    )

    # Cache result
    if use_cache:
        _cache[identifier] = (result, time.time() + cache_ttl)

    return result


def clear_cache() -> None:
    """Clear the fetch cache."""
    _cache.clear()
