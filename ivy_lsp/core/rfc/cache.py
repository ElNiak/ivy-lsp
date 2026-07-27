"""Two-tier (memory + disk) cache for RFC documents."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RfcCache:
    """Two-tier cache: in-memory dict backed by on-disk file store.

    Args:
        cache_dir: Directory for persistent disk cache. None disables disk tier.
        cache_ttl: Seconds before a cached entry is considered stale.
        local_dir: Directory of user-provided local RFC text files.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        cache_ttl: int = 3600,
        local_dir: str | Path | None = None,
    ) -> None:
        """Initialize cache tiers and configuration."""
        self._memory: dict[str, tuple[dict, float]] = {}
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._cache_ttl = cache_ttl
        self._local_dir = Path(local_dir) if local_dir else None

    def get(self, rfc_id: str) -> Optional[dict]:
        """Look up *rfc_id* in memory, then disk. Returns None on miss."""
        rfc_id = self.normalize_id(rfc_id)

        # Memory tier.
        if rfc_id in self._memory:
            entry, ts = self._memory[rfc_id]
            if time.time() - ts < self._cache_ttl:
                return entry
            del self._memory[rfc_id]

        # Disk tier.
        if self._cache_dir is not None:
            disk_entry = self._read_disk(rfc_id)
            if disk_entry is not None:
                self._memory[rfc_id] = (disk_entry, time.time())
                return disk_entry

        return None

    def put(self, rfc_id: str, text: str, content_hash: str, source: str) -> None:
        """Store an RFC in both memory and disk tiers."""
        rfc_id = self.normalize_id(rfc_id)
        entry = {
            "text": text,
            "content_hash": content_hash,
            "source": source,
        }
        self._memory[rfc_id] = (entry, time.time())

        if self._cache_dir is not None:
            self._write_disk(rfc_id, text, content_hash, source)

    def get_local(self, rfc_id: str) -> Optional[dict]:
        """Check local RFC directory for a matching file."""
        if self._local_dir is None:
            return None

        rfc_id = self.normalize_id(rfc_id)
        for ext in (".txt", ".text", ""):
            path = self._local_dir / f"{rfc_id}{ext}"
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                return {
                    "text": text,
                    "content_hash": content_hash,
                    "source": str(path),
                }
        return None

    def set_local_dir(self, path: Path | None) -> None:
        """Override the local RFC file directory."""
        self._local_dir = Path(path) if path else None

    def clear(self) -> None:
        """Clear the in-memory cache. Disk cache is not deleted."""
        self._memory.clear()

    @staticmethod
    def normalize_id(rfc_id: str) -> str:
        """Normalize to lowercase, reject path traversal attempts."""
        rfc_id = rfc_id.lower().strip()
        if ".." in rfc_id or "/" in rfc_id or "\\" in rfc_id:
            raise ValueError(f"Invalid RFC ID: {rfc_id!r}")
        if rfc_id.startswith("rfc"):
            return rfc_id
        if rfc_id.isdigit():
            return f"rfc{rfc_id}"
        return rfc_id

    def _read_disk(self, rfc_id: str) -> Optional[dict]:
        """Read cached RFC from disk if present and not stale."""
        rfc_dir = self._cache_dir / rfc_id  # type: ignore[union-attr]
        meta_path = rfc_dir / "meta.json"
        raw_path = rfc_dir / "raw.txt"

        if not meta_path.is_file() or not raw_path.is_file():
            return None

        try:
            with open(meta_path) as f:
                meta = json.load(f)
            fetch_time = meta.get("fetch_time", 0)
            if time.time() - fetch_time >= self._cache_ttl:
                return None
            text = raw_path.read_text(encoding="utf-8", errors="replace")
            return {
                "text": text,
                "content_hash": meta.get("content_hash", ""),
                "source": meta.get("source", ""),
            }
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read disk cache for %s: %s", rfc_id, exc)
            return None

    def _write_disk(
        self, rfc_id: str, text: str, content_hash: str, source: str
    ) -> None:
        """Write RFC text and metadata to disk cache."""
        rfc_dir = self._cache_dir / rfc_id  # type: ignore[union-attr]
        try:
            rfc_dir.mkdir(parents=True, exist_ok=True)
            (rfc_dir / "raw.txt").write_text(text, encoding="utf-8")
            meta = {
                "content_hash": content_hash,
                "source": source,
                "fetch_time": time.time(),
            }
            with open(rfc_dir / "meta.json", "w") as f:
                json.dump(meta, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to write disk cache for %s: %s", rfc_id, exc)
