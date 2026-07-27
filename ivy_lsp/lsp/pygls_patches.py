"""Monkey-patches for pygls 2.0.x bugs.

Applied at import time when pygls 2.0.x is detected.
"""

import logging
from concurrent.futures import InvalidStateError

logger = logging.getLogger(__name__)


def _patch_pygls_cancelled_future() -> None:
    """Work around pygls 2.0.1 bug: responses to cancelled futures crash.

    pygls._handle_response() calls future.set_result() without checking
    future.cancelled(), so a late response to a timed-out or cancelled
    request raises InvalidStateError.  This wraps the method to suppress
    that harmless race.
    """
    from pygls.protocol.json_rpc import JsonRPCProtocol

    _original = JsonRPCProtocol._handle_response

    def _safe_handle_response(self, msg_id, result=None, error=None):
        try:
            _original(self, msg_id, result, error)
        except InvalidStateError:
            logger.debug("Ignoring response to cancelled/completed request %s", msg_id)

    JsonRPCProtocol._handle_response = _safe_handle_response  # type: ignore[assignment]


class _ClosedPipeGuardWriter:
    """Proxy that silently swallows writes once the pipe is dead.

    When the LSP client disconnects, the stdout pipe closes and
    ``BufferedWriter.write()`` raises ``ValueError`` or
    ``BrokenPipeError``.  pygls's ``_send_data()`` catches
    ``BrokenPipeError`` but logs it at ERROR level with a full
    traceback before re-raising --- producing noisy shutdown output.

    This guard prevents that noise entirely: on the first write failure
    it marks the pipe as dead, fires an ``on_pipe_break`` callback to
    signal background threads (indexer, bulk analysis, compiler), and
    returns ``len(data)`` as if the write succeeded.  All subsequent
    writes are silently swallowed.

    The server still shuts down cleanly via the stdin-EOF path: when
    the client disconnects, stdin closes, the read loop exits, and
    pygls calls ``shutdown()`` in its ``finally`` block.
    """

    __slots__ = ("_inner", "_dead", "_on_pipe_break")

    def __init__(self, inner, on_pipe_break=None):
        self._inner = inner
        self._dead = False
        self._on_pipe_break = on_pipe_break

    def write(self, data):
        if self._dead:
            return len(data)
        try:
            return self._inner.write(data)
        except (ValueError, BrokenPipeError, OSError):
            self._dead = True
            if self._on_pipe_break is not None:
                try:
                    self._on_pipe_break()
                except Exception:
                    logger.debug("pipe-break callback failed", exc_info=True)
            return len(data)

    def close(self):
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _patch_pygls_closed_pipe() -> None:
    """Wrap ``set_writer`` so the writer is guarded against closed-pipe ValueError."""
    from pygls.protocol.json_rpc import JsonRPCProtocol

    _original_set_writer = JsonRPCProtocol.set_writer

    def _guarded_set_writer(self, writer, include_headers=True):
        def _on_pipe_break():
            srv = getattr(self, "_server", None)
            if srv is None:
                return
            for attr in ("_shutdown_event", "_bulk_analysis_cancel"):
                ev = getattr(srv, attr, None)
                if ev is not None:
                    ev.set()
            indexer = getattr(srv, "indexer", None)
            if indexer is not None:
                indexer.request_stop()
            compiler = getattr(srv, "compiler_manager", None)
            if compiler is not None:
                try:
                    compiler.shutdown()
                except Exception:
                    logger.debug(
                        "compiler shutdown failed during pipe break", exc_info=True
                    )

        _original_set_writer(
            self,
            _ClosedPipeGuardWriter(writer, _on_pipe_break),  # type: ignore[arg-type]
            include_headers,
        )

    JsonRPCProtocol.set_writer = _guarded_set_writer  # type: ignore[assignment]


def apply_patches() -> None:
    """Detect pygls 2.0.x and apply all relevant patches."""
    try:
        from importlib.metadata import version as _meta_version

        import pygls as _pygls_mod

        _pygls_ver = getattr(_pygls_mod, "__version__", None) or _meta_version("pygls")
        if _pygls_ver.startswith("2.0."):
            from pygls.protocol.json_rpc import JsonRPCProtocol as _JRP

            _patches_applied = []
            if hasattr(_JRP, "_handle_response"):
                _patch_pygls_cancelled_future()
                _patches_applied.append("cancelled_future")
            if hasattr(_JRP, "set_writer"):
                _patch_pygls_closed_pipe()
                _patches_applied.append("closed_pipe_guard")
            if _patches_applied:
                logger.debug(
                    "pygls %s patches applied: %s",
                    _pygls_ver,
                    ", ".join(_patches_applied),
                )
    except ImportError:
        logger.debug("pygls not installed or version unavailable, patches skipped")
    except Exception:
        logger.debug("Failed to apply pygls patches", exc_info=True)


# Apply patches at import time (matches original server.py behaviour).
apply_patches()
