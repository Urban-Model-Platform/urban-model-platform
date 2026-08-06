"""Service: JobCleanupService (V-9).

Periodically removes finished jobs whose retention period has elapsed, freeing
both the job record itself and — if applicable — its stored result.

This is deliberately a *generic* job-lifecycle concern, not a result-storage
concern: it runs for every deployment regardless of whether a result store is
configured. When no store is configured, ``ResultStoragePort`` is a
``NullResultStorage`` and its ``delete()`` is a harmless no-op — the service
never needs to know or care which storage backend (if any) is active.

Two independent retention rules distinguish anonymous from authenticated jobs
(see ``JobManagerConfig``/settings): an anonymous job cannot ever be revisited
by its creator (there is no account to log back into), so it is reasonable to
expire it quickly and by default. An authenticated user's job is part of their
history; the default is to never auto-delete it, and an operator who wants to
must set an explicit, deliberately separate retention period.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.result_storage import ResultStorageError, ResultStoragePort
from ump.core.models.job import Job

logger = logging.getLogger(__name__)


class JobCleanupService:
    """Finds and removes expired jobs, best-effort on the storage side."""

    def __init__(
        self,
        job_repo: JobRepositoryPort,
        result_storage: ResultStoragePort,
        anonymous_retention_minutes: Optional[int],
        authenticated_retention_minutes: Optional[int],
    ) -> None:
        self._repo = job_repo
        self._storage = result_storage
        self._anonymous_retention_minutes = anonymous_retention_minutes
        self._authenticated_retention_minutes = authenticated_retention_minutes

    async def run_once(self) -> int:
        """Delete every currently-expired job. Returns how many were removed.

        Safe to call repeatedly (e.g. from a periodic scheduler): jobs that
        are not yet expired are simply not returned by ``list_expired``, and
        deleting an already-deleted job is a no-op on both the storage and
        repository side.
        """
        anonymous_cutoff, authenticated_cutoff = self._cutoffs()
        if anonymous_cutoff is None and authenticated_cutoff is None:
            return 0  # both retention rules disabled — nothing to do, ever

        expired = await self._repo.list_expired(
            anonymous_cutoff=anonymous_cutoff,
            authenticated_cutoff=authenticated_cutoff,
        )
        for job in expired:
            await self._delete_one(job)
        if expired:
            logger.info("[cleanup] removed %d expired job(s)", len(expired))
        return len(expired)

    def _cutoffs(self) -> tuple[Optional[datetime], Optional[datetime]]:
        now = datetime.now(timezone.utc)
        anonymous = (
            now - timedelta(minutes=self._anonymous_retention_minutes)
            if self._anonymous_retention_minutes is not None
            else None
        )
        authenticated = (
            now - timedelta(minutes=self._authenticated_retention_minutes)
            if self._authenticated_retention_minutes is not None
            else None
        )
        return anonymous, authenticated

    async def _delete_one(self, job: Job) -> None:
        """Remove one job's stored result (best-effort) and its record.

        Storage cleanup failing must never prevent the job record itself from
        being deleted — an orphaned GeoPackage/provider entity is a much
        smaller problem than a job that can never be cleaned up because one
        storage call keeps failing. The error is logged so operators can
        still notice and investigate a persistently broken store.
        """
        try:
            await self._storage.delete(job.id)
        except ResultStorageError as exc:
            logger.warning(
                "[cleanup] could not delete stored result for job_id=%s: %s",
                job.id,
                exc,
            )
        await self._repo.delete(job.id)
