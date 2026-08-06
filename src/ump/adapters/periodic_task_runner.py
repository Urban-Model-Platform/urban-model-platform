"""PeriodicTaskRunner: a small, generic asyncio background-loop adapter.

Runs an async callable on a fixed interval until stopped. This is
infrastructure, not a core concern — the core (``JobCleanupService``) only
exposes ``run_once()``; *when* and *how often* to call it is a scheduling
detail that belongs in the adapter layer, same as the existing poll-loop
pattern in ``JobManager``.

Kept deliberately generic (not named after job cleanup) so it can be reused
for any other periodic background task the composition root wants to wire up
without inventing a second ad-hoc loop implementation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class PeriodicTaskRunner:
    """Runs ``task()`` every ``interval_seconds`` until ``stop()`` is awaited."""

    def __init__(
        self,
        task: Callable[[], Awaitable[None]],
        interval_seconds: float,
        name: str = "periodic-task",
    ) -> None:
        self._task = task
        self._interval = interval_seconds
        self._name = name
        self._runner: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        """Start the background loop. Safe to call at most once per instance."""
        if self._runner is not None:
            return  # already running — starting twice would spawn two loops
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        """Signal the loop to stop and wait for it to finish the current cycle."""
        if self._runner is None:
            return
        self._stop_event.set()
        await self._runner
        self._runner = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._task()
            except Exception as exc:  # noqa: BLE001 — a failed cycle must not
                # kill the loop; the next scheduled run should still happen.
                logger.error("[%s] cycle failed: %s", self._name, exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass  # normal case: interval elapsed, loop again
