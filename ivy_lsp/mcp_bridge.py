"""Stdio-to-HTTP bridge for the MCP sidecar.

Reads JSON-RPC messages from stdin (what Claude Code sends),
forwards them to the sidecar's HTTP endpoint, and pipes
responses back to stdout.

Includes automatic reconnection with exponential backoff and
a standalone fallback if the sidecar becomes permanently unavailable.

Usage: python -m ivy_lsp.mcp_bridge 19847 [port_file]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import anyio

from ivy_lsp.observability import LogCategory, log_phase

logger = logging.getLogger(__name__)

# --- Reconnection constants ---
_MAX_RECONNECT_ATTEMPTS = 5
_BACKOFF_SCHEDULE = [2.0, 4.0, 8.0, 16.0, 30.0]
_HEALTH_CHECK_INTERVAL = 15.0
_TIMEOUT_CHECK_INTERVAL = 5.0

# Sentinel value to signal stdin EOF
_EOF_SENTINEL = None


def _get_bridge_timeout() -> float:
    """Read per-request timeout from env (default 120s)."""
    raw = os.environ.get("IVY_LSP_BRIDGE_TIMEOUT")
    if raw:
        try:
            return max(10.0, float(raw))
        except (ValueError, TypeError):
            pass
    return 120.0


def _read_port_from_file(port_file: str) -> int | None:
    """Read the sidecar port from a port file on disk.

    Returns the port number, or None if the file is missing or unreadable.
    """
    try:
        with open(port_file) as f:
            content = f.read().strip()
            if content:
                return int(content)
    except (OSError, ValueError) as exc:
        logger.debug("Could not read port file %s: %s", port_file, exc)
    return None


def _synthesize_errors(
    pending: dict[str | int, Any],
    attempt: int = 0,
    max_attempts: int = _MAX_RECONNECT_ATTEMPTS,
) -> None:
    """Write JSON-RPC error responses for all pending (in-flight) requests.

    This is called when the connection drops so that the client does not
    hang waiting for a response that will never come.  The message is
    differentiated by attempt number to guide the client.
    """
    if attempt == 0:
        msg = "Ivy LSP server starting, please retry in a moment"
    elif attempt < max_attempts:
        msg = f"Ivy LSP server reconnecting (attempt {attempt}/{max_attempts})"
    else:
        msg = (
            f"Ivy LSP server unavailable after {max_attempts} attempts. "
            "Run /nct-health to diagnose."
        )

    for req_id in list(pending.keys()):
        error = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32603,
                "message": msg,
            },
        }
        sys.stdout.write(json.dumps(error) + "\n")
        sys.stdout.flush()
    log_phase(
        logger,
        category=LogCategory.DIAGNOSTIC,
        phase="mcp-bridge",
        message="Synthesized MCP bridge errors for pending requests",
        data={
            "pending_count": len(pending),
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
        level=logging.WARNING,
    )
    pending.clear()


async def _relay_stdin(stdin_queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
    """Read JSON-RPC lines from stdin and enqueue them.

    On EOF, enqueues ``_EOF_SENTINEL`` so that downstream tasks can
    detect the end of input.
    """
    loop = asyncio.get_event_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                logger.info("stdin EOF — shutting down bridge")
                await stdin_queue.put(_EOF_SENTINEL)
                return
            stripped = line.strip()
            if not stripped:
                continue
            await stdin_queue.put(stripped)
    except Exception:
        logger.error("_relay_stdin crashed", exc_info=True)
        await stdin_queue.put(_EOF_SENTINEL)


async def _forward_to_sidecar(
    stdin_queue: asyncio.Queue,  # type: ignore[type-arg]
    write_stream: Any,
    pending: dict[str | int, Any],
) -> None:
    """Dequeue messages from *stdin_queue*, track requests, forward to sidecar."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    try:
        while True:
            item = await stdin_queue.get()
            if item is _EOF_SENTINEL:
                logger.debug("_forward_to_sidecar received EOF sentinel")
                break
            try:
                # Track request IDs with timestamps for timeout detection
                parsed = json.loads(item)
                if "id" in parsed and "method" in parsed:
                    pending[parsed["id"]] = time.monotonic()
            except (json.JSONDecodeError, TypeError):
                pass
            msg = JSONRPCMessage.model_validate_json(item)
            await write_stream.send(SessionMessage(msg))
    except Exception:
        logger.error("_forward_to_sidecar crashed", exc_info=True)
    finally:
        # Cancel the task group so sibling tasks also stop
        raise anyio.get_cancelled_exc_class()()


async def _relay_stdout(
    read_stream: Any,
    pending: dict[str | int, Any],
) -> None:
    """Read responses from sidecar, write to stdout, clear pending IDs."""
    try:
        async for session_msg in read_stream:
            json_str = session_msg.message.model_dump_json(
                by_alias=True, exclude_none=True
            )
            # Remove completed request IDs from pending tracking
            try:
                parsed = json.loads(json_str)
                if "id" in parsed and ("result" in parsed or "error" in parsed):
                    pending.pop(parsed["id"], None)
            except (json.JSONDecodeError, TypeError):
                pass
            sys.stdout.write(json_str + "\n")
            sys.stdout.flush()
    except Exception:
        logger.error("_relay_stdout crashed", exc_info=True)
    finally:
        raise anyio.get_cancelled_exc_class()()


async def _fallback_standalone(stdin_queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
    """Last resort: spawn a standalone MCP process and relay stdio."""
    workspace = os.environ.get("IVY_WORKSPACE_ROOT", os.getcwd())
    logger.warning("Falling back to standalone MCP (workspace=%s)", workspace)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ivy_lsp",
        "--mcp",
        "--workspace",
        workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _drain_queue_to_proc() -> None:
        """Feed queued stdin lines into the subprocess."""
        assert proc.stdin is not None
        try:
            while True:
                item = await stdin_queue.get()
                if item is _EOF_SENTINEL:
                    proc.stdin.close()
                    return
                proc.stdin.write((item + "\n").encode())
                await proc.stdin.drain()
        except Exception:
            logger.error("_drain_queue_to_proc crashed", exc_info=True)

    async def _relay_proc_stdout() -> None:
        """Forward subprocess stdout to our stdout."""
        assert proc.stdout is not None
        try:
            async for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        except Exception:
            logger.error("_relay_proc_stdout crashed", exc_info=True)

    async def _relay_proc_stderr() -> None:
        """Forward subprocess stderr to our stderr."""
        assert proc.stderr is not None
        try:
            log_phase(
                logger,
                category=LogCategory.ACTIVITY,
                phase="mcp-bridge",
                message="Standalone MCP stderr relay started",
                data={"workspace": workspace},
                level=logging.DEBUG,
            )
            async for line in proc.stderr:
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
        except Exception:
            pass

    await asyncio.gather(
        _drain_queue_to_proc(),
        _relay_proc_stdout(),
        _relay_proc_stderr(),
    )
    await proc.wait()


async def _timeout_watchdog(
    pending: dict[str | int, Any],
    timeout: float,
) -> None:
    """Periodically check for requests that have exceeded the timeout.

    Synthesizes JSON-RPC error responses for timed-out requests so that
    the client (Claude) does not hang indefinitely.
    """
    while True:
        await asyncio.sleep(_TIMEOUT_CHECK_INTERVAL)
        now = time.monotonic()
        timed_out = [
            req_id
            for req_id, start_time in list(pending.items())
            if isinstance(start_time, (int, float)) and (now - start_time) > timeout
        ]
        for req_id in timed_out:
            elapsed = now - pending.pop(req_id, now)
            error = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": (
                        f"Ivy MCP tool timed out after {elapsed:.0f}s "
                        f"(limit: {timeout:.0f}s). Run /nct-health to diagnose."
                    ),
                },
            }
            sys.stdout.write(json.dumps(error) + "\n")
            sys.stdout.flush()
            logger.warning(
                "Request %s timed out after %.1fs (limit: %.0fs)",
                req_id,
                elapsed,
                timeout,
            )


async def run(port: int, port_file: str | None = None) -> None:
    """Bridge stdin/stdout to the MCP HTTP sidecar on *port*.

    Implements automatic reconnection with exponential backoff.  If all
    reconnection attempts are exhausted, falls back to spawning a
    standalone ``ivy_lsp --mcp`` process.
    """
    from mcp.client.streamable_http import streamablehttp_client

    # Decouple stdin reading from forwarding so buffered input survives reconnects
    stdin_queue: asyncio.Queue = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]
    # Track in-flight request IDs (value = monotonic timestamp) for timeout detection
    pending_requests: dict[str | int, Any] = {}
    bridge_timeout = _get_bridge_timeout()

    # Start the stdin reader once — it feeds the queue across reconnects
    stdin_task = asyncio.ensure_future(_relay_stdin(stdin_queue))
    # Start the timeout watchdog once — it runs across reconnects
    watchdog_task = asyncio.ensure_future(
        _timeout_watchdog(pending_requests, bridge_timeout)
    )

    for attempt in range(_MAX_RECONNECT_ATTEMPTS + 1):
        current_port = port

        if attempt > 0:
            # On reconnect, re-read port file in case sidecar restarted
            if port_file:
                new_port = _read_port_from_file(port_file)
                if new_port is not None:
                    current_port = new_port
            backoff = _BACKOFF_SCHEDULE[min(attempt - 1, len(_BACKOFF_SCHEDULE) - 1)]
            logger.warning(
                "Reconnecting (attempt %d/%d) in %.1fs to port %d",
                attempt,
                _MAX_RECONNECT_ATTEMPTS,
                backoff,
                current_port,
            )
            await asyncio.sleep(backoff)

        url = f"http://127.0.0.1:{current_port}/mcp"
        try:
            async with streamablehttp_client(url) as (
                read_stream,
                write_stream,
                _,
            ):
                logger.info("Connected to MCP sidecar at %s", url)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        _forward_to_sidecar,
                        stdin_queue,
                        write_stream,
                        pending_requests,
                    )
                    tg.start_soon(_relay_stdout, read_stream, pending_requests)
        except Exception as exc:
            logger.error("Connection lost: %s", exc)
            # Synthesize errors for any pending requests
            _synthesize_errors(pending_requests, attempt, _MAX_RECONNECT_ATTEMPTS)
            if attempt >= _MAX_RECONNECT_ATTEMPTS:
                logger.error("All reconnection attempts exhausted")
                stdin_task.cancel()
                watchdog_task.cancel()
                await _fallback_standalone(stdin_queue)
                return
            continue

        # Clean exit from relay tasks (stdin EOF forwarded)
        break

    stdin_task.cancel()
    watchdog_task.cancel()


def main() -> None:
    """Entry point for ``python -m ivy_lsp.mcp_bridge [port] [port_file]``."""
    import glob as _glob

    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19847
    port_file: str | None = sys.argv[2] if len(sys.argv) > 2 else None

    # Auto-detect port file if not provided
    if port_file is None:
        candidates = _glob.glob("/tmp/ivy-mcp-*.port")
        if candidates:
            port_file = candidates[0]

    logger.info("Starting MCP bridge on port %d (port_file=%s)", port, port_file)
    anyio.run(run, port, port_file)


if __name__ == "__main__":
    main()
