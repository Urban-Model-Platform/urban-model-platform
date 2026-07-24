"""PollLockPort — distributed exclusive-ownership lock for job poll loops.

Ensures that only one UMP instance polls any given job at a time when running
with ≥2 instances behind a load balancer (Feature IX).

Two adapters:
- ``NoOpPollLock`` — always grants the lock (single-instance / test usage)
- ``PgAdvisoryPollLock`` — uses PostgreSQL session-level advisory locks;
  the lock is released automatically when the DB connection closes, so a
  crashed instance never leaves a job permanently locked.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PollLockPort(ABC):
    """Acquire / release exclusive poll-loop ownership for a job."""

    @abstractmethod
    async def try_acquire(self, job_id: str) -> bool:
        """Attempt to acquire exclusive poll ownership.

        Returns ``True`` if the lock was acquired (this instance should poll),
        ``False`` if another instance already holds it (skip polling).
        Must be non-blocking — never waits for the lock to become free.
        """
        ...

    @abstractmethod
    async def release(self, job_id: str) -> None:
        """Release the lock acquired by ``try_acquire``."""
        ...
