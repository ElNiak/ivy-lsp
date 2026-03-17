"""Tests for ivy_lsp.utils.async_subprocess."""

from __future__ import annotations

import asyncio
import sys

import pytest

from ivy_lsp.config import reset_config
from ivy_lsp.utils.async_subprocess import (
    SubprocessResult,
    get_tool_semaphore,
    run_ivy_subprocess,
)

# ---------------------------------------------------------------------------
# SubprocessResult dataclass
# ---------------------------------------------------------------------------


class TestSubprocessResult:
    def test_frozen(self):
        r = SubprocessResult(success=True, message="OK")
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]

    def test_defaults(self):
        r = SubprocessResult(success=False, message="fail")
        assert r.output_lines == []
        assert r.duration == 0.0
        assert r.returncode is None


# ---------------------------------------------------------------------------
# Semaphore
# ---------------------------------------------------------------------------


class TestGetToolSemaphore:
    async def test_returns_semaphore(self):
        sem = get_tool_semaphore()
        assert isinstance(sem, asyncio.Semaphore)

    async def test_default_limit(self, monkeypatch):
        monkeypatch.delenv("IVY_LSP_MAX_CONCURRENT_TOOLS", raising=False)
        reset_config()
        # Reset module-level state to force re-creation
        import ivy_lsp.utils.async_subprocess as mod

        mod._semaphores.clear()
        mod._semaphore_limit = None

        sem = get_tool_semaphore()
        # Default is 4 — verify by acquiring 4 times without blocking
        assert sem._value == 4  # noqa: SLF001

    async def test_env_override(self, monkeypatch):
        monkeypatch.setenv("IVY_LSP_MAX_CONCURRENT_TOOLS", "2")
        reset_config()
        import ivy_lsp.utils.async_subprocess as mod

        mod._semaphores.clear()
        mod._semaphore_limit = None

        sem = get_tool_semaphore()
        assert sem._value == 2  # noqa: SLF001

    async def test_minimum_one(self, monkeypatch):
        monkeypatch.setenv("IVY_LSP_MAX_CONCURRENT_TOOLS", "0")
        reset_config()
        import ivy_lsp.utils.async_subprocess as mod

        mod._semaphores.clear()
        mod._semaphore_limit = None

        sem = get_tool_semaphore()
        assert sem._value == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# run_ivy_subprocess — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunIvySubprocess:
    async def test_success(self):
        result = await run_ivy_subprocess(
            [sys.executable, "-c", "print('hello')"],
            timeout=10.0,
            use_semaphore=False,
        )
        assert result.success is True
        assert result.message == "OK"
        assert any("hello" in line for line in result.output_lines)
        assert result.duration > 0
        assert result.returncode == 0

    async def test_nonzero_exit(self):
        result = await run_ivy_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            timeout=10.0,
            use_semaphore=False,
        )
        assert result.success is False
        assert "Exit code 1" in result.message
        assert result.returncode == 1

    async def test_stderr_combined(self):
        result = await run_ivy_subprocess(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('err\\n'); print('out')",
            ],
            timeout=10.0,
            use_semaphore=False,
        )
        assert result.success is True
        lines = result.output_lines
        assert any("err" in l for l in lines)
        assert any("out" in l for l in lines)


# ---------------------------------------------------------------------------
# run_ivy_subprocess — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunIvySubprocessErrors:
    async def test_timeout(self):
        result = await run_ivy_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
            use_semaphore=False,
        )
        assert result.success is False
        assert "Timed out" in result.message

    async def test_file_not_found(self):
        result = await run_ivy_subprocess(
            ["__nonexistent_ivy_tool__"],
            timeout=5.0,
            use_semaphore=False,
        )
        assert result.success is False
        assert "not found on PATH" in result.message

    async def test_cwd(self, tmp_path):
        result = await run_ivy_subprocess(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            timeout=10.0,
            cwd=str(tmp_path),
            use_semaphore=False,
        )
        assert result.success is True
        assert str(tmp_path) in "\n".join(result.output_lines)


# ---------------------------------------------------------------------------
# Semaphore concurrency limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSemaphoreLimiting:
    async def test_bounds_concurrency(self, monkeypatch):
        monkeypatch.setenv("IVY_LSP_MAX_CONCURRENT_TOOLS", "2")
        reset_config()
        import ivy_lsp.utils.async_subprocess as mod

        mod._semaphores.clear()
        mod._semaphore_limit = None

        # Use a script that takes a measurable amount of time
        cmd = [sys.executable, "-c", "import time; time.sleep(0.3); print('done')"]

        async def _tracked_run():
            result = await run_ivy_subprocess(
                cmd,
                timeout=10.0,
                use_semaphore=True,
            )
            return result

        # We can't easily track concurrency inside run_ivy_subprocess
        # without mocking, so instead verify the semaphore value directly
        sem = get_tool_semaphore()
        assert sem._value == 2  # noqa: SLF001

        # Launch 4 tasks; only 2 should be allowed through at a time
        tasks = [asyncio.create_task(_tracked_run()) for _ in range(4)]
        results = await asyncio.gather(*tasks)
        assert all(r.success for r in results)

    async def test_bypass_semaphore(self):
        result = await run_ivy_subprocess(
            [sys.executable, "-c", "print('fast')"],
            timeout=10.0,
            use_semaphore=False,
        )
        assert result.success is True
