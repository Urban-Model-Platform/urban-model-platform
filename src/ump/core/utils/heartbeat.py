"""Periodic "still running" log line around one long await."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import AsyncGenerator


@asynccontextmanager
async def heartbeat(
    log: logging.Logger, what: str, interval: float = 30.0
) -> AsyncGenerator[None]:
    """Log *what* at INFO every *interval* seconds while the block runs."""
    start = time.monotonic()

    async def _beat() -> None:
        while True:
            await asyncio.sleep(interval)
            log.info(
                "%s — still running, %.0fs elapsed", what, time.monotonic() - start
            )

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
