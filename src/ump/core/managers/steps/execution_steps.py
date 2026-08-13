"""Concrete PipelineStep implementations for the job execution pipeline.

Each step has a single responsibility, receives its dependencies via __init__,
and mutates the shared JobExecutionContext in place.

Ordering in the pipeline:
  ValidateAndResolveStep
  CreateLocalJobStep
  PersistAcceptedStep
  ForwardToProviderStep
  HandleProviderResponseStep
  DeriveStatusInfoStep
  FinalizeJobStep
  ShapeClientResponseStep
  InitiatePollingStep
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from ump.core.config import JobManagerConfig
from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.observers import JobStateObserver
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.status_derivation import StatusDerivationContext

# Import pipeline primitives from job_manager to avoid duplication.
# Steps live in a sub-package so this is a relative import from the parent package.
from ump.core.managers.job_manager import (
    JobExecutionContext,
    PipelineStep,
    TransientOGCError,
)
from ump.core.managers.status_derivation_orchestrator import (
    StatusDerivationOrchestrator,
)
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.link import Link
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.settings import logger

REQUIRED_STATUS_FIELDS = {"jobID", "status", "type"}


# ---------------------------------------------------------------------------
# Shared utilities (module-level — no class state needed)
# ---------------------------------------------------------------------------


def _extract_status_info(body: Any) -> Optional[JobStatusInfo]:
    if not isinstance(body, dict):
        return None
    if not REQUIRED_STATUS_FIELDS.issubset(body.keys()):
        return None
    try:
        return JobStatusInfo(**body)
    except Exception:
        return None


def _is_inline_small(inputs: Dict[str, Any], limit: int) -> bool:
    return len(str(inputs)) < limit


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, OGCProcessException):
        if exc.response.status in (502, 503, 504):
            return True
        if 400 <= exc.response.status < 500:
            return False
    return True


def _resolve_location(base: str, location: str) -> str:
    if location.startswith("http://") or location.startswith("https://"):
        return location
    return urljoin(base.rstrip("/") + "/", location.lstrip("/"))


# Policies under which UMP takes ownership of the result: it fetches the value
# from the remote, stores it, and hands the client a reference link. For these
# the remote must always deliver ``value`` — the remote may not support
# ``reference`` at all (that is the whole point of *emulating* it).
_STORE_OWNING_POLICIES = ("emulate-ref", "emulate-ref-only")


def _rewrite_outbound_transmission_mode(
    payload: Dict[str, Any], policy: Optional[str]
) -> Dict[str, Any]:
    """Return the execute payload UMP should POST to the remote for *policy*.

    Under a store-owning policy (``emulate-ref`` / ``emulate-ref-only``) every
    output the client requested as ``transmissionMode: reference`` is rewritten
    to ``value`` before forwarding: UMP fulfils the reference itself by storing
    the value result and returning an href link on the way back. Outputs the
    client already requested as ``value`` (and every other policy) are passed
    through untouched.

    This is a pure function: the input ``payload`` is never mutated. The
    client's original intent is preserved in ``job.outputs_spec`` (captured
    separately from the unmodified payload) so the result-storage coordinator
    can still detect that a reference was requested.
    """
    if policy not in _STORE_OWNING_POLICIES:
        return payload

    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        return payload

    needs_rewrite = any(
        isinstance(spec, dict) and spec.get("transmissionMode") == "reference"
        for spec in outputs.values()
    )
    if not needs_rewrite:
        return payload

    rewritten_outputs: Dict[str, Any] = {}
    for output_id, spec in outputs.items():
        if isinstance(spec, dict) and spec.get("transmissionMode") == "reference":
            new_spec = dict(spec)
            new_spec["transmissionMode"] = "value"
            rewritten_outputs[output_id] = new_spec
        else:
            rewritten_outputs[output_id] = spec

    new_payload = dict(payload)
    new_payload["outputs"] = rewritten_outputs
    return new_payload


def _ensure_self_link(job_id: str, status_info: JobStatusInfo) -> None:
    existing = status_info.links or []
    filtered = [
        lnk
        for lnk in existing
        if not (
            lnk.rel in {"self", "results"} and f"/jobs/{job_id}" not in (lnk.href or "")
        )
    ]
    if any(lnk.rel == "self" for lnk in filtered):
        status_info.links = filtered
        return
    status_info.links = filtered + [
        Link(
            href=f"/jobs/{job_id}",
            rel="self",
            type="application/json",
            title="Job status",
        )
    ]


def _ensure_results_link(job_id: str, status_info: JobStatusInfo) -> None:
    if status_info.status != StatusCode.successful:
        return
    existing = status_info.links or []
    if any(lnk.rel == "results" for lnk in existing):
        return
    status_info.links = existing + [
        Link(
            href=f"/jobs/{job_id}/results",
            rel="results",
            type="application/json",
            title="Job results",
        )
    ]


def _enrich_status_info(status_info: JobStatusInfo, accepted_created: datetime) -> None:
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


async def _notify_observers_created(
    observers: List[JobStateObserver], job: Job, status_info: JobStatusInfo
) -> None:
    for obs in observers:
        try:
            await obs.on_job_created(job, status_info)
        except Exception as exc:
            logger.error(
                f"[observer:error] on_job_created observer={type(obs).__name__} job_id={job.id} err={exc}"
            )


async def _notify_observers_changed(
    observers: List[JobStateObserver],
    job: Job,
    old: Optional[JobStatusInfo],
    new: JobStatusInfo,
) -> None:
    for obs in observers:
        try:
            await obs.on_status_changed(job, old, new)
        except Exception as exc:
            logger.error(
                f"[observer:error] on_status_changed observer={type(obs).__name__} job_id={job.id} err={exc}"
            )


async def _notify_observers_completed(
    observers: List[JobStateObserver], job: Job, final: JobStatusInfo
) -> None:
    for obs in observers:
        try:
            await obs.on_job_completed(job, final)
        except Exception as exc:
            logger.error(
                f"[observer:error] on_job_completed observer={type(obs).__name__} job_id={job.id} err={exc}"
            )


# ---------------------------------------------------------------------------
# Step 1: ValidateAndResolveStep
# ---------------------------------------------------------------------------


class ValidateAndResolveStep(PipelineStep):
    """Validate process_id format and resolve provider prefix + remote process id.

    Sets: context.provider, context.provider_process_id
    Halts: on unknown provider or invalid process_id

    ``context.provider_process_id`` is the **verbatim configured remote ID**
    (from providers.yaml), not the bare id stripped of provider prefix.
    This is important when the remote server is another UMP instance whose
    process IDs contain colons (e.g. ``fair2adapt:pluvial-flood-risk-regional``).
    """

    def __init__(
        self, validator: ProcessIdValidatorPort, providers: ProvidersPort
    ) -> None:
        self._validator = validator
        self._providers = providers

    def _find_remote_id(self, provider_name: str, canonical_id: str) -> str:
        """Return the verbatim configured remote ID for a canonical UMP process ID."""
        provider = self._providers.get_provider(provider_name)
        if provider:
            for proc_cfg in provider.processes:
                # Build the canonical ID for this configured remote ID and compare.
                expected = _to_canonical_via_validator(
                    self._validator, provider_name, proc_cfg.id
                )
                if expected == canonical_id:
                    return proc_cfg.id
        # Fallback: extract bare ID from canonical using the injected validator.
        try:
            _, bare = self._validator.extract(canonical_id)
            return bare
        except ValueError:
            return canonical_id

    async def process(self, context: JobExecutionContext) -> None:
        try:
            provider_prefix, _ = self._validator.extract(context.process_id)
        except ValueError:
            # Bare id — try first available provider as fallback
            names = self._providers.list_providers()
            if not names:
                _halt(
                    context,
                    404,
                    "Not Found",
                    f"No providers configured for '{context.process_id}'",
                )
                return
            provider_prefix = names[0]

        provider = self._providers.get_provider(provider_prefix)
        if provider is None:
            _halt(context, 404, "Not Found", f"Provider '{provider_prefix}' not found")
            return

        # Use verbatim configured remote ID, not the bare stripped id
        remote_id = self._find_remote_id(provider_prefix, context.process_id)

        context.provider = provider
        context.provider_process_id = remote_id
        # Carry the per-process config so downstream steps (ForwardToProviderStep)
        # can apply the transmission-mode policy without re-resolving it. The
        # remote_id is the verbatim configured ProcessConfig.id, so a direct
        # match is exact.
        context.process_config = next(
            (pc for pc in provider.processes if pc.id == remote_id), None
        )
        logger.debug(
            f"[step:resolve] process_id={context.process_id} provider={provider_prefix} remote_id={remote_id}"
        )


# ---------------------------------------------------------------------------
# Step 2: CreateLocalJobStep
# ---------------------------------------------------------------------------


class CreateLocalJobStep(PipelineStep):
    """Create the local Job domain object with a fresh UUID.

    Sets: context.job
    Does NOT persist — PersistAcceptedStep handles that.
    """

    def __init__(self, config: JobManagerConfig) -> None:
        self._config = config

    async def process(self, context: JobExecutionContext) -> None:
        if context.provider is None:
            _halt(
                context,
                500,
                "Server Error",
                "Provider not resolved before CreateLocalJobStep",
            )
            return

        inputs = (
            context.execute_payload.get("inputs") if context.execute_payload else None
        )
        inline = (
            inputs
            if inputs
            and _is_inline_small(inputs, self._config.inline_inputs_size_limit)
            else None
        )
        storage = "inline" if inline else ("object" if inputs else "inline")

        context.job = Job(
            id=str(uuid.uuid4()),
            process_id=context.process_id,
            provider=context.provider.name,
            user_id=context.user_id,
            status=str(StatusCode.accepted),
            inputs=inline,
            inputs_storage=storage,
            # Capture the client's response preference and per-output specs so
            # Feature VIII (result storage / policy enforcement) can read them
            # at job-completion time without re-parsing the original request.
            response_mode=context.response_mode or "raw",
            outputs_spec=context.output_specs or None,
        )
        logger.debug(
            f"[step:create] job_id={context.job.id} inline_inputs={'yes' if inline else 'no'}"
        )


# ---------------------------------------------------------------------------
# Step 3: PersistAcceptedStep
# ---------------------------------------------------------------------------


class PersistAcceptedStep(PipelineStep):
    """Persist the new job and store its initial 'accepted' statusInfo snapshot.

    Sets: context.accepted_si
    Notifies: on_job_created observers
    """

    def __init__(
        self, repo: JobRepositoryPort, observers: List[JobStateObserver]
    ) -> None:
        self._repo = repo
        self._observers = observers

    async def process(self, context: JobExecutionContext) -> None:
        if context.job is None:
            _halt(
                context,
                500,
                "Server Error",
                "Job not created before PersistAcceptedStep",
            )
            return

        now = datetime.now(timezone.utc)
        accepted_si = JobStatusInfo(
            jobID=context.job.id,
            status=StatusCode.accepted,
            type="process",
            processID=context.process_id,
            created=now,
            updated=now,
            message=None,
            progress=0,
            links=[
                Link(
                    href=f"/jobs/{context.job.id}",
                    rel="self",
                    type="application/json",
                    title="Job status",
                )
            ],
        )
        context.job.apply_status_info(accepted_si)
        await self._repo.create(context.job)
        await _notify_observers_created(self._observers, context.job, accepted_si)
        context.accepted_si = accepted_si
        logger.debug(f"[step:persist-accepted] job_id={context.job.id}")


# ---------------------------------------------------------------------------
# Step 4: ForwardToProviderStep
# ---------------------------------------------------------------------------


class ForwardToProviderStep(PipelineStep):
    """POST the execute request to the remote OGC provider with retry/backoff.

    Sets: context.provider_resp
    Halts: on failure (returns accepted snapshot so client still gets 201)
    """

    def __init__(
        self,
        http: HttpClientPort,
        repo: JobRepositoryPort,
        retry: Optional[Any],
        config: JobManagerConfig,
        remote_auth: Optional[
            Any
        ] = None,  # RemoteAuthPort; kept generic to avoid circular import
    ) -> None:
        self._http = http
        self._repo = repo
        self._retry = retry
        self._config = config
        self._remote_auth = remote_auth

    async def process(self, context: JobExecutionContext) -> None:
        if context.job is None or context.provider is None:
            _halt(
                context,
                500,
                "Server Error",
                "Job/provider missing before ForwardToProviderStep",
            )
            return

        exec_url = (
            str(context.provider.url).rstrip("/")
            + f"/processes/{context.provider_process_id}/execution"
        )
        prefer = context.headers.get("Prefer") or context.headers.get("prefer")
        auth_headers = (
            self._remote_auth.resolve(context.provider.authentication).headers
            if self._remote_auth
            else {}
        )
        forward_headers = {**auth_headers, **(({"Prefer": prefer}) if prefer else {})}

        # Apply the transmission-mode policy to the *outbound* request only.
        # Under emulate-ref / emulate-ref-only a client-requested
        # transmissionMode=reference is downgraded to value so the remote (which
        # may not support reference at all) returns the raw result; UMP then
        # stores it and hands the client a reference link on the way back. The
        # original client intent lives on in context.execute_payload / the
        # persisted job.outputs_spec and is intentionally left untouched here.
        policy = (
            context.process_config.transmission_mode_policy
            if context.process_config is not None
            else None
        )
        payload = _rewrite_outbound_transmission_mode(
            context.execute_payload or {}, policy
        )

        logger.debug(
            f"[step:forward] exec_url={exec_url} job_id={context.job.id} policy={policy}"
        )

        async def _do_forward():
            try:
                return await self._http.post(
                    exec_url, json=payload, headers=forward_headers
                )
            except OGCProcessException as exc:
                if _is_transient_error(exc):
                    raise TransientOGCError(exc.response) from exc
                raise

        try:
            if self._retry:
                resp = await self._retry.execute(
                    _do_forward,
                    attempts=self._config.forward_max_retries,
                    wait_initial=self._config.forward_retry_base_wait,
                    wait_max=self._config.forward_retry_max_wait,
                    exception_types=(TransientOGCError,),
                )
            else:
                resp = await _do_forward()
            context.provider_resp = resp
            logger.debug(
                f"[step:forward] OK job_id={context.job.id} status={resp.get('status')}"
            )
        except (TransientOGCError, OGCProcessException) as exc:
            ogc = exc if isinstance(exc, OGCProcessException) else exc
            await self._repo.mark_failed(
                context.job.id,
                reason=ogc.response.title,
                diagnostic=ogc.response.detail,
            )
            logger.warning(
                f"[step:forward] OGC error job_id={context.job.id} status={ogc.response.status}"
            )
            _halt_with_accepted(context)
        except Exception as exc:
            await self._repo.mark_failed(
                context.job.id, reason="Upstream Error", diagnostic=str(exc)
            )
            logger.error(
                f"[step:forward] unexpected error job_id={context.job.id} err={exc}"
            )
            _halt_with_accepted(context)


# ---------------------------------------------------------------------------
# Step 5: HandleProviderResponseStep
# ---------------------------------------------------------------------------


class HandleProviderResponseStep(PipelineStep):
    """Detect upstream HTTP error responses (≥ 400) with non-statusInfo bodies.

    Halts: when upstream returned an error and the body is not a statusInfo.
    In that case the upstream body is propagated directly to the client.
    """

    def __init__(self, repo: JobRepositoryPort) -> None:
        self._repo = repo

    async def process(self, context: JobExecutionContext) -> None:
        if context.provider_resp is None or context.job is None:
            return

        upstream_status = context.provider_resp.get("status")
        upstream_body = context.provider_resp.get("body")

        if not upstream_status or upstream_status < 400:
            return

        # If the body IS valid statusInfo, let DeriveStatusInfoStep handle it
        if _extract_status_info(upstream_body) is not None:
            return

        # Non-statusInfo error body: mark failed and propagate upstream response
        await self._repo.mark_failed(
            context.job.id, reason=f"Upstream {upstream_status}"
        )
        logger.debug(
            f"[step:handle-error] upstream_status={upstream_status} job_id={context.job.id}"
        )

        body = (
            upstream_body
            if isinstance(upstream_body, (dict, list))
            else {"error": str(upstream_body)}
        )
        context.response = {
            "status": upstream_status,
            "headers": {"Location": f"/jobs/{context.job.id}"},
            "body": body,
        }
        context.should_halt = True


# ---------------------------------------------------------------------------
# Step 6: DeriveStatusInfoStep
# ---------------------------------------------------------------------------


class DeriveStatusInfoStep(PipelineStep):
    """Select the appropriate status derivation strategy and run it.

    Sets: context.status_info, context.remote_status_url, context.remote_job_id,
          context.diagnostic
    """

    def __init__(self, orchestrator: StatusDerivationOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def process(self, context: JobExecutionContext) -> None:
        if (
            context.provider_resp is None
            or context.job is None
            or context.accepted_si is None
        ):
            return

        deriv_ctx = StatusDerivationContext(
            job=context.job,
            process_id=context.process_id,
            provider=context.provider,
            provider_resp=context.provider_resp,
            accepted_si=context.accepted_si,
        )
        result = await self._orchestrator.derive_status(deriv_ctx)

        if result.status_info and result.status_info.status != StatusCode.failed:
            # Normalise remote job ID to local UUID
            if result.status_info.jobID and result.status_info.jobID != context.job.id:
                if not context.job.remote_job_id:
                    context.job.remote_job_id = result.status_info.jobID
                result.status_info.jobID = context.job.id
            # Adopt accepted timestamps and enrich missing fields
            result.status_info.processID = context.process_id
            if result.status_info.created is None:
                result.status_info.created = context.accepted_si.created
            result.status_info.updated = datetime.now(timezone.utc)
            _enrich_status_info(result.status_info, context.accepted_si.created)

        _ensure_self_link(context.job.id, result.status_info)

        context.status_info = result.status_info
        context.remote_status_url = result.remote_status_url
        context.remote_job_id = result.remote_job_id
        context.diagnostic = result.diagnostic
        logger.debug(
            f"[step:derive] job_id={context.job.id} status={result.status_info.status if result.status_info else None}"
        )


# ---------------------------------------------------------------------------
# Step 7: FinalizeJobStep
# ---------------------------------------------------------------------------


class FinalizeJobStep(PipelineStep):
    """Persist the derived statusInfo, update remote identifiers, notify observers."""

    def __init__(
        self, repo: JobRepositoryPort, observers: List[JobStateObserver]
    ) -> None:
        self._repo = repo
        self._observers = observers

    async def process(self, context: JobExecutionContext) -> None:
        if context.job is None or context.status_info is None:
            return

        old_si = (
            JobStatusInfo(**context.job.status_info.model_dump())
            if context.job.status_info
            else None
        )

        if context.remote_status_url:
            context.job.remote_status_url = context.remote_status_url
        if context.remote_job_id:
            context.job.remote_job_id = context.remote_job_id
        if context.diagnostic:
            context.job.diagnostic = context.diagnostic

        if context.status_info.status == StatusCode.successful:
            _ensure_self_link(context.job.id, context.status_info)
            _ensure_results_link(context.job.id, context.status_info)

        context.job.apply_status_info(context.status_info)
        await self._repo.update(context.job)

        await _notify_observers_changed(
            self._observers, context.job, old_si, context.status_info
        )
        if context.job.is_in_terminal_state():
            await _notify_observers_completed(
                self._observers, context.job, context.status_info
            )

        logger.debug(
            f"[step:finalize] job_id={context.job.id} status={context.status_info.status} "
            f"remote_url={context.job.remote_status_url}"
        )


# ---------------------------------------------------------------------------
# Step 8: ShapeClientResponseStep
# ---------------------------------------------------------------------------


class ShapeClientResponseStep(PipelineStep):
    """Select the correct OGC client-facing HTTP response shape.

    Current implementation: always returns 201 + accepted statusInfo (async semantics).

    Full OGC table (to be implemented when sync execution is added):
      async  | any      | any   | any | 201 | application/json | statusInfo
      sync   | raw      | value | 1   | 200 | per output def   | raw output bytes
      sync   | raw      | value | >1  | 200 | multipart/related | one part per output
      sync   | raw      | ref   | 1   | 204 | —                | empty + Link headers
      sync   | document | value | any | 200 | application/json | results document

    When sync is implemented, this step reads context.execution_mode, context.response_mode,
    and context.output_specs to select the right row.
    """

    async def process(self, context: JobExecutionContext) -> None:
        if context.job is None or context.accepted_si is None:
            return
        # Async path (current only path): always 201 + accepted snapshot
        # The accepted snapshot lets the client poll for the true final status.
        context.response = {
            "status": 201,
            "headers": {"Location": f"/jobs/{context.job.id}"},
            "body": context.accepted_si.model_dump(),
        }
        logger.debug(
            f"[step:shape] job_id={context.job.id} execution_mode={context.execution_mode} -> 201 accepted"
        )


# ---------------------------------------------------------------------------
# Step 9: InitiatePollingStep
# ---------------------------------------------------------------------------


class InitiatePollingStep(PipelineStep):
    """Schedule background polling if the job is non-terminal and has a remote status URL.

    The schedule_callback is `JobManager._schedule_poll` — injected to keep
    the polling loop on the manager where it owns task lifecycle.
    """

    def __init__(self, schedule_callback: Callable[[str], None]) -> None:
        self._schedule = schedule_callback

    async def process(self, context: JobExecutionContext) -> None:
        if context.job is None or context.status_info is None:
            return
        if context.job.is_in_terminal_state():
            return
        if not context.job.remote_status_url:
            return
        self._schedule(context.job.id)
        logger.debug(f"[step:poll] scheduled polling job_id={context.job.id}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_canonical_via_validator(
    validator: ProcessIdValidatorPort,
    provider_name: str,
    configured_id: str,
) -> str:
    """Return the canonical process ID for *configured_id*, avoiding double-prefixing.

    If *configured_id* already carries *provider_name* as its prefix it is
    returned unchanged; otherwise the prefix is added via *validator.create()*.
    """
    try:
        existing_provider, _ = validator.extract(configured_id)
        if existing_provider == provider_name:
            return configured_id
    except ValueError:
        pass
    return validator.create(provider_name, configured_id)


def _halt(context: JobExecutionContext, status: int, title: str, detail: str) -> None:
    """Abort the pipeline with an OGC problem response."""
    context.response = {
        "status": status,
        "headers": {},
        "body": {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    }
    context.should_halt = True


def _halt_with_accepted(context: JobExecutionContext) -> None:
    """Abort pipeline, return 201 + accepted snapshot (forward failed but job was registered)."""
    if context.job and context.accepted_si:
        context.response = {
            "status": 201,
            "headers": {"Location": f"/jobs/{context.job.id}"},
            "body": context.accepted_si.model_dump(),
        }
    context.should_halt = True
