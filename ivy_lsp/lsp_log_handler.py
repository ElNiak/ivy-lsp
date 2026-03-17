"""LSP log handler that bridges Python logging to LSP window/logMessage."""

import logging
import sys
import threading
import time
from typing import TYPE_CHECKING

from lsprotocol import types as lsp

if TYPE_CHECKING:
    from ivy_lsp.server import IvyLanguageServer

logger = logging.getLogger(__name__)


class LspLogHandler(logging.Handler):
    """Bridge Python logging -> LSP window/logMessage notifications.

    Rate-limited to prevent flooding the stdio pipe, which can cause
    write-side blocking and contribute to thread pool starvation.
    """

    _LEVEL_MAP = {
        logging.DEBUG: lsp.MessageType.Log,
        logging.INFO: lsp.MessageType.Info,
        logging.WARNING: lsp.MessageType.Warning,
        logging.ERROR: lsp.MessageType.Error,
        logging.CRITICAL: lsp.MessageType.Error,
    }

    _CAT_PRIORITY = {"MIL": 1, "DIA": 2, "PER": 3, "ACT": 4}
    _CAT_MIN_INTERVAL = {"MIL": 0.01, "DIA": 0.01, "PER": 0.1, "ACT": 0.1}
    _DEFAULT_MIN_INTERVAL = 0.05
    _MAX_MESSAGE_LEN = 8192  # 8 KB cap per log message

    _tls = threading.local()  # per-thread recursion guard

    def __init__(self, server: "IvyLanguageServer"):
        """Initialize with a reference to the language server."""
        super().__init__()
        self._server = server
        self._lock = threading.Lock()  # non-reentrant; no I/O under lock
        self._last_emit = 0.0
        self._drop_counts: dict = {}
        self._pipe_dead = False

    @staticmethod
    def _extract_category(msg: str) -> str:
        if msg.startswith("[MIL"):
            return "MIL"
        if msg.startswith("[ACT"):
            return "ACT"
        if msg.startswith("[DIA"):
            return "DIA"
        if msg.startswith("[PER"):
            return "PER"
        return ""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record as a ``window/logMessage`` notification."""
        if self._pipe_dead:
            return
        # Per-thread recursion guard: pygls logs inside _send_data(),
        # which would re-enter this handler. Skip to prevent infinite loop.
        if getattr(self._tls, "sending", False):
            return

        # --- fast path: all state under lock (no I/O) ---
        with self._lock:
            now = time.time()
            if record.levelno < logging.WARNING:
                msg = self.format(record)
                cat = self._extract_category(msg)
                min_interval = self._CAT_MIN_INTERVAL.get(
                    cat, self._DEFAULT_MIN_INTERVAL
                )
                if getattr(self._server, "initializing", False):
                    min_interval = max(min_interval, 1.0)
                if (now - self._last_emit) < min_interval:
                    cat_key = cat or "_untagged"
                    self._drop_counts[cat_key] = self._drop_counts.get(cat_key, 0) + 1
                    return
            else:
                msg = self.format(record)

            if len(msg) > self._MAX_MESSAGE_LEN:
                msg = msg[: self._MAX_MESSAGE_LEN] + "... [truncated]"

            msg_type = self._LEVEL_MAP.get(record.levelno, lsp.MessageType.Log)
            if self._drop_counts:
                parts = []
                for k, v in sorted(self._drop_counts.items()):
                    label = k if k != "_untagged" else "other"
                    parts.append(f"{v} {label}")
                suppression = "[" + ", ".join(parts) + " messages suppressed]"
                msg = f"{msg} {suppression}"
                self._drop_counts = {}
            self._last_emit = now

        # --- slow path: send notification WITHOUT holding lock ---
        self._tls.sending = True
        try:
            self._server.window_log_message(
                lsp.LogMessageParams(type=msg_type, message=msg)
            )
        except Exception:
            self._pipe_dead = True
            try:
                sys.stderr.write(f"[ivy-lsp-fallback] {msg}\n")
                sys.stderr.flush()
            except Exception:
                pass
        finally:
            self._tls.sending = False


# Backward-compatible alias (was private ``_LspLogHandler`` in server.py).
_LspLogHandler = LspLogHandler
