"""Stdio-to-HTTP bridge for the MCP sidecar.

Reads JSON-RPC messages from stdin (what Claude Code sends),
forwards them to the sidecar's HTTP endpoint, and pipes
responses back to stdout.

Usage: python -m ivy_lsp.mcp_bridge 19847
"""

from __future__ import annotations

import asyncio
import logging
import sys

import anyio

logger = logging.getLogger(__name__)


async def run(port: int) -> None:
    """Bridge stdin/stdout to the MCP HTTP sidecar on *port*."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    url = f"http://127.0.0.1:{port}/mcp"
    logger.info("Connecting to MCP sidecar at %s", url)

    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with anyio.create_task_group() as tg:

            async def relay_stdin() -> None:
                """Read JSON-RPC lines from stdin, forward to sidecar."""
                loop = asyncio.get_event_loop()
                try:
                    while True:
                        line = await loop.run_in_executor(None, sys.stdin.readline)
                        if not line:
                            logger.info("stdin EOF — shutting down bridge")
                            break
                        stripped = line.strip()
                        if not stripped:
                            continue
                        msg = JSONRPCMessage.model_validate_json(stripped)
                        await write_stream.send(SessionMessage(msg))
                except Exception:
                    logger.error("relay_stdin crashed", exc_info=True)
                finally:
                    tg.cancel_scope.cancel()

            async def relay_stdout() -> None:
                """Read responses from sidecar, write to stdout."""
                try:
                    async for session_msg in read_stream:
                        json_str = session_msg.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        sys.stdout.write(json_str + "\n")
                        sys.stdout.flush()
                except Exception:
                    logger.error("relay_stdout crashed", exc_info=True)
                finally:
                    tg.cancel_scope.cancel()

            tg.start_soon(relay_stdin)
            tg.start_soon(relay_stdout)


def main() -> None:
    """Entry point for ``python -m ivy_lsp.mcp_bridge [port]``."""
    import os

    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19847
    logger.info("Starting MCP bridge on port %d", port)
    anyio.run(run, port)


if __name__ == "__main__":
    main()
