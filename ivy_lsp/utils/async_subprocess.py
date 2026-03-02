"""Shared async subprocess runner for Ivy CLI tools.

Provides bounded concurrency via ``asyncio.Semaphore`` and a clean
``SubprocessResult`` return type used by both the LSP and MCP servers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_semaphore_lock = threading.Lock()
_semaphores: Dict[int, asyncio.Semaphore] = {}
_semaphore_limit: Optional[int] = None


@dataclass(frozen=True)
class SubprocessResult:
    """Clean return type for Ivy subprocess invocations."""

    success: bool
    message: str
    output_lines: List[str] = field(default_factory=list)
    duration: float = 0.0
    returncode: Optional[int] = None


def get_tool_semaphore() -> asyncio.Semaphore:
    """Return a concurrency semaphore for the current event loop.

    Each event loop gets its own ``asyncio.Semaphore`` instance (since
    asyncio primitives are not thread-safe across loops). Configured
    via ``IVY_LSP_MAX_CONCURRENT_TOOLS`` env var (default: 4).

    The semaphore is created lazily per loop on first call and reused
    thereafter. A global bound ensures that all code paths respect
    the same concurrency limit.
    """
    global _semaphore_limit
    limit = max(1, int(os.environ.get("IVY_LSP_MAX_CONCURRENT_TOOLS", "4")))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    loop_id = id(loop)

    with _semaphore_lock:
        existing = _semaphores.get(loop_id)
        if existing is not None and _semaphore_limit == limit:
            return existing
        sem = asyncio.Semaphore(limit)
        _semaphores[loop_id] = sem
        _semaphore_limit = limit
        return sem


async def run_ivy_subprocess(
    cmd: Sequence[str],
    *,
    timeout: float = 120.0,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    use_semaphore: bool = True,
) -> SubprocessResult:
    """Run an Ivy CLI tool as an async subprocess.

    Args:
        cmd: Command and arguments (e.g. ``["ivy_check", "file.ivy"]``).
        timeout: Maximum wall-clock seconds before the process is killed.
        cwd: Working directory for the subprocess.
        env: Environment variables (``None`` inherits the current env).
        use_semaphore: When ``True`` (default), acquire the global
            concurrency semaphore before spawning.  Set to ``False``
            for lightweight tools (e.g. ``ivy_show``) where blocking
            on the semaphore would add unnecessary latency.

    Returns:
        A :class:`SubprocessResult` with success flag, combined output
        lines, duration, and return code.
    """
    start = time.monotonic()

    async def _run() -> SubprocessResult:
        nonlocal start
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            return SubprocessResult(
                success=False,
                message=f"{cmd[0]} not found on PATH",
                duration=time.monotonic() - start,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Process %s did not exit after kill; may be orphaned",
                    cmd[0],
                )
            return SubprocessResult(
                success=False,
                message=f"Timed out after {timeout}s",
                duration=time.monotonic() - start,
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        output_lines = (stderr_text + stdout_text).splitlines()
        ok = proc.returncode == 0

        return SubprocessResult(
            success=ok,
            message="OK" if ok else f"Exit code {proc.returncode}",
            output_lines=output_lines,
            duration=time.monotonic() - start,
            returncode=proc.returncode,
        )

    if use_semaphore:
        async with get_tool_semaphore():
            return await _run()
    return await _run()
