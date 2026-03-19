"""Logging filter that deduplicates repeated messages.

Prevents log flooding from cascading errors (e.g. a single parse error
causing hundreds of identical compilation failures).  The first occurrence
is logged at the original level; subsequent duplicates are suppressed and
a periodic count summary is emitted.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Tuple


class DedupFilter(logging.Filter):
    """Suppress duplicate log messages by (file, line, message) key.

    After the first occurrence, duplicates are counted silently.
    Every *summary_interval* seconds (default 60), a summary of
    suppressed duplicates is emitted at DEBUG level.
    """

    def __init__(self, summary_interval: float = 60.0) -> None:
        """Initialize with a summary interval in seconds."""
        super().__init__()
        self._seen: Dict[Tuple[str, int, str], int] = {}
        self._summary_interval = summary_interval
        self._last_summary = time.monotonic()

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for duplicate messages, True for first occurrence."""
        key = (
            getattr(record, "filename", ""),
            record.lineno,
            record.getMessage(),
        )

        count = self._seen.get(key, 0)
        self._seen[key] = count + 1

        if count == 0:
            # First occurrence — allow through
            return True

        # Check if it's time for a summary
        now = time.monotonic()
        if now - self._last_summary >= self._summary_interval:
            self._emit_summary(record)
            self._last_summary = now

        # Suppress duplicate
        return False

    def _emit_summary(self, record: logging.LogRecord) -> None:
        """Emit a summary of suppressed duplicates."""
        suppressed = {k: v for k, v in self._seen.items() if v > 1}
        if not suppressed:
            return
        total = sum(v - 1 for v in suppressed.values())
        top_3 = sorted(suppressed.items(), key=lambda x: x[1], reverse=True)[:3]
        details = ", ".join(
            f"{k[2][:60]}... (x{v})" if len(k[2]) > 60 else f"{k[2]} (x{v})"
            for k, v in top_3
        )
        record.msg = (
            f"[dedup] Suppressed {total} duplicate log messages. Top: {details}"
        )
        record.args = None

    def reset(self) -> None:
        """Clear dedup state (e.g. at session boundaries)."""
        self._seen.clear()
        self._last_summary = time.monotonic()
