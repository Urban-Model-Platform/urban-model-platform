"""JobManager: orchestrates local job creation and remote execution forwarding.

Responsibilities (Step 1):
1. Create local job with accepted snapshot.
2. Forward execute request to remote provider.
3. Extract initial statusInfo (direct body or via Location polling once).
4. Persist job & status history.
5. Schedule background polling until terminal state.
6. Return 201 Created + Location + statusInfo body.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from pydantic import BaseModel

from ump.core.config import JobManagerConfig
from ump.core.exceptions import OGCProcessException, OptimisticLockError
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.observers import JobStateObserver
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.result_storage import ResultStoragePort
from ump.core.interfaces.status_derivation import StatusDerivationContext
from ump.core.managers.status_derivation_orchestrator import (
    StatusDerivationOrchestrator,
)
from ump.core.models.job import (
    Job,
    JobStatusInfo,
    StatusCode,
)
from ump.core.models.link import Link
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.providers_config import ProcessConfig, ProviderConfig
from ump.core.settings import logger

REQUIRED_STATUS_FIELDS = {"jobID", "status", "type"}

# V-11: appended to a gated job's statusInfo message while its required
# result reference is being stored and its liveness confirmed. The job stays
# `running` externally throughout, so this is the only client-visible signal
# that something is still happening beyond normal execution.
_PUBLICATION_IN_PROGRESS_MESSAGE = "Result publication in progress"
_PUBLICATION_COMPLETE_MESSAGE = "Result available and published"


# -------------------------------------------
# Pipeline primitives
# -------------------------------------------


class PipelineStep(ABC):
    """Abstract base for a single step in the job execution pipeline.

    Each step receives the shared ``JobExecutionContext``, mutates it in place
    (or sets ``should_halt`` to abort early), and returns control to the pipeline.
    """

    @abstractmethod
    async def process(self, context: JobExecutionContext) -> None: ...


@dataclass
class ExecutionResult:
    """Distilled output of a completed pipeline execution."""

    job_id: str
    status_info: Optional[JobStatusInfo] = None
    response_status: int = 201
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: Dict[str, Any] = field(default_factory=dict)

    def to_response(self) -> Dict[str, Any]:
        return {
            "status": self.response_status,
            "headers": self.response_headers,
            "body": self.response_body,
        }


class JobExecutionContext(BaseModel):
    """Carries mutable state through pipeline steps.

    Passed by reference through each ``PipelineStep.process`` call.
    Steps mutate fields directly; set ``should_halt = True`` to abort.
    """

    model_config = {"arbitrary_types_allowed": True}

    # ---- set by caller before pipeline runs ----
    process_id: str = ""
    execute_payload: Dict[str, Any] = {}
    headers: Dict[str, str] = {}
    user_id: Optional[str] = None  # authenticated user; None = anonymous
    # First-class execution context (used by ShapeClientResponseStep)
    execution_mode: str = "async"  # "sync" | "async" — derived from Prefer header
    response_mode: str = "raw"  # "raw" | "document" — from ExecuteRequest.response
    output_specs: Dict[str, Any] = {}  # per-output OutputSpec (transmissionMode etc.)

    # ---- set by ValidateAndResolveStep ----
    provider: Optional[ProviderConfig] = None
    provider_process_id: str = ""  # raw process id without provider prefix
    process_config: Optional[ProcessConfig] = None  # per-process policy config

    # ---- set by CreateLocalJobStep ----
    job: Optional[Job] = None

    # ---- set by PersistAcceptedStep ----
    accepted_si: Optional[JobStatusInfo] = None

    # ---- set by ForwardToProviderStep ----
    provider_resp: Optional[Dict[str, Any]] = None

    # ---- set by DeriveStatusInfoStep ----
    status_info: Optional[JobStatusInfo] = None
    remote_status_url: Optional[str] = None
    remote_job_id: Optional[str] = None
    diagnostic: Optional[str] = None

    # ---- pipeline control ----
    should_halt: bool = False
    response: Optional[Dict[str, Any]] = None  # final response dict

    def to_result(self) -> ExecutionResult:
        job_id = self.job.id if self.job else ""
        if self.response:
            return ExecutionResult(
                job_id=job_id,
                status_info=self.status_info,
                response_status=self.response.get("status", 201),
                response_headers=self.response.get("headers", {}),
                response_body=self.response.get("body", {}),
            )
        body = self.status_info.model_dump() if self.status_info else {}
        return ExecutionResult(
            job_id=job_id,
            status_info=self.status_info,
            response_status=201,
            response_headers={"Location": f"/jobs/{job_id}"},
            response_body=body,
        )


class JobExecutionPipeline:
    """Runs a sequence of ``PipelineStep`` instances against a shared context.

    Stops early when any step sets ``context.should_halt = True``.
    """

    def __init__(self, steps: List[PipelineStep]) -> None:
        self.steps = steps

    async def execute(self, context: JobExecutionContext) -> ExecutionResult:
        for step in self.steps:
            if context.should_halt:
                break
            await step.process(context)
        return context.to_result()


class TransientOGCError(OGCProcessException):
    """Wrapper for transient OGC errors that should be retried.

    Used to distinguish retryable errors (502, 503, 504) from
    non-retryable client errors (4xx) in retry logic.
    """

    pass


def _parse_json_document(
    body_bytes: bytes, content_type: str
) -> Optional[Dict[str, Any]]:
    """Parse a remote results body as an OGC document response, if it is one.

    Returns None (never raises) when the content type is not JSON or the
    body does not decode as a JSON object — callers treat this as "no inline
    outputs to merge" rather than an error.
    """
    if "json" not in (content_type or "").split(";")[0].strip().lower():
        return None
    try:
        parsed = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class JobManager:
    """Orchestrates job lifecycle: creation, forwarding, status derivation, polling, results proxy.

    Attributes:
        config: Immutable configuration for job behavior (poll intervals, timeouts, etc.)
    """

    def __init__(
        self,
        providers: ProvidersPort,
        http_client: HttpClientPort,
        process_id_validator: ProcessIdValidatorPort,
        job_repo: JobRepositoryPort,
        config: JobManagerConfig,
        retry_port: Optional[
            Any
        ] = None,  # RetryPort protocol; kept generic to avoid tight coupling
        result_storage_port: Optional[ResultStoragePort] = None,
        remote_auth: Optional[
            Any
        ] = None,  # RemoteAuthPort; kept generic to avoid tight coupling
        poll_lock: Optional[PollLockPort] = None,  # distributed poll-loop ownership
        observers: Optional[
            list[JobStateObserver]
        ] = None,  # Observer pattern for state transitions
    ) -> None:
        self._providers = providers
        self._http = http_client
        self._validator = process_id_validator
        self._repo = job_repo
        self.config = config
        self._poll_tasks: Set[asyncio.Task] = set()
        # Job IDs with an active poll loop. Prevents duplicate loops from
        # being spawned when PollingSchedulerObserver re-triggers _schedule_poll
        # during an already-running poll cycle.
        self._active_poll_jobs: Set[str] = set()
        self._shutdown = False
        self._retry = retry_port
        self._result_storage = result_storage_port
        self._remote_auth = remote_auth
        self._poll_lock = poll_lock
        self._observers = observers or []

        # Initialize status derivation orchestrator
        self._status_orchestrator = StatusDerivationOrchestrator(http_client)

    async def _notify_job_created(self, job: Job, status_info: JobStatusInfo) -> None:
        """Notify all observers that a job was created."""
        for observer in self._observers:
            try:
                await observer.on_job_created(job, status_info)
            except Exception as exc:
                logger.error(
                    f"[observer:error] on_job_created failed observer={type(observer).__name__} "
                    f"job_id={job.id} error={exc}"
                )

    async def _notify_status_changed(
        self,
        job: Job,
        old_status_info: Optional[JobStatusInfo],
        new_status_info: JobStatusInfo,
    ) -> None:
        """Notify all observers that job status changed."""
        for observer in self._observers:
            try:
                await observer.on_status_changed(job, old_status_info, new_status_info)
            except Exception as exc:
                logger.error(
                    f"[observer:error] on_status_changed failed observer={type(observer).__name__} "
                    f"job_id={job.id} error={exc}"
                )

    async def _notify_job_completed(
        self, job: Job, final_status_info: JobStatusInfo
    ) -> None:
        """Notify all observers that a job reached terminal state."""
        for observer in self._observers:
            try:
                await observer.on_job_completed(job, final_status_info)
            except Exception as exc:
                logger.error(
                    f"[observer:error] on_job_completed failed observer={type(observer).__name__} "
                    f"job_id={job.id} error={exc}"
                )

    async def run_execution_pipeline(
        self,
        process_id: str,
        execute_payload: Optional[Dict[str, Any]],
        headers: Dict[str, str],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pipeline-based execution entrypoint. - Renamed from "create_and_forward"

        Extracts execution_mode and response_mode as first-class context fields
        so pipeline steps (especially ShapeClientResponseStep) can make OGC-correct
        response-shape decisions without reading the raw payload.
        """
        prefer = (headers.get("Prefer") or headers.get("prefer") or "").lower()
        execution_mode = "sync" if "respond-sync" in prefer else "async"

        payload = execute_payload or {}
        response_mode = payload.get("response", "raw")
        output_specs = payload.get("outputs", {})

        context = JobExecutionContext(
            process_id=process_id,
            execute_payload=payload,
            headers=headers,
            user_id=user_id,
            execution_mode=execution_mode,
            response_mode=response_mode,
            output_specs=output_specs,
        )
        pipeline = self._build_execution_pipeline()
        result = await pipeline.execute(context)
        return result.to_response()

    def _build_execution_pipeline(self) -> JobExecutionPipeline:
        """Construct the step pipeline with all dependencies wired."""
        from ump.core.managers.steps import (
            CreateLocalJobStep,
            DeriveStatusInfoStep,
            EnforceTransmissionPolicyStep,
            FinalizeJobStep,
            ForwardToProviderStep,
            HandleProviderResponseStep,
            InitiatePollingStep,
            PersistAcceptedStep,
            ShapeClientResponseStep,
            ValidateAndResolveStep,
        )

        return JobExecutionPipeline(
            steps=[
                ValidateAndResolveStep(self._validator, self._providers),
                EnforceTransmissionPolicyStep(),
                CreateLocalJobStep(self.config),
                PersistAcceptedStep(self._repo, self._observers),
                ForwardToProviderStep(
                    self._http, self._repo, self._retry, self.config, self._remote_auth
                ),
                HandleProviderResponseStep(self._repo),
                DeriveStatusInfoStep(self._status_orchestrator),
                FinalizeJobStep(self._repo, self._observers),
                ShapeClientResponseStep(),
                InitiatePollingStep(self._schedule_poll),
            ]
        )

    # ----------------- Helper methods -----------------
    def _enrich_status_info(
        self,
        status_info: JobStatusInfo,
        job: Job,
        accepted_created: datetime,
    ) -> None:
        """Enrich status info with contextual fields based on current status.

        Fills in missing timestamps, progress, and messages for consistent UX.
        Modifies status_info in place.
        """
        now = datetime.now(timezone.utc)

        if status_info.status == StatusCode.running:
            if status_info.started is None:
                status_info.started = accepted_created
            if status_info.progress is None:
                status_info.progress = 0
            if not status_info.message:
                status_info.message = "Running"
        elif status_info.status == StatusCode.successful:
            if status_info.started is None:
                status_info.started = accepted_created
            if status_info.finished is None:
                status_info.finished = now
            if status_info.progress is None:
                status_info.progress = 100
            if not status_info.message:
                status_info.message = "Completed"
        elif status_info.status == StatusCode.failed:
            if status_info.finished is None:
                status_info.finished = now
            if not status_info.message:
                status_info.message = "Failed"

    def _normalize_and_enrich_status_info(
        self,
        status_info: JobStatusInfo,
        job: Job,
        process_id: str,
        accepted_si: JobStatusInfo,
    ) -> None:
        """Normalize and enrich valid statusInfo with local context.

        Modifies status_info in place to:
        - Ensure processID is set correctly
        - Adopt accepted created timestamp if remote omitted it
        - Update updated timestamp
        - Enrich missing optional fields (started, finished, progress, message)
        """
        status_info.processID = process_id

        # Adopt accepted created timestamp if remote omitted it
        if status_info.created is None:
            status_info.created = accepted_si.created
        status_info.updated = datetime.now(timezone.utc)

        # Enrich missing optional fields
        created_time = accepted_si.created or datetime.now(timezone.utc)
        self._enrich_status_info(status_info, job, created_time)

    async def _init_job(
        self, process_id: str, provider_prefix: str, inputs: Optional[Dict[str, Any]]
    ) -> Job:
        # Local UUID creation: we intentionally decouple local job identity from any remote job id.
        # This guards against collisions across providers and allows stable user-facing references
        # even if upstream retries or reassigns a different remote identifier.
        job_id = str(uuid.uuid4())
        job = Job(
            id=job_id,
            process_id=process_id,
            provider=provider_prefix,
            status=str(StatusCode.accepted),
            inputs=inputs if inputs and self._is_inline_small(inputs) else None,
            inputs_storage=(
                "inline"
                if inputs and self._is_inline_small(inputs)
                else "object"
                if inputs
                else "inline"
            ),
        )
        return job

    async def _persist_accepted(self, job: Job, process_id: str) -> JobStatusInfo:
        accepted_si = JobStatusInfo(
            jobID=job.id,
            status=StatusCode.accepted,
            type="process",
            processID=process_id,
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            message=None,
            progress=0,
        )

        accepted_si.links = [
            Link(
                href=f"/jobs/{job.id}",
                rel="self",
                type="application/json",
                title="Job status",
            )
        ]
        job.apply_status_info(accepted_si)
        await self._repo.create(job)
        # Notify observers (includes status history recording)
        await self._notify_job_created(job, accepted_si)
        return accepted_si

    def _is_transient_error(self, exc: Exception) -> bool:
        """Check if exception represents a transient error worth retrying.

        Transient errors include:
        - Connection errors (server busy, connection refused, etc.)
        - Timeout errors (server slow to respond)
        - 502/503/504 gateway/service unavailable errors

        Non-transient errors that should fail immediately:
        - 4xx client errors (bad request, not found, etc.)
        - Authentication errors
        """
        if isinstance(exc, OGCProcessException):
            # Retry on gateway/service unavailable errors
            if exc.response.status in (502, 503, 504):
                return True
            # Don't retry client errors or auth errors
            if 400 <= exc.response.status < 500:
                return False
        # Other exceptions might be transient connection issues
        return True

    def _wrap_if_transient(self, exc: OGCProcessException) -> OGCProcessException:
        """Classify an OGC error, wrapping transient ones so the retry adapter retries them.

        This is the only piece of retry-related logic that belongs in the core:
        deciding *what* is worth retrying. The actual looping/backoff mechanics
        are delegated entirely to the injected ``RetryPort`` implementation.
        """
        return TransientOGCError(exc.response) if self._is_transient_error(exc) else exc

    async def _handle_forward_error(self, job: Job, exc: Exception) -> None:
        """Handle exceptions during forward request, marking job as failed."""
        if isinstance(exc, OGCProcessException):
            await self._repo.mark_failed(
                job.id, reason=exc.response.title, diagnostic=exc.response.detail
            )
            logger.warning(
                f"[job:forward] OGCProcessException job_id={job.id} title={exc.response.title}"
            )
        else:
            await self._repo.mark_failed(
                job.id, reason="Upstream Error", diagnostic=str(exc)
            )
            logger.error(
                f"[job:forward] unexpected exception job_id={job.id} error={exc}"
            )

    async def _forward_once(
        self, exec_url: str, payload: Dict[str, Any], headers: Dict[str, str], job: Job
    ) -> Dict[str, Any]:
        """Single-attempt forward POST; wraps transient OGC errors for the retry adapter."""
        logger.debug(
            f"[job:forward] POST exec_url={exec_url} job_id={job.id} "
            f"headers={list(headers.keys())} payload_size={len(str(payload))}"
        )
        try:
            resp = await self._http.post(exec_url, json=payload, headers=headers)
        except OGCProcessException as exc:
            raise self._wrap_if_transient(exc) from exc
        logger.debug(
            f"[job:forward] POST completed job_id={job.id} status={resp.get('status')} "
            f"keys={list(resp.keys())}"
        )
        return resp

    async def _safe_forward(
        self, job: Job, exec_url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        """Forward execution request to remote provider with retry logic.

        Uses TenacityRetryAdapter (if available) for exponential backoff on transient
        errors (connection errors, timeouts, 502/503/504). Non-transient errors (4xx)
        fail immediately without retry by wrapping them in non-retryable exceptions.
        """

        try:
            # Use retry adapter if available, with config-based retry settings
            if self._retry:
                logger.debug(f"[job:forward] using retry adapter job_id={job.id}")
                resp = await self._retry.execute(
                    self._forward_once,
                    exec_url,
                    payload,
                    headers,
                    job,
                    attempts=self.config.forward_max_retries,
                    wait_initial=self.config.forward_retry_base_wait,
                    wait_max=self.config.forward_retry_max_wait,
                    exception_types=(TransientOGCError,),  # Only retry transient errors
                )
                return resp
            else:
                # Fallback: single attempt without retry
                logger.debug(
                    f"[job:forward] no retry adapter, single attempt job_id={job.id}"
                )
                return await self._forward_once(exec_url, payload, headers, job)

        except TransientOGCError as exc:
            # Transient error retry exhausted - unwrap original exception
            logger.error(
                f"[job:forward] transient error retry exhausted job_id={job.id} "
                f"status={exc.response.status} title={exc.response.title}"
            )
            await self._handle_forward_error(job, exc)
            return None

        except OGCProcessException as exc:
            # Non-transient error (no retry attempted)
            logger.warning(
                f"[job:forward] non-transient error job_id={job.id} "
                f"status={exc.response.status} title={exc.response.title}"
            )
            await self._handle_forward_error(job, exc)
            return None

        except Exception as exc:
            # Unexpected non-OGC exception
            logger.error(
                f"[job:forward] unexpected exception job_id={job.id} error={exc}"
            )
            await self._handle_forward_error(job, exc)
            return None

    async def _derive_status_info(
        self,
        job: Job,
        process_id: str,
        provider: Any,
        provider_resp: Dict[str, Any],
        accepted_si: JobStatusInfo,
    ) -> tuple[JobStatusInfo, Optional[str], Optional[str], Optional[str]]:
        """Derive statusInfo from provider response using Strategy pattern.

        Delegates to StatusDerivationOrchestrator which selects the appropriate
        strategy based on response pattern (direct statusInfo, immediate results,
        Location follow-up, or fallback failed).

        Returns (status_info, remote_status_url, remote_job_id, diagnostic).
        """
        # Create context for strategy evaluation
        context = StatusDerivationContext(
            job=job,
            process_id=process_id,
            provider=provider,
            provider_resp=provider_resp,
            accepted_si=accepted_si,
        )

        # Use orchestrator to derive status via appropriate strategy
        result = await self._status_orchestrator.derive_status(context)

        # Normalize and enrich if we got valid statusInfo
        if result.status_info and result.status_info.status != StatusCode.failed:
            # Normalize remote job ID to local one, and retain remote id separately
            if result.status_info.jobID and result.status_info.jobID != job.id:
                if not job.remote_job_id:
                    job.remote_job_id = result.status_info.jobID
                result.status_info.jobID = job.id

            self._normalize_and_enrich_status_info(
                result.status_info, job, process_id, accepted_si
            )

        # Ensure local self link consistency for any status
        self._ensure_self_link(job.id, result.status_info)

        return (
            result.status_info,
            result.remote_status_url,
            result.remote_job_id,
            result.diagnostic,
        )

    async def _finalize_job(
        self,
        job: Job,
        status_info: JobStatusInfo,
        remote_status_url: Optional[str],
        remote_job_id: Optional[str],
        diagnostic: Optional[str],
    ) -> None:
        # Capture old status for observer notification
        old_status_info = (
            JobStatusInfo(**job.status_info.model_dump()) if job.status_info else None
        )

        if remote_status_url:
            job.remote_status_url = remote_status_url
        if remote_job_id:
            job.remote_job_id = remote_job_id
        if diagnostic:
            job.diagnostic = diagnostic

        # Inject local results link if job already successful and link absent
        if status_info and status_info.status == StatusCode.successful:
            self._ensure_self_link(job.id, status_info)
            self._ensure_results_link(job.id, status_info)
        job.apply_status_info(status_info)
        await self._repo.update(job)

        logger.debug(
            f"[job:finalize] job_id={job.id} status={status_info.status} remote_status_url={job.remote_status_url} remote_job_id={job.remote_job_id} terminal={job.is_in_terminal_state()}"
        )

        # Notify observers (includes status history recording, polling scheduling, verification)
        await self._notify_status_changed(job, old_status_info, status_info)

        if job.is_in_terminal_state():
            await self._notify_job_completed(job, status_info)

    async def _handle_upstream_error_response(
        self, job: Job, upstream_status: int, upstream_body: Any
    ) -> Optional[Dict[str, Any]]:
        """Handle error responses (>=400) from upstream provider.

        Returns propagated error response dict if body is not statusInfo, None otherwise.
        """
        if upstream_status < 400:
            return None

        si = self._extract_status_info(upstream_body)
        if si:
            # Valid statusInfo in error response; will be handled normally
            return None

        # Non-statusInfo error body; mark local job failed and propagate upstream response
        await self._repo.mark_failed(job.id, reason=f"Upstream {upstream_status}")
        logger.debug(
            f"[job:error-propagate] marking job failed job_id={job.id} upstream_status={upstream_status} returning raw upstream body"
        )

        return {
            "status": upstream_status,
            "headers": {"Location": f"/jobs/{job.id}"},
            "body": upstream_body
            if isinstance(upstream_body, (dict, list))
            else {"error": str(upstream_body)},
        }

    def _response(
        self, job_id: str, status_info: Optional[JobStatusInfo]
    ) -> Dict[str, Any]:
        body = status_info.model_dump() if status_info else {}
        return {"status": 201, "headers": {"Location": f"/jobs/{job_id}"}, "body": body}

    async def _verify_remote_results(
        self, provider: Any, process_id: str, remote_job_id: str
    ) -> bool:
        """Fetch remote results for terminal successful job; return True if fetched.

        Failure to fetch indicates mismatch between remote status and availability; we
        treat this as local failure to ensure clients don't assume success without outputs.
        """
        try:
            base = str(provider.url).rstrip("/")
            results_url = f"{base}/jobs/{remote_job_id}/results"
            logger.debug(f"[job:verify] fetching results_url={results_url}")

            async def fetch():
                return await self._http.get(results_url)

            # Use injected retry adapter if available (supports transient unavailability right after success)
            if self._retry:
                try:
                    resp = await self._retry.execute(fetch)
                except Exception as exc:
                    logger.debug(
                        f"[job:verify] retry exhausted job_id={remote_job_id} err={exc}"
                    )
                    return False
            else:
                # Fallback single attempt
                resp = await fetch()

            if isinstance(resp, dict):
                logger.debug(
                    f"[job:verify] results fetch ok keys={list(resp.keys())[:5]}"
                )
                return True
            logger.debug(
                f"[job:verify] results non-dict type={type(resp).__name__}; treating as success"
            )
            return True
        except Exception as exc:
            logger.debug(
                f"[job:verify] results fetch exception job_id={remote_job_id} err={exc}"
            )
            return False

    async def _resolve_provider(self, process_id: str) -> tuple[str, str]:
        try:
            provider_prefix, raw_id = self._validator.extract(process_id)
            return provider_prefix, raw_id
        except ValueError:
            # search summaries as fallback
            # For simplicity reuse provider list; a proper search could use cached summaries.
            for name in self._providers.list_providers():
                # naive attempt: assume raw_id exists under provider
                return name, process_id
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Not Found",
                    status=404,
                    detail=f"Process '{process_id}' not found",
                    instance=None,
                )
            )

    async def _resolve_ttw(self, job: Job) -> Optional[float]:
        """Resolve time-to-wait (TTW) for job from config hierarchy.

        Resolution order:
        1. ProcessConfig.ttw_job_done (most specific, per-process override)
        2. ProviderConfig.ttw_job_done (provider-wide default)
        3. None (no timeout if neither is set)

        Args:
            job: Job instance with process_id set

        Returns:
            Timeout in seconds (float), or None if no timeout configured
        """
        if not job.process_id:
            return None

        try:
            provider_name, _ = await self._resolve_provider(job.process_id)
            process_config = self._providers.get_process_config(
                provider_name, job.process_id
            )

            # Check process-level override first
            if process_config and process_config.ttw_job_done is not None:
                return process_config.ttw_job_done

            # Fall back to provider-level default
            provider_config = self._providers.get_provider(provider_name)
            return provider_config.ttw_job_done if provider_config else None
        except Exception as exc:
            # If config resolution fails, log and return None (no timeout)
            logger.warning(
                f"[job:ttw] failed to resolve ttw for job_id={job.id} process_id={job.process_id} err={exc}"
            )
            return None

    def _extract_status_info(self, body: Any) -> Optional[JobStatusInfo]:
        if not isinstance(body, dict):
            return None
        if not REQUIRED_STATUS_FIELDS.issubset(body.keys()):
            return None
        try:
            return JobStatusInfo(**body)
        except Exception:
            return None

    def _resolve_location(self, base: str, location: str) -> str:
        if location.startswith("http://") or location.startswith("https://"):
            return location
        return urljoin(base.rstrip("/") + "/", location.lstrip("/"))

    def _is_inline_small(self, inputs: Dict[str, Any]) -> bool:
        """Check if inputs are small enough for inline storage."""
        return len(str(inputs)) < self.config.inline_inputs_size_limit

    async def _check_and_handle_timeout(self, job: Job) -> bool:
        """Check if job has exceeded timeout and mark as failed if so.

        Resolves the timeout from the process config hierarchy:
        1. ProcessConfig.ttw_job_done (if set, process-specific override)
        2. ProviderConfig.ttw_job_done (provider-wide default)
        3. None (no timeout if neither is set)

        Returns True if timeout was reached and job was marked failed.
        """
        ttw = await self._resolve_ttw(job)

        if ttw is None or job.created is None:
            return False

        elapsed = (datetime.now(timezone.utc) - job.created).total_seconds()
        if elapsed <= ttw:
            return False

        logger.warning(
            f"[job:poll] timeout reached job_id={job.id} elapsed={elapsed}s > {ttw}s; marking failed"
        )

        old_status = (
            JobStatusInfo(**job.status_info.model_dump()) if job.status_info else None
        )

        timeout_si = JobStatusInfo(
            jobID=job.id,
            status=StatusCode.failed,
            type="process",
            processID=job.process_id,
            message=f"Timed out after {ttw}s waiting for remote completion",
            created=job.created,
            updated=datetime.now(timezone.utc),
            finished=datetime.now(timezone.utc),
            progress=None,
        )

        job.apply_status_info(timeout_si)
        await self._repo.update(job)

        # Notify observers (includes status history recording)
        await self._notify_status_changed(job, old_status, timeout_si)
        await self._notify_job_completed(job, timeout_si)
        return True

    # ---------------- Polling -----------------
    def _schedule_poll(self, job_id: str) -> None:
        if self._shutdown:
            return
        if job_id in self._active_poll_jobs:
            logger.debug(f"[job:poll] poll already active, skipping job_id={job_id}")
            return
        self._active_poll_jobs.add(job_id)
        logger.debug(f"[job:poll] scheduling poll loop job_id={job_id}")
        task = asyncio.create_task(self._poll_loop(job_id))
        self._poll_tasks.add(task)
        task.add_done_callback(
            lambda t: (
                self._poll_tasks.discard(t),
                self._active_poll_jobs.discard(job_id),
            )
        )

    async def _poll_loop(self, job_id: str) -> None:
        """Continuously poll remote status until terminal or shutdown.

        Acquires a distributed advisory lock (if configured) before entering
        the loop so that only one UMP instance polls each job at a time.
        The lock is released in the finally block, which covers both normal
        exit and exceptions (including asyncio.CancelledError on shutdown).
        """
        if self._poll_lock:
            acquired = await self._poll_lock.try_acquire(job_id)
            if not acquired:
                logger.debug(
                    f"[job:poll] lock held by another instance, skipping job_id={job_id}"
                )
                return
        try:
            while not self._shutdown:
                # Check if we should continue polling
                should_stop, reason = await self._should_stop_polling(job_id)
                if should_stop:
                    logger.debug(f"[job:poll] stopping: {reason} job_id={job_id}")
                    return

                # Get fresh job state
                job = await self._repo.get(job_id)
                if not job:  # Job disappeared (should not happen, but defensive)
                    logger.debug(f"[job:poll] job disappeared job_id={job_id}")
                    return

                # Attempt to fetch and process remote status
                terminal_reached = await self._poll_and_update_status(job)
                if terminal_reached:
                    return

                # Sleep before next poll
                await asyncio.sleep(self.config.poll_interval)
        finally:
            if self._poll_lock:
                await self._poll_lock.release(job_id)

    async def _should_stop_polling(self, job_id: str) -> tuple[bool, str]:
        """Check if polling should stop for a job.

        Returns (should_stop, reason) tuple.
        """
        job = await self._repo.get(job_id)

        if not job:
            return True, "job not found"

        if job.is_in_terminal_state():
            return True, f"terminal state {job.status}"

        if not job.remote_status_url:
            return True, "no remote_status_url"

        # Check timeout
        if await self._check_and_handle_timeout(job):
            return True, "timeout exceeded"

        return False, ""

    async def _poll_and_update_status(self, job: Job) -> bool:
        """Poll remote status and update job if status changed.

        Returns True if terminal state reached, False otherwise.
        """
        # Guard: must have remote status URL
        if not job.remote_status_url:
            return False

        try:
            # Fetch remote status
            provider = (
                self._providers.get_provider(job.provider) if job.provider else None
            )
            auth_headers = (
                self._remote_auth.resolve(provider.authentication).headers
                if self._remote_auth and provider
                else {}
            )
            resp = await self._http.get(
                job.remote_status_url, headers=auth_headers or None
            )
            status_info = self._extract_status_info(resp)

            # Guard: skip if no valid status info
            if not status_info:
                logger.debug(f"[job:poll] no valid statusInfo job_id={job.id}")
                return False

            # Process the status update
            return await self._process_status_update(job, status_info)

        except Exception as exc:
            logger.debug(f"[job:poll] fetch error job_id={job.id} err={exc}")
            return False

    async def _process_status_update(
        self, job: Job, status_info: JobStatusInfo
    ) -> bool:
        """Process a status update from remote provider.

        Normalizes IDs, enriches fields, updates job, notifies observers.
        Returns True if terminal state reached.
        """
        # Normalize remote job ID
        if status_info.jobID and status_info.jobID != job.id:
            if not job.remote_job_id:
                job.remote_job_id = status_info.jobID
            status_info.jobID = job.id

        # Ensure processID is set
        status_info.processID = job.process_id

        # Enrich if status changed or fields missing
        prev_status = job.status_info.status if job.status_info else None
        if self._needs_enrichment(status_info, prev_status):
            created_time = job.created or datetime.now(timezone.utc)
            self._enrich_status_info(status_info, job, created_time)

        # Update timestamp
        status_info.updated = datetime.now(timezone.utc)

        # Ensure local links
        self._ensure_self_link(job.id, status_info)

        # Capture old status for observers
        old_status = (
            JobStatusInfo(**job.status_info.model_dump()) if job.status_info else None
        )

        # V-11: defer the `successful` transition until the stored reference
        # is confirmed live. If this job's policy (+ client request) requires
        # a stored reference, persist `running` with a progressing message
        # instead of `successful` -- but still hand the TRUE final status to
        # observers below, so ResultStorageObserver can store, verify, and
        # itself own the eventual successful/failed transition (see
        # `ResultStorageObserver._finalize_publication`). Jobs that don't
        # require storage are unaffected and take the normal path.
        final_status_info = status_info
        gated = False
        if status_info.status == StatusCode.successful:
            if await self._requires_stored_reference(job):
                gated = True
                status_info = status_info.model_copy(
                    update={
                        "status": StatusCode.running,
                        "message": _PUBLICATION_IN_PROGRESS_MESSAGE,
                    }
                )
            else:
                self._ensure_results_link(job.id, status_info)

        # Apply and persist — retry once on optimistic lock conflict.
        # Conflicts only occur when two instances poll the same job simultaneously
        # (race during rolling deploy); the advisory lock (Feature IX) prevents
        # this in steady state, but we still guard here for defence-in-depth.
        job.apply_status_info(status_info)
        try:
            await self._repo.update(job)
        except OptimisticLockError:
            logger.debug(
                f"[job:poll] optimistic lock conflict, re-reading job_id={job.id}"
            )
            refreshed_job = await self._repo.get(job.id)
            if not refreshed_job:
                return False

            job = refreshed_job
            job.apply_status_info(status_info)

            await self._repo.update(job)

        # Only fire the observer event on a real status transition.
        # Polling frequently returns the same status (e.g. accepted→accepted while
        # a job is queued). Firing on no-change would: (a) flood status_history with
        # duplicate records and (b) cause PollingSchedulerObserver to spawn a new
        # poll loop on every iteration, creating unbounded fan-out.
        old_code = old_status.status if old_status else None
        if old_code != status_info.status:
            await self._notify_status_changed(job, old_status, status_info)

        if gated:
            # The remote already reported a terminal outcome; stop polling and
            # hand off to the completion observers with the TRUE final status
            # so ResultStorageObserver can store + verify and itself persist
            # the eventual successful/failed transition. The job record we
            # just persisted stays `running` in the meantime.
            await self._notify_job_completed(job, final_status_info)
            return True

        # Check if terminal
        if job.is_in_terminal_state():
            logger.debug(
                f"[job:poll] terminal state reached job_id={job.id} status={job.status}"
            )
            await self._notify_job_completed(job, status_info)
            return True

        return False

    def _needs_enrichment(
        self, status_info: JobStatusInfo, prev_status: Optional[StatusCode]
    ) -> bool:
        """Check if status info needs enrichment (status changed or fields missing)."""
        if prev_status != status_info.status:
            return True
        return any(
            getattr(status_info, f) is None for f in ["started", "progress", "message"]
        )

    async def shutdown(self) -> None:
        self._shutdown = True
        for task in list(self._poll_tasks):
            task.cancel()
        if self._poll_tasks:
            await asyncio.gather(*self._poll_tasks, return_exceptions=True)

    # ---------------- Results Access -----------------
    async def get_results(self, job_id: str) -> Dict[str, Any]:
        """Fetch remote results for a terminal successful job.

        We never persist results locally; every invocation proxies the provider.
        Returns a dict with either:
          - ``{"status": 200, "content_type": str, "body_bytes": bytes}``
            for any successful result (JSON or binary — caller decides how to
            serialize based on ``content_type``)
          - ``{"status": 404, "body": {"detail": ...}}`` for not-found / not-ready
          - ``{"status": 500, "body": {"detail": ...}}`` for unexpected errors

        The remote's Content-Type header is returned verbatim and must be
        forwarded to the client; UMP never parses the result body.
        """
        job = await self._repo.get(job_id)
        if not job or not job.status_info:
            return {"status": 404, "body": {"detail": "Job not found"}}
        if job.status_info.status != StatusCode.successful:
            return {"status": 404, "body": {"detail": "Results not available"}}
        if not job.remote_job_id or not job.provider:
            return {"status": 404, "body": {"detail": "Remote job id missing"}}
        # A *required* result store that failed (emulate-ref with an explicitly
        # requested reference, or emulate-ref-only) is recorded by the
        # completion observer as a machine-readable marker in ``diagnostic``.
        # The client asked for a reference precisely so we would NOT return the
        # (potentially huge) inline value — so surface a hard error instead of
        # transparently proxying that value.
        if job.diagnostic and job.diagnostic.startswith(
            Job.RESULT_STORAGE_FAILED_MARKER
        ):
            logger.error(
                f"[job:results] required result store failed job_id={job.id} "
                f"diagnostic={job.diagnostic}"
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Result Storage Failed",
                    status=502,
                    detail=(
                        "The result was requested as a reference but could not "
                        "be stored, so it cannot be delivered. The inline value "
                        "is intentionally not returned. Please retry later or "
                        "contact the operator if the problem persists."
                    ),
                    instance=None,
                )
            )
        provider = self._providers.get_provider(job.provider)
        base = str(provider.url).rstrip("/")
        results_url = f"{base}/jobs/{job.remote_job_id}/results"
        auth_headers = (
            self._remote_auth.resolve(provider.authentication).headers
            if self._remote_auth
            else {}
        )

        # V-10: once any output has been persisted to a result store, always
        # answer with an OGC ``document`` response — one JSON object with one
        # entry per output, each an inline value or an ``href`` reference link.
        # A single code path handles any mix of stored/inline outputs; no 302
        # redirect is used (a redirect has exactly one target and cannot
        # represent more than one output — see REF-F5 decision log).
        # Jobs that never stored anything (pass-through / value-only / a
        # non-downgraded emulate-ref value) are unaffected and keep the
        # original transparent proxy below.
        if job.stored_outputs:
            policy = await self._resolve_transmission_mode_policy(job)
            return await self._build_stored_results_document(
                job, results_url, auth_headers, policy
            )

        # Required-store still in progress: unreachable under V-11 -- a job
        # only reports `successful` (checked above) after
        # ResultStorageObserver has confirmed the stored reference is live, so
        # `stored_outputs` is always populated by the time a client can
        # observe `successful`. Kept documented here (rather than silently
        # removed) because this is the historical location of the old
        # "Results Finalizing" 503 window that V-11 eliminates.

        logger.debug(
            f"[job:results] proxy fetch results_url={results_url} job_id={job.id}"
        )
        try:
            if self._retry:
                body_bytes, content_type = await self._retry.execute(
                    self._fetch_results_once,
                    results_url,
                    auth_headers or None,
                    job,
                    attempts=self.config.results_fetch_max_retries,
                    wait_initial=self.config.results_fetch_retry_base_wait,
                    wait_max=self.config.results_fetch_retry_max_wait,
                    exception_types=(TransientOGCError,),
                )
            else:
                body_bytes, content_type = await self._fetch_results_once(
                    results_url, auth_headers or None, job
                )
            return {
                "status": 200,
                "content_type": content_type,
                "body_bytes": body_bytes,
            }
        except TransientOGCError as exc:
            # Retries exhausted; surface as the underlying OGC error (e.g. 504)
            logger.error(
                f"[job:results] retries exhausted job_id={job.id} status={exc.response.status}"
            )
            raise OGCProcessException(exc.response) from exc
        except OGCProcessException as exc:
            raise exc
        except Exception as exc:
            logger.error(f"[job:results] unexpected error job_id={job.id} err={exc}")
            return {
                "status": 500,
                "body": {"detail": "Unexpected error fetching results"},
            }

    async def _resolve_transmission_mode_policy(self, job: Job) -> Optional[str]:
        """Best-effort lookup of the process's configured transmission-mode-policy.

        Used only to decide the shape of the stored results document (see
        ``_build_stored_results_document``). Never raises: a missing or
        unresolvable process config (e.g. provider removed from
        ``providers.yaml`` mid-run) falls back to ``None``, which keeps the
        previous merge-with-remote behaviour rather than failing the request.
        """
        if not job.process_id:
            return None
        try:
            provider_name, _ = await self._resolve_provider(job.process_id)
            process_config = self._providers.get_process_config(
                provider_name, job.process_id
            )
            return process_config.transmission_mode_policy if process_config else None
        except Exception as exc:
            logger.debug(
                f"[job:results] could not resolve transmission-mode-policy "
                f"job_id={job.id} process_id={job.process_id} err={exc}"
            )
            return None

    async def _requires_stored_reference(self, job: Job) -> bool:
        """V-11: does this job's policy (+ client request) require a stored
        reference to be live before it may report `successful`?

        Delegates the actual policy rule to
        ``result_storage_coordinator.should_store_reference`` so there is a
        single source of truth shared with ``ResultStorageCoordinator.should_store``.
        Best-effort: any resolution error (e.g. provider removed from
        providers.yaml mid-run) returns False so the job is not stuck waiting
        on a policy that can no longer be resolved.
        """
        if not job.process_id:
            return False
        try:
            provider_name, _ = await self._resolve_provider(job.process_id)
            process_config = self._providers.get_process_config(
                provider_name, job.process_id
            )
        except Exception as exc:
            logger.debug(
                f"[job:poll] could not resolve process config for gating "
                f"job_id={job.id} process_id={job.process_id} err={exc}"
            )
            return False
        if process_config is None:
            return False

        from ump.core.services.result_storage_coordinator import (
            should_store_reference,
        )

        return should_store_reference(job, process_config)

    async def _build_stored_results_document(
        self,
        job: Job,
        results_url: str,
        auth_headers: Optional[Dict[str, str]],
        policy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the OGC ``document`` response for a job with stored outputs.

        Stored outputs (``job.stored_outputs``, written by
        ``ResultStorageCoordinator``) are authoritative and always become an
        ``href`` entry pointing at the stored collection's ``items``
        endpoint — no remote call is needed for those.

        Under ``transmission-mode-policy: emulate-ref-only`` the value
        channel is never open (see ``ProcessConfig``): the client cannot
        request ``value`` and every output is unconditionally stored. The
        remote document is therefore never fetched or merged in this case —
        doing so previously leaked the remote's inline value (or, if the
        remote returned a non-document raw body, its unrelated top-level
        keys) alongside the reference link. The response now consists
        exclusively of the stored ``href`` references, matching the
        policy's contract that only reference transmission is ever visible
        to the client.

        For ``emulate-ref`` (mixed value/reference per request), an output
        the process produced but did *not* store — a mixed-output job, or
        one where only some outputs were requested as reference — is still
        filled in by fetching the remote document once and copying its
        inline value. This fetch is best-effort: if it fails, we still
        return the outputs we do have rather than fail a request that has
        real, storable data to offer (mirrors the best-effort philosophy
        used throughout result storage — V-9 cleanup, V-7 fallback).
        """
        document: Dict[str, Any] = {}
        stored_outputs = job.stored_outputs or {}

        if policy != "emulate-ref-only":
            try:
                body_bytes, content_type = await self._fetch_results_once(
                    results_url, auth_headers, job
                )
                remote_document = _parse_json_document(body_bytes, content_type)
                if remote_document:
                    document.update(remote_document)
            except Exception as exc:
                logger.warning(
                    f"[job:results] remote fetch failed while building stored "
                    f"document job_id={job.id} err={exc} — "
                    "serving stored outputs only"
                )

        for output_id, ref in stored_outputs.items():
            document[output_id] = {
                "href": ref["items_url"],
                "rel": "item",
                "type": "application/geo+json",
            }

        return {
            "status": 200,
            "content_type": "application/json",
            "body_bytes": json.dumps(document).encode("utf-8"),
        }

    async def _fetch_results_once(
        self, results_url: str, headers: Optional[Dict[str, str]], job: Job
    ) -> tuple[bytes, str]:
        """Single-attempt results fetch; wraps transient OGC errors for the retry adapter."""
        try:
            return await self._http.get_content(
                results_url,
                timeout=self.config.results_fetch_timeout,
                headers=headers,
            )
        except OGCProcessException as exc:
            raise self._wrap_if_transient(exc) from exc

    # ---------------- Link Helpers -----------------
    def _ensure_results_link(self, job_id: str, status_info: JobStatusInfo) -> None:
        """Ensure a relative results link is present in statusInfo.links when successful."""
        if status_info.status != StatusCode.successful:
            return
        existing = status_info.links or []
        if any(l.rel == "results" for l in existing):
            return
        results_link = Link(
            href=f"/jobs/{job_id}/results",
            rel="results",
            type="application/json",
            title="Job results",
        )
        status_info.links = existing + [results_link]

    def _ensure_self_link(self, job_id: str, status_info: JobStatusInfo) -> None:
        """Guarantee a local self link (remove remote self/results with foreign job id)."""
        existing = status_info.links or []
        filtered = [
            l
            for l in existing
            if not (
                l.rel in {"self", "results"} and f"/jobs/{job_id}" not in (l.href or "")
            )
        ]
        if any(l.rel == "self" for l in filtered):
            status_info.links = filtered
            return
        self_link = Link(
            href=f"/jobs/{job_id}",
            rel="self",
            type="application/json",
            title="Job status",
        )
        status_info.links = filtered + [self_link]
