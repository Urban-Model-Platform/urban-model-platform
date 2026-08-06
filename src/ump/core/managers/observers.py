"""Concrete observer implementations for job state transitions.

This module provides production-ready observers that handle:
- Status history recording
- Background polling scheduling
- Results verification
- Eager result storage on completion
"""

import asyncio
import logging
from typing import Optional, Set

from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.observers import JobStateObserver
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.result_storage import ResultStorageError
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.providers_config import ProcessConfig
from ump.core.services.result_storage_coordinator import ResultStorageCoordinator

logger = logging.getLogger(__name__)


class StatusHistoryObserver:
    """Records all status changes to job repository.

    Extracts status history recording from JobManager, making it an explicit
    side effect that can be enabled/disabled independently.
    """

    def __init__(self, repository: JobRepositoryPort):
        self._repo = repository

    async def on_job_created(
        self,
        job: Job,
        status_info: JobStatusInfo,
    ) -> None:
        """Record initial status in history."""
        await self._repo.append_status(job.id, status_info)
        logger.debug(
            f"[observer:history] recorded initial status job_id={job.id} status={status_info.status}"
        )

    async def on_status_changed(
        self,
        job: Job,
        old_status_info: Optional[JobStatusInfo],
        new_status_info: JobStatusInfo,
    ) -> None:
        """Record status change in history."""
        await self._repo.append_status(job.id, new_status_info)
        logger.debug(
            f"[observer:history] recorded status change job_id={job.id} "
            f"old={old_status_info.status if old_status_info else None} new={new_status_info.status}"
        )

    async def on_job_completed(
        self,
        job: Job,
        final_status_info: JobStatusInfo,
    ) -> None:
        """Terminal status already recorded in on_status_changed."""
        pass


class PollingSchedulerObserver:
    """Schedules background polling for running jobs.

    Extracts polling scheduling decision from JobManager, decoupling state
    observation from implementation. The actual poll loop remains in JobManager
    as it requires complex dependencies (status derivation, enrichment, etc.).

    This observer makes the "when to start polling" decision explicit and
    testable without duplicating complex polling logic.
    """

    def __init__(self, schedule_callback):
        """Initialize with callback to JobManager._schedule_poll method.

        Args:
            schedule_callback: Callable that schedules a poll loop for a job_id
        """
        self._schedule_callback = schedule_callback

    async def on_job_created(
        self,
        job: Job,
        status_info: JobStatusInfo,
    ) -> None:
        """Job creation doesn't trigger polling (wait for status change)."""
        pass

    async def on_status_changed(
        self,
        job: Job,
        old_status_info: Optional[JobStatusInfo],
        new_status_info: JobStatusInfo,
    ) -> None:
        """Schedule polling if job is running with remote status URL."""
        if job.remote_status_url and not job.is_in_terminal_state():
            logger.debug(f"[observer:polling] triggering poll schedule job_id={job.id}")
            self._schedule_callback(job.id)

    async def on_job_completed(
        self,
        job: Job,
        final_status_info: JobStatusInfo,
    ) -> None:
        """Terminal jobs don't need polling."""
        pass


class ResultsVerificationObserver:
    """Verifies remote results are accessible for successful jobs.

    Extracts results verification from JobManager, making it a separate
    concern that can be enabled/disabled independently.
    """

    def __init__(self, http_client: HttpClientPort):
        self._http = http_client

    async def on_job_created(
        self,
        job: Job,
        status_info: JobStatusInfo,
    ) -> None:
        """Job creation doesn't trigger verification."""
        pass

    async def on_status_changed(
        self,
        job: Job,
        old_status_info: Optional[JobStatusInfo],
        new_status_info: JobStatusInfo,
    ) -> None:
        """Status changes don't trigger verification (wait for completion)."""
        pass

    async def on_job_completed(
        self,
        job: Job,
        final_status_info: JobStatusInfo,
    ) -> None:
        """Verify remote results are accessible for successful jobs."""
        if final_status_info.status != StatusCode.successful:
            return

        # Extract results URL from status info links
        results_url = None
        if final_status_info.links:
            for link in final_status_info.links:
                if link.rel == "results":
                    results_url = link.href
                    break

        if not results_url:
            logger.debug(f"[observer:verify] no results URL job_id={job.id}")
            return

        # Skip verification for local results (already served by this API)
        if results_url.startswith("/jobs/"):
            logger.debug(f"[observer:verify] skipping local results job_id={job.id}")
            return

        # Verify remote results are accessible
        try:
            logger.debug(
                f"[observer:verify] checking remote results job_id={job.id} url={results_url}"
            )
            await self._http.get(results_url, timeout=10.0)
            logger.debug(f"[observer:verify] remote results accessible job_id={job.id}")
        except Exception as exc:
            logger.warning(
                f"[observer:verify] remote results check failed job_id={job.id} "
                f"url={results_url} error={exc}"
            )
            # Don't fail the job, just log warning


class ResultStorageObserver:
    """Eagerly stores a job's result the moment the job completes successfully.

    This is the *trigger* for Feature V result storage, deliberately kept thin.
    It answers only "is now the moment, and is this job in scope?" — the actual
    fetch/convert/register sequence lives in ``ResultStorageCoordinator`` and the
    storage adapter behind it.  The observer knows nothing about GeoPackages,
    ldproxy or Kubernetes.

    Why an observer and not a step in the poll loop: storage is a post-completion
    side effect that must not slow down or break status polling.  Riding on the
    existing ``on_job_completed`` notification keeps the poll loop hot path clean
    and reuses the error isolation the notifier already provides.

    Failure semantics — read this before changing anything:

    ``emulate-ref``
        The coordinator swallows storage failures itself and falls back to
        serving the value inline.  Nothing reaches us.

    ``emulate-ref-only``
        The coordinator re-raises, because the policy promised a reference that
        we cannot deliver.  We catch it here and record the reason on the job's
        ``diagnostic`` field.  We deliberately do **not** mark the job failed:
        the computation succeeded, only its result delivery did not.  The
        caller, ``JobManager._notify_job_completed``, swallows observer
        exceptions, so letting it propagate would lose the information entirely.
        Persisting it is what allows ``GET /jobs/{id}/results`` (V-10) to answer
        with a results-unavailable error instead of a confusing empty success.
    """

    def __init__(
        self,
        coordinator: ResultStorageCoordinator,
        providers: ProvidersPort,
        repository: JobRepositoryPort,
    ) -> None:
        self._coordinator = coordinator
        self._providers = providers
        self._repo = repository

    async def on_job_created(
        self,
        job: Job,
        status_info: JobStatusInfo,
    ) -> None:
        """A job that just started has no result to store."""
        pass

    async def on_status_changed(
        self,
        job: Job,
        old_status_info: Optional[JobStatusInfo],
        new_status_info: JobStatusInfo,
    ) -> None:
        """Intermediate statuses carry no result; we wait for completion."""
        pass

    async def on_job_completed(
        self,
        job: Job,
        final_status_info: JobStatusInfo,
    ) -> None:
        """Store the result if policy and client intent call for it."""
        if final_status_info.status != StatusCode.successful:
            return  # failed/dismissed jobs have nothing to store

        process_config = self._resolve_process_config(job)
        if process_config is None:
            return  # already logged; a missing config is not worth crashing over

        if not self._coordinator.should_store(job, process_config):
            logger.debug(
                f"[observer:storage] storage not required job_id={job.id} "
                f"policy={process_config.transmission_mode_policy}"
            )
            return

        logger.info(f"[observer:storage] storing result job_id={job.id}")
        try:
            await self._coordinator.coordinate(job, process_config, self._repo)
        except ResultStorageError as exc:
            await self._record_unavailable_result(job, exc)

    def _resolve_process_config(self, job: Job) -> Optional[ProcessConfig]:
        """Look up the process config that carries the transmission-mode policy.

        ``job.process_id`` is the canonical, provider-prefixed id
        (``"provider:process"``), which is exactly what ``get_process_config``
        expects alongside the provider name.  ``job.provider`` is normally set;
        we fall back to the prefix of the process id for jobs persisted before
        that field existed.

        Returns None (and logs) when the config cannot be resolved — e.g. the
        provider was removed from providers.yaml while the job was running.
        Storage is then silently skipped rather than raising inside the
        notification loop.
        """
        if not job.process_id:
            logger.warning(
                f"[observer:storage] job has no process_id, skipping job_id={job.id}"
            )
            return None

        provider_name = job.provider or job.process_id.split(":", 1)[0]

        try:
            process_config = self._providers.get_process_config(
                provider_name, job.process_id
            )
        except Exception as exc:
            logger.warning(
                f"[observer:storage] process config lookup failed job_id={job.id} "
                f"process_id={job.process_id} error={exc}"
            )
            return None

        if process_config is None:
            logger.warning(
                f"[observer:storage] no process config for job_id={job.id} "
                f"process_id={job.process_id}, skipping storage"
            )
        return process_config

    async def _record_unavailable_result(
        self, job: Job, exc: ResultStorageError
    ) -> None:
        """Persist why the promised reference could not be produced.

        Best-effort by design: if even this write fails we log and move on,
        because the caller cannot act on an exception raised from an observer.
        """
        reason = f"Result storage failed: {exc}"
        logger.error(f"[observer:storage] {reason} job_id={job.id}")

        job.diagnostic = reason
        job.touch()
        try:
            await self._repo.update(job)
        except Exception as persist_error:
            logger.error(
                f"[observer:storage] could not persist storage failure "
                f"job_id={job.id} error={persist_error}"
            )
