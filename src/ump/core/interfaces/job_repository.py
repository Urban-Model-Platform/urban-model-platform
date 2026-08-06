"""JobRepositoryPort: hexagonal port for persisting and querying Jobs.

Async methods anticipate DB/network-backed adapters; in-memory implementation
still uses async for interface uniformity.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence

from ump.core.models.job import Job, JobStatusInfo


class JobRepositoryPort(ABC):
    """Port abstraction for Job persistence and status history."""

    @abstractmethod
    async def create(self, job: Job) -> Job:
        """Persist a newly created Job and return stored instance."""
        raise NotImplementedError

    @abstractmethod
    async def get(self, job_id: str) -> Optional[Job]:
        """Return Job or None if not found."""
        raise NotImplementedError

    @abstractmethod
    async def update(self, job: Job) -> Job:
        """Persist modifications to an existing job and return updated instance."""
        raise NotImplementedError

    @abstractmethod
    async def list(
        self,
        provider: Optional[str] = None,
        process_id: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        public_only: bool = False,
        include_public: bool = True,
    ) -> Sequence[Job]:
        """List jobs with optional filters.

        Visibility rules:
                public_only=True          → only jobs where user_id IS NULL
                user_id set, include_public=True  → user's own + public jobs
                user_id set, include_public=False → user's own jobs only
                no user_id, public_only=False     → all jobs (auth disabled / admin)
        """
        raise NotImplementedError

    @abstractmethod
    async def mark_failed(
        self,
        job_id: str,
        reason: str,
        diagnostic: Optional[str] = None,
    ) -> Optional[Job]:
        """Mark a job failed and return updated Job or None if not found."""
        raise NotImplementedError

    @abstractmethod
    async def append_status(
        self, job_id: str, status_info: JobStatusInfo
    ) -> Optional[Job]:
        """Append a new status snapshot and update current status_info."""
        raise NotImplementedError

    @abstractmethod
    async def append_event(self, job_id: str, event: dict) -> None:
        """Record a domain/event log entry (optional; may be no-op)."""
        raise NotImplementedError

    @abstractmethod
    async def list_expired(
        self,
        anonymous_cutoff: Optional[datetime] = None,
        authenticated_cutoff: Optional[datetime] = None,
    ) -> Sequence[Job]:
        """Return finished jobs eligible for cleanup, split by two retention rules.

        Anonymous jobs (``user_id IS NULL``) are matched against
        ``anonymous_cutoff``; authenticated jobs (``user_id`` set) against
        ``authenticated_cutoff``.  Either cutoff being ``None`` disables that
        half of the query entirely — passing both as ``None`` returns an empty
        sequence rather than "everything", so a caller cannot accidentally sweep
        the whole table by omitting arguments.

        Only jobs in a terminal state (``successful``, ``failed``, ``dismissed``)
        with a ``finished`` timestamp are eligible; still-running jobs are never
        returned regardless of age.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, job_id: str) -> None:
        """Permanently remove a job and its history.

        Idempotent: deleting a job that no longer exists is a no-op, so the
        cleanup service can retry without first checking existence.
        """
        raise NotImplementedError
