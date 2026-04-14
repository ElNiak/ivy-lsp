"""Reusable async lazy-build utility with cooldown, timeout, and concurrency control.

Encapsulates the pattern: "return a cached value, or build it once on first
access, with retry-cooldown on transient failure and permanent-failure
detection."  Used by ``McpServerState`` for both the SemanticModel and
RequirementGraph builders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LazyAsyncBuilder(Generic[T]):
    """Async lazy builder with cooldown, timeout, and concurrency control.

    Builds an object of type *T* on first access via an async *build_fn*,
    caches it, and returns the cached value on subsequent calls.  Handles:

    - **Fast path**: already-built value returned immediately.
    - **Cooldown retry**: after a transient failure, waits *retry_cooldown*
      seconds before retrying.
    - **Permanent failure**: if *permanent_failure_check* returns ``True``
      for an exception, the builder stops retrying.
    - **Concurrent build protection**: only one build runs at a time;
      concurrent callers see ``None`` (or wait briefly via ``get_or_wait``).
    - **Timeout**: optional ``asyncio.wait_for`` wrapper.
    """

    def __init__(
        self,
        build_fn: Callable[[], Awaitable[T]],
        timeout: float | None = None,
        retry_cooldown: float = 30.0,
        permanent_failure_check: Callable[[Exception], bool] | None = None,
        name: str = "builder",
    ) -> None:
        """Initialise the lazy builder.

        Args:
            build_fn: Async callable that produces the object.
            timeout: Maximum seconds for a single build attempt
                (``None`` = no timeout).
            retry_cooldown: Seconds to wait before retrying after a
                transient failure.
            permanent_failure_check: Optional predicate; if it returns
                ``True`` for an exception, the builder marks the failure
                as permanent and stops retrying.
            name: Human-readable label used in log messages.
        """
        self._build_fn = build_fn
        self._timeout = timeout
        self._retry_cooldown = retry_cooldown
        self._permanent_failure_check = permanent_failure_check
        self._name = name

        # Cached value
        self._value: T | None = None

        # Build state
        self._lock = asyncio.Lock()
        self._building = False
        self._last_failure: float = 0.0
        self._last_error: str | None = None
        self._permanent_failed = False

    # --- value property (get/set for pre-population) ---

    @property
    def value(self) -> T | None:
        """Return the cached value, or ``None`` if not yet built."""
        return self._value

    @value.setter
    def value(self, obj: T | None) -> None:
        """Set (or clear) the cached value directly."""
        self._value = obj

    # --- Core async get ---

    async def get(self) -> T | None:
        """Return the cached value, or build it if needed.

        Returns ``None`` if the build fails, is on cooldown, or another
        coroutine is already building.
        """
        # Fast path: already built
        if self._value is not None:
            return self._value

        # Permanent failure — never retry
        if self._permanent_failed:
            return None

        # Cooldown — too soon to retry
        if self._last_failure and (
            time.monotonic() - self._last_failure < self._retry_cooldown
        ):
            return None

        # Another coroutine is building — don't queue up
        if self._building:
            return None

        async with self._lock:
            # Double-check after acquiring lock
            if self._value is not None:
                return self._value
            if self._permanent_failed:
                return None
            if self._last_failure and (
                time.monotonic() - self._last_failure < self._retry_cooldown
            ):
                return None

            self._building = True
            try:
                if self._timeout is not None:
                    result = await asyncio.wait_for(
                        self._build_fn(), timeout=self._timeout
                    )
                else:
                    result = await self._build_fn()
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] Build timed out after %.0fs", self._name, self._timeout
                )
                self._last_failure = time.monotonic()
                self._last_error = f"Build timed out after {self._timeout:.0f}s"
                return None
            except Exception as exc:
                if self._permanent_failure_check and self._permanent_failure_check(exc):
                    logger.warning("[%s] Permanent failure: %s", self._name, exc)
                    self._permanent_failed = True
                    self._last_error = f"Permanent: {exc}"
                else:
                    logger.error(
                        "[%s] Build failed: %s", self._name, exc, exc_info=True
                    )
                    self._last_failure = time.monotonic()
                    self._last_error = str(exc)
                return None
            finally:
                self._building = False

            if result is not None:
                self._value = result
            else:
                self._last_failure = time.monotonic()
                self._last_error = "Build returned None (missing dependencies?)"
            return result

    # --- get_or_wait: non-blocking variant ---

    async def get_or_wait(self, timeout: float = 5.0) -> T | None:
        """Return the value if ready, wait briefly if building, else kick off background build.

        Unlike :meth:`get`, this never blocks for a full build.  If no
        build is in progress it schedules one in the background and
        returns ``None`` immediately.
        """
        if self._value is not None:
            return self._value

        if not self._building:
            # Kick off a background build but don't wait
            asyncio.ensure_future(self.get())
            return None

        # Currently building — wait briefly
        deadline = time.monotonic() + timeout
        while self._building and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        return self._value  # may still be None

    # --- Status reporting ---

    def get_status(self) -> dict:
        """Return a status dict describing the current builder state.

        Returns a dict with key ``"state"`` being one of
        ``"ready"``, ``"building"``, ``"failed"``, or ``"not_built"``.
        On failure, includes ``"error"`` and ``"retry_in_seconds"`` keys.
        """
        if self._value is not None:
            return {"state": "ready"}
        if self._building:
            return {"state": "building"}
        if self._last_error:
            if self._permanent_failed:
                return {
                    "state": "failed",
                    "error": self._last_error,
                    "permanent": True,
                }
            elapsed = time.monotonic() - self._last_failure
            remaining = max(0, self._retry_cooldown - elapsed)
            return {
                "state": "failed",
                "error": self._last_error,
                "retry_in_seconds": round(remaining),
            }
        return {"state": "not_built"}

    def invalidate(self) -> None:
        """Clear the cached value so the next get() triggers a fresh build."""
        self._value = None
        self._building = False
        self._last_failure = 0.0
        self._last_error = None
        self._permanent_failed = False
