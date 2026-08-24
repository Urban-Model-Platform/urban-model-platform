"""Concrete observer implementations for job state transitions.

This module provides production-ready observers that handle:
- Status history recording
- Background polling scheduling
- Results verification
- Eager result storage on completion
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

from ump.core.exceptions import OptimisticLockError
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.observers import JobStateObserver
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.result_storage import ResultStorageError
from ump.core.managers.steps.execution_steps import (
    _ensure_results_link,
    _ensure_self_link,
)
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.providers_config import ProcessConfig
from ump.core.services.result_storage_coordinator import ResultStorageCoordinator

logger = logging.getLogger(__name__)

_PERSIST_FAILURE_MAX_ATTEMPTS = 5


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
        Storage is triggered only when the client explicitly requested
        ``transmissionMode: reference``. A storage failure is therefore fatal
        for delivery semantics and is raised by the coordinator.

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
        """Store the result if policy and client intent call for it.

        V-11: when storage is required, the job's *persisted* status is still
        ``running`` at this point — ``JobManager`` deliberately withheld the
        ``successful`` transition (see ``_process_status_update``) and calls
        us with the *true* final status the remote reported. We now own
        flipping the job to its real terminal state:

          - store + publication-confirm succeeds -> persist ``successful``
            (with the self/results links V-11 also withheld).
          - store fails, or the adapter could not confirm every reference is
            publicly reachable within its own budget -> persist final
            ``failed`` with a standardized message; no auto-retry.

        Jobs that never required storage are unaffected: ``JobManager``
        already persisted them as ``successful`` and this method returns
        immediately below.
        """
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
            references = await self._coordinator.coordinate(
                job, process_config, self._repo
            )
        except ResultStorageError as exc:
            # _finalize_publication sets both the diagnostic marker and the
            # terminal `failed` status in one persisted write, superseding the
            # old _record_unavailable_result (which only annotated diagnostic
            # on a job that stayed `successful`, back when V-11 did not exist).
            await self._finalize_publication(
                job,
                final_status_info,
                success=False,
                reason=str(exc),
            )
            return

        unconfirmed = [ref for ref in (references or []) if ref.publication_pending]
        if unconfirmed:
            reason = (
                f"{len(unconfirmed)} of {len(references)} stored reference(s) "
                "could not be confirmed reachable within the allotted time."
            )
            logger.error(f"[observer:storage] {reason} job_id={job.id}")
            await self._finalize_publication(
                job, final_status_info, success=False, reason=reason
            )
            return

        await self._finalize_publication(job, final_status_info, success=True)

    async def _finalize_publication(
        self,
        job: Job,
        final_status_info: JobStatusInfo,
        success: bool,
        reason: Optional[str] = None,
    ) -> None:
        """Persist the deferred terminal transition.

        Re-reads the freshest job snapshot and retries on optimistic-lock
        conflicts, the same pattern used elsewhere in this module — the poll
        loop may have written the ``running`` gate snapshot moments earlier and
        could race a concurrent read.
        """
        from ump.core.managers.job_manager import _PUBLICATION_COMPLETE_MESSAGE

        current = job
        for attempt in range(1, _PERSIST_FAILURE_MAX_ATTEMPTS + 1):
            if current.status_info is None:
                return
            if success:
                new_status_info = current.status_info.model_copy(
                    update={
                        "status": StatusCode.successful,
                        "message": _PUBLICATION_COMPLETE_MESSAGE,
                        "finished": final_status_info.finished,
                        "progress": final_status_info.progress,
                    }
                )
                _ensure_self_link(job.id, new_status_info)
                _ensure_results_link(job.id, new_status_info)
                updates: dict = {"status_info": new_status_info}
            else:
                reason_text = reason or "Result publication failed."
                message = f"Result reference could not be published: {reason_text}"
                new_status_info = current.status_info.model_copy(
                    update={
                        "status": StatusCode.failed,
                        "message": message,
                        "finished": final_status_info.finished
                        or datetime.now(timezone.utc),
                    }
                )
                _ensure_self_link(job.id, new_status_info)
                updates = {
                    "status_info": new_status_info,
                    "diagnostic": f"{Job.RESULT_STORAGE_FAILED_MARKER}: {reason_text}",
                }

            updated = current.model_copy(update=updates)
            updated.status = str(new_status_info.status)
            updated.touch()
            try:
                await self._repo.update(updated)
                logger.info(
                    f"[observer:storage] job_id={job.id} finalized "
                    f"status={new_status_info.status}"
                )
                return
            except OptimisticLockError:
                fresh = await self._repo.get(job.id)
                if fresh is None:
                    logger.warning(
                        "[observer:storage] job vanished while finalizing "
                        "publication job_id=%s",
                        job.id,
                    )
                    return
                current = fresh
                logger.debug(
                    "[observer:storage] retry %d/%d finalizing publication "
                    "job_id=%s after concurrent modification",
                    attempt,
                    _PERSIST_FAILURE_MAX_ATTEMPTS,
                    job.id,
                )

        logger.error(
            "[observer:storage] gave up finalizing publication job_id=%s "
            "after %d attempts — job left in its last persisted state",
            job.id,
            _PERSIST_FAILURE_MAX_ATTEMPTS,
        )

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
