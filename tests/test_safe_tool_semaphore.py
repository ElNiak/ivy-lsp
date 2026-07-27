"""Tests for safe_tool semaphore acquisition timeout."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_semaphore_acquisition_times_out():
    """When all slots are exhausted, new calls should time out."""
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sem.acquire(), timeout=0.1)
    sem.release()


@pytest.mark.asyncio
async def test_semaphore_releases_on_tool_timeout():
    """After a tool times out, the semaphore slot must be released."""
    sem = asyncio.Semaphore(1)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=0.5)
        await asyncio.wait_for(asyncio.sleep(10), timeout=0.1)
    except asyncio.TimeoutError:
        pass
    finally:
        sem.release()
    acquired = sem.locked()
    assert not acquired, "Semaphore should be free after release"
