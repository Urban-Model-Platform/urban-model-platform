"""Service: ResultStorageCoordinator.

This is the UMP core's orchestrator for result storage.  It answers two
questions for every completed job:

  1. *Should* we store the result?
     Determined by the process's ``transmission_mode_policy`` and, for
     ``emulate-ref``, by the client's expressed intent in ``job.outputs_spec``.

  2. *How do we hand it off?*
     Fetch the remote bytes → extract one ``ResultPayload`` per storable output
     → delegate to ``ResultStoragePort.store()`` → inject the returned reference
     links into the job record.

The coordinator itself has no knowledge of GeoPackages, ldproxy, YAML files, or
Kubernetes.  Those are adapter concerns.  What it knows is:

  - The OGC document response structure (output IDs as top-level JSON keys).
  - How to extract a value from a qualified-value envelope
    ``{"value": ..., "mediaType": "..."}`` vs an inline value.
  - Which outputs the operator has configured for storage (``store-outputs``),
    or how to auto-detect storable outputs when no list is configured.
  - How to build OGC-conformant ``rel=results`` link objects from the
    ``StoredReference`` objects the adapter returns.
  - Per-policy failure handling: fall back to inline value for ``emulate-ref``,
    surface a 502 for ``emulate-ref-only``.

The coordinator is called eagerly at job completion by ``ResultStorageObserver``
(V-7).  It must not be called for jobs in non-terminal or non-successful states.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Optional

from ump.core.exceptions import OGCProcessException, OptimisticLockError
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.result_storage import (
    ResultPayload,
    ResultStorageError,
    ResultStoragePort,
    StoredReference,
)
from ump.core.models.job import Job
from ump.core.models.link import Link
from ump.core.models.providers_config import ProcessConfig

logger = logging.getLogger(__name__)

# The result-storage observer runs concurrently with the pipeline's finalize
# step; both persist the same job row. Optimistic locking makes one of them
# lose the race. We re-read and re-apply a bounded number of times so the
# stored reference is never silently dropped just because finalize committed
# first.
_PERSIST_MAX_ATTEMPTS = 5

# Default storage-fetch retry budget and per-attempt timeout. These are internal
# tuning values with sensible defaults that operators are not expected to change,
# so they live here next to the code that uses them rather than as global
# settings. The remote model server can report a job ``successful`` a moment
# before its ``/results`` endpoint is actually queryable (eventual consistency
# between the job-status store and the result assembly). A GET issued the
# instant we see ``successful`` therefore sometimes returns 404 (or a transient
# 5xx / timeout). Because storage runs eagerly on completion, we retry the fetch
# a bounded number of times with exponential backoff before giving up, so a few
# seconds' lag on the remote side no longer silently fails a required store.
# _FETCH_TIMEOUT is the per-attempt budget: a large result body the upstream is
# slowly assembling/streaming can take far longer than the small default HTTP
# client timeout, which would otherwise abort every attempt before the body
# arrives. Constructor kwargs still allow overrides (e.g. in tests).
_FETCH_MAX_ATTEMPTS = 8
_FETCH_BASE_WAIT = 2.0
_FETCH_MAX_WAIT = 30.0
_FETCH_TIMEOUT = 300.0
# HTTP statuses that mean "not ready yet / try again" rather than a permanent
# failure. 404 is included deliberately: right after ``successful`` it signals
# the result document has not materialised yet, not that it will never exist.
_TRANSIENT_FETCH_STATUSES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})

# Appended to a job's statusInfo message when the result was stored
# successfully but the store has not yet confirmed the collection is publicly
# queryable. The reference links are final and correct; the client may simply
# need to retry for a few moments while the store finishes publishing.
_PUBLICATION_PENDING_MESSAGE = (
    "Result stored; publication is finalizing and the reference links may take "
    "a few moments to become queryable."
)


class ResultStorageCoordinator:
    """Orchestrates the full store-result lifecycle for one completed job.

    Injected into ``JobManager`` at the composition root.  Called by
    ``ResultStorageObserver`` when a job transitions to ``successful``.
    """

    def __init__(
        self,
        storage_port: ResultStoragePort,
        http_client: HttpClientPort,
        providers: ProvidersPort,
        remote_auth: Optional[RemoteAuthPort] = None,
        fetch_max_attempts: int = _FETCH_MAX_ATTEMPTS,
        fetch_base_wait: float = _FETCH_BASE_WAIT,
        fetch_max_wait: float = _FETCH_MAX_WAIT,
        fetch_timeout: Optional[float] = _FETCH_TIMEOUT,
    ) -> None:
        self._storage = storage_port
        self._http = http_client
        self._providers = providers
        self._remote_auth = remote_auth
        # Storage-fetch retry budget. Defaults come from the module constants
        # above; callers/tests may override via constructor kwargs.
        self._fetch_max_attempts = fetch_max_attempts
        self._fetch_base_wait = fetch_base_wait
        self._fetch_max_wait = fetch_max_wait
        # Per-request timeout for each fetch attempt. Defaults to _FETCH_TIMEOUT
        # so a large, slowly-streamed result body is not cut off by the small
        # default HTTP client timeout; pass None to defer to the client default.
        self._fetch_timeout = fetch_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_store(self, job: Job, process_config: ProcessConfig) -> bool:
        """Return True if storage is required for this completed job.

        This is the single place where policy meets client intent.  The answer
        depends on two things:

        ``emulate-ref-only``
            Always store.  The policy mandates that every result goes through
            the store; no client preference can override this.

        ``emulate-ref``
            Store only if the client expressed ``transmissionMode: reference``
            for at least one output in their execute request.  If the client
            wanted ``value``, we proxy it directly without storing.

        Anything else (``pass-through``, ``value-only``)
            Never store.  The store is either not relevant or explicitly blocked.
        """
        return should_store_reference(job, process_config)

    async def coordinate(
        self,
        job: Job,
        process_config: ProcessConfig,
        repo: JobRepositoryPort,
    ) -> Optional[list[StoredReference]]:
        """Run the full store-result sequence for one completed job.

        Returns the ``StoredReference`` list this call persisted, or ``None``
        when nothing was (re-)stored (already stored — idempotent skip — or no
        storable payloads were found). V-11 uses the returned list to inspect
        each reference's ``publication_pending`` flag and decide whether the
        job may transition to ``successful`` yet.

        Steps:
          1. Guard: skip if already stored (idempotency on retry).
          2. Fetch the result bytes from the remote server.
          3. Extract per-output ``ResultPayload`` objects from the response.
          4. Hand payloads to the storage port.
          5. Build reference links from the returned ``StoredReference`` list.
          6. Inject the links into the job record and persist.

        On failure the result store is *required* (the client asked for a
        reference), so we never fall back to the inline value — that would
        risk emitting a huge payload the client deliberately avoided. Both
        ``emulate-ref`` (client explicitly requested reference) and
        ``emulate-ref-only`` re-raise ``ResultStorageError``; the completion
        observer records a storage-failure marker and ``GET /results``
        surfaces it as a results-unavailable error.
        """
        policy = process_config.transmission_mode_policy

        if await self._storage.exists(job.id):
            logger.debug("[storage] already stored, skipping — job_id=%s", job.id)
            return None

        try:
            payloads = await self._fetch_and_extract_payloads(job, process_config)
        except ResultStorageError as exc:
            self._handle_storage_failure(exc, job.id, policy, step="fetch")
            return None

        if not payloads:
            logger.warning(
                "[storage] no storable payloads found for job_id=%s — "
                "check store-outputs config and output media types",
                job.id,
            )
            return None

        try:
            references = await self._storage.store(job.id, payloads)
        except ResultStorageError as exc:
            self._handle_storage_failure(exc, job.id, policy, step="store")
            return None

        if references:
            await self._persist_stored_references(job, payloads, references, repo)
            logger.info(
                "[storage] stored %d collection(s) for job_id=%s",
                len(references),
                job.id,
            )
        return references

    # ------------------------------------------------------------------
    # Fetch and payload extraction
    # ------------------------------------------------------------------

    async def _fetch_and_extract_payloads(
        self,
        job: Job,
        process_config: ProcessConfig,
    ) -> list[ResultPayload]:
        """Fetch the remote result and split it into per-output payloads.

        The remote results endpoint is ``{provider.url}/jobs/{remote_job_id}/results``.
        We always call it with the auth headers resolved for this provider.
        """
        provider = self._providers.get_provider(job.provider or "")
        if provider is None:
            raise ResultStorageError(
                f"Cannot fetch results: provider '{job.provider}' not found in config"
            )

        results_url = (
            str(provider.url).rstrip("/") + f"/jobs/{job.remote_job_id}/results"
        )
        auth_headers = (
            self._remote_auth.resolve(provider.authentication).headers
            if self._remote_auth
            else {}
        )

        body_bytes, content_type = await self._fetch_results_with_retry(
            results_url, auth_headers or None, job.id
        )

        store_output_ids = _resolve_store_output_ids(
            policy=process_config.transmission_mode_policy,
            outputs_spec=job.outputs_spec,
            store_outputs_config=process_config.store_outputs,
        )

        return _extract_payloads(
            body_bytes=body_bytes,
            content_type=content_type,
            outputs_spec=job.outputs_spec,
            store_outputs_config=store_output_ids,
        )

    async def _fetch_results_with_retry(
        self,
        results_url: str,
        headers: Optional[dict[str, str]],
        job_id: str,
    ) -> tuple[bytes, str]:
        """Fetch the remote results document, retrying on transient failures.

        The remote can briefly answer 404 (or 5xx / time out) on ``/results``
        immediately after reporting the job ``successful`` — the status store
        and the result assembly are eventually consistent. We retry with
        exponential backoff so a short lag no longer downgrades a reference
        output to an inline value.

        Any error that is still failing after the last attempt — or that is
        non-transient (e.g. a genuine 4xx other than the ones listed in
        ``_TRANSIENT_FETCH_STATUSES``) — is translated into a
        ``ResultStorageError`` so the coordinator's policy-aware failure
        handling applies, rather than escaping uncaught through the observer.
        """
        last_exc: Optional[OGCProcessException] = None
        for attempt in range(1, self._fetch_max_attempts + 1):
            try:
                return await self._http.get_content(
                    results_url, timeout=self._fetch_timeout, headers=headers
                )
            except OGCProcessException as exc:
                status = getattr(exc.response, "status", None)
                is_transient = status in _TRANSIENT_FETCH_STATUSES
                if not is_transient or attempt == self._fetch_max_attempts:
                    raise ResultStorageError(
                        f"Failed to fetch results for storage from {results_url}: "
                        f"upstream status={status}"
                    ) from exc
                last_exc = exc
                wait = min(
                    self._fetch_base_wait * (2 ** (attempt - 1)),
                    self._fetch_max_wait,
                )
                logger.info(
                    "[storage] results not ready (status=%s) job_id=%s — "
                    "retry %d/%d in %.1fs",
                    status,
                    job_id,
                    attempt,
                    self._fetch_max_attempts,
                    wait,
                )
                await asyncio.sleep(wait)

        # Unreachable: the loop either returns, raises on the last attempt, or
        # raises for a non-transient status. Guard for type-checkers.
        raise ResultStorageError(
            f"Failed to fetch results for storage from {results_url}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _handle_storage_failure(
        self,
        exc: ResultStorageError,
        job_id: str,
        policy: str,
        step: str,
    ) -> None:
        """Handle a failure of a *required* result store — always fatal.

        A store only runs when the policy requires it and, under
        ``emulate-ref``, only when the client *explicitly* asked for
        ``transmissionMode: reference`` (see ``should_store``). In every case
        reaching this point the client deliberately requested a reference —
        typically because the inline value would be too large to return in the
        response body.

        Silently falling back to that inline value would therefore violate the
        client's intent and risk emitting a huge payload. Instead we always
        re-raise: the completion observer records a machine-readable
        storage-failure marker on the job (``_record_unavailable_result``), and
        ``GET /jobs/{id}/results`` surfaces it as a results-unavailable error
        rather than proxying the value. This holds identically for
        ``emulate-ref`` and ``emulate-ref-only``.
        """
        logger.error(
            "[storage] %s failed for job_id=%s (%s) — result unavailable, "
            "not falling back to inline value (policy=%s): %s",
            step,
            job_id,
            type(exc).__name__,
            policy,
            exc,
        )
        raise exc

    async def _record_downgrade(self, job: Job, repo: JobRepositoryPort) -> None:
        """Persist the emulate-ref value-fallback so clients can detect it.

        Best-effort: ``job.status_info`` is always set by the time the
        completion observer runs, but if it were ever missing there is nothing
        to annotate — skip rather than raise from a background hand-off.

        Uses the same re-read-and-retry strategy as reference persistence so a
        concurrent finalize commit cannot swallow the downgrade marker.
        """
        if job.status_info is None:
            return

        def apply(fresh: Job) -> Optional[Job]:
            if fresh.status_info is None:
                return None
            updated_info = fresh.status_info.model_copy(
                update={
                    "transmissionModeApplied": "value",
                    "message": _append_message(
                        fresh.status_info.message,
                        "Requested result reference could not be stored; "
                        "delivering the value inline instead.",
                    ),
                }
            )
            return fresh.model_copy(update={"status_info": updated_info})

        await self._persist_with_retry(job, repo, apply, what="downgrade marker")

    async def _persist_stored_references(
        self,
        job: Job,
        payloads: list[ResultPayload],
        references: list[StoredReference],
        repo: JobRepositoryPort,
    ) -> None:
        """Persist stored-reference links + stored_outputs, retrying on races.

        The transform is a pure function of ``payloads``/``references`` applied
        onto the freshest job snapshot, so re-applying after a re-read is safe
        and idempotent (``_apply_stored_references`` keys on href/output_id).
        """

        def apply(fresh: Job) -> Job:
            return _apply_stored_references(fresh, payloads, references)

        await self._persist_with_retry(job, repo, apply, what="stored references")

    async def _persist_with_retry(
        self,
        job: Job,
        repo: JobRepositoryPort,
        apply,
        what: str,
    ) -> None:
        """Read-modify-write ``job`` with bounded retries on optimistic-lock loss.

        ``apply`` receives the freshest job snapshot and returns the mutated
        copy to persist (or ``None`` to skip). On ``OptimisticLockError`` we
        re-read the job and re-apply, because the completion observer races the
        pipeline's finalize step for the same row.
        """
        current = job
        for attempt in range(1, _PERSIST_MAX_ATTEMPTS + 1):
            updated = apply(current)
            if updated is None:
                return
            try:
                await repo.update(updated)
                return
            except OptimisticLockError:
                fresh = await repo.get(job.id)
                if fresh is None:
                    logger.warning(
                        "[storage] job_id=%s vanished while persisting %s",
                        job.id,
                        what,
                    )
                    return
                current = fresh
                logger.debug(
                    "[storage] retry %d/%d persisting %s for job_id=%s "
                    "after concurrent modification",
                    attempt,
                    _PERSIST_MAX_ATTEMPTS,
                    what,
                    job.id,
                )
        logger.error(
            "[storage] gave up persisting %s for job_id=%s after %d attempts "
            "— stored data exists but the job record could not be annotated",
            what,
            job.id,
            _PERSIST_MAX_ATTEMPTS,
        )


# ---------------------------------------------------------------------------
# Pure helper functions (no side effects, easy to unit-test)
# ---------------------------------------------------------------------------


def should_store_reference(job: Job, process_config: ProcessConfig) -> bool:
    """Pure policy check: does this job require a stored reference?

    Extracted from ``ResultStorageCoordinator.should_store`` so callers that
    need the answer without a fully-constructed coordinator (e.g.
    ``JobManager``'s poll loop, deciding whether to gate the ``successful``
    transition per V-11) can reuse the exact same rule rather than
    duplicating it.

    ``emulate-ref-only``: always required.
    ``emulate-ref``: required only if the client asked for
    ``transmissionMode: reference`` on at least one output.
    Anything else: never required.
    """
    policy = process_config.transmission_mode_policy

    if policy == "emulate-ref-only":
        return True

    if policy == "emulate-ref":
        return _client_requested_reference(job.outputs_spec)

    return False


def _client_requested_reference(outputs_spec: Optional[dict]) -> bool:
    """Return True if any output in the execute request asked for reference.

    Looks at the ``transmissionMode`` key inside each entry of the
    ``outputs`` map from the original execute body.  Returns False when
    ``outputs_spec`` is None or empty (client did not specify a preference).
    """
    if not outputs_spec:
        return False
    for spec in outputs_spec.values():
        if isinstance(spec, dict) and spec.get("transmissionMode") == "reference":
            return True
    return False


def _resolve_store_output_ids(
    policy: str,
    outputs_spec: Optional[dict],
    store_outputs_config: Optional[list[str]],
) -> Optional[list[str]]:
    """Return the explicit list of output IDs to store, or None for auto-detect.

    Precedence:

    1. ``store_outputs_config`` — an operator's explicit ``store-outputs`` list
       in providers.yaml always wins. It may use dot-notation paths to reach
       nested outputs, so it is passed through verbatim.

    2. ``emulate-ref`` — storage is scoped to exactly the outputs the client
       asked to receive as ``transmissionMode: reference``. Outputs the client
       requested as ``value`` are proxied inline and must NOT be stored: mixing
       a non-geospatial ``value`` output (e.g. an ``application/json``
       classification table) into the store batch would raise
       ``UnsupportedResultError`` and, under emulate-ref, wrongly downgrade the
       *whole* job — including the geospatial output the client legitimately
       wanted as a reference. Returning only the reference-requested IDs keeps
       each output on its intended channel.

    3. ``emulate-ref-only`` (or any other store-activating policy) with no
       explicit config — return None so the extractor auto-detects every output
       in the document. Under emulate-ref-only every output is a reference by
       policy, so no client-driven narrowing applies.

    A pure function with no side effects for straightforward unit testing.
    """
    if store_outputs_config:
        return store_outputs_config

    if policy == "emulate-ref" and outputs_spec:
        reference_ids = [
            output_id
            for output_id, spec in outputs_spec.items()
            if isinstance(spec, dict) and spec.get("transmissionMode") == "reference"
        ]
        # None (not []) preserves auto-detect semantics if, defensively, no
        # reference output is found — though should_store guarantees at least
        # one under this policy.
        return reference_ids or None

    return None


def _extract_payloads(
    body_bytes: bytes,
    content_type: str,
    outputs_spec: Optional[dict],
    store_outputs_config: Optional[list[str]],
) -> list[ResultPayload]:
    """Convert the raw HTTP response body into a list of ``ResultPayload`` objects.

    Two response shapes are handled:

    ``application/json`` (document response)
        The body is a JSON object whose top-level keys are output IDs.  Each
        value is either an inline GeoJSON-style value or a qualified-value
        envelope ``{"value": ..., "mediaType": "..."}`` for binary formats.

    Anything else (raw response, single output)
        The body IS the output.  The output ID is inferred from
        ``store_outputs_config`` (first entry) or ``outputs_spec`` (first key).
        If neither is available the output is identified as ``"output"``.

    Returns an empty list if no storable outputs can be identified.
    """
    normalised_ct = (content_type or "").split(";")[0].strip().lower()

    # Discriminate document response vs raw single-output response.
    #
    # An OGC *document* response is served as ``application/json`` and its
    # top-level keys are output IDs. A raw single output (a GeoJSON
    # FeatureCollection, a FlatGeobuf, ...) is NOT a document — its bytes ARE
    # the output.
    #
    # Critically, ``application/geo+json`` must be treated as RAW, not as a
    # document: a GeoJSON FeatureCollection's top-level keys are ``type`` and
    # ``features``, which are not output IDs. The previous ``"json" in ct``
    # test wrongly matched ``geo+json`` and tried to iterate those keys,
    # yielding a bare ``features`` array that pyogrio cannot parse.
    if normalised_ct == "application/json":
        # Could be a genuine document response, OR a GeoJSON payload that the
        # server mislabelled as application/json. Parse once and disambiguate
        # on the JSON shape rather than trusting the header alone.
        try:
            document = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResultStorageError(
                f"Could not parse document response body as JSON: {exc}"
            ) from exc
        if _is_geojson_document(document):
            # Whole body is a single GeoJSON output despite the generic label.
            return _extract_single_raw_payload(
                body_bytes, "application/geo+json", store_outputs_config, outputs_spec
            )
        return _extract_payloads_from_document(
            document, store_outputs_config, outputs_spec
        )

    # Raw response: the entire body is one output (geo+json, flatgeobuf, ...).
    return _extract_single_raw_payload(
        body_bytes, normalised_ct, store_outputs_config, outputs_spec
    )


def _is_geojson_document(document: object) -> bool:
    """Return True if ``document`` is a GeoJSON object, not an OGC document response.

    GeoJSON objects carry a top-level ``type`` of ``FeatureCollection``,
    ``Feature``, or a geometry type. An OGC document response instead maps
    output IDs to values and has no such ``type`` discriminator. Detecting the
    GeoJSON shape lets us store a single geo output correctly even when a
    server labels it ``application/json`` instead of ``application/geo+json``.
    """
    if not isinstance(document, dict):
        return False
    geojson_types = {
        "FeatureCollection",
        "Feature",
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }
    return document.get("type") in geojson_types


def _extract_payloads_from_document(
    document: dict,
    store_outputs_config: Optional[list[str]],
    outputs_spec: Optional[dict],
) -> list[ResultPayload]:
    """Walk the OGC document response and build payloads for the outputs to store.

    Which outputs to store is determined by (in priority order):

    1. ``store_outputs_config`` — an explicit list of output IDs (dot-notation
       paths) the operator configured in providers.yaml.
    2. Auto-detection — every top-level key in the document is a candidate.
       The storage adapter will accept or raise ``UnsupportedResultError`` based
       on the output's resolved media_type.

    Dot-notation paths allow navigating into nested JSON when a remote server
    wraps the actual FeatureCollection inside a parent object:
      ``"results.voronoi"`` → ``document["results"]["voronoi"]``
    """
    output_ids = store_outputs_config if store_outputs_config else list(document.keys())

    payloads: list[ResultPayload] = []
    for output_id in output_ids:
        raw_value = _navigate_dot_path(document, output_id)
        if raw_value is None:
            logger.warning(
                "[storage] output '%s' not found in document response — skipping",
                output_id,
            )
            continue

        payload_bytes, media_type = _unwrap_output_value(raw_value, output_id)
        payloads.append(
            ResultPayload(
                output_id=output_id,
                body_bytes=payload_bytes,
                media_type=media_type,
            )
        )

    return payloads


def _extract_single_raw_payload(
    body_bytes: bytes,
    content_type: str,
    store_outputs_config: Optional[list[str]],
    outputs_spec: Optional[dict],
) -> list[ResultPayload]:
    """Wrap a raw (non-document) response body as a single ResultPayload.

    For ``response: raw`` with one output the server returns the bytes
    directly — there is no JSON envelope to parse.  We infer the output ID
    from the configuration or the outputs_spec.
    """
    # Resolve the output ID: explicit config wins, then first key from the
    # client's outputs map, then a safe default.
    if store_outputs_config:
        output_id = store_outputs_config[0]
    elif outputs_spec:
        output_id = next(iter(outputs_spec.keys()), "output")
    else:
        output_id = "output"

    return [
        ResultPayload(
            output_id=output_id,
            body_bytes=body_bytes,
            media_type=content_type or "application/octet-stream",
        )
    ]


def _navigate_dot_path(document: dict, path: str) -> Optional[object]:
    """Navigate a dot-notation path into a nested dict.

    ``"voronoi"``         → ``document["voronoi"]``
    ``"results.voronoi"`` → ``document["results"]["voronoi"]``

    Returns None if any segment is missing.
    """
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)  # type: ignore[assignment]
        if current is None:
            return None
    return current


def _unwrap_output_value(
    raw_value: object,
    output_id: str,
) -> tuple[bytes, str]:
    """Extract the byte payload and media_type from an OGC document output value.

    The OGC spec allows two forms for an output value in a document response:

    **Qualified value** (explicit mediaType):
        ``{"value": <data>, "mediaType": "application/geo+json"}``
        For binary formats the ``value`` is base64-encoded; we decode it here
        so the storage adapter always receives raw bytes.

    **Inline value** (no wrapper):
        The output IS the data directly — e.g. a GeoJSON FeatureCollection dict
        or a scalar.  Media type defaults to ``application/json`` for objects/arrays.

    Returns a ``(bytes, media_type)`` tuple.
    """
    # Qualified value envelope: {"value": ..., "mediaType": "..."}
    if isinstance(raw_value, dict) and "value" in raw_value:
        media_type: str = raw_value.get("mediaType", "application/octet-stream")
        inner = raw_value["value"]

        # Binary: base64-encoded string inside the JSON envelope.
        if isinstance(inner, str) and raw_value.get("encoding") == "base64":
            try:
                return base64.b64decode(inner), media_type
            except Exception as exc:
                raise ResultStorageError(
                    f"Output '{output_id}': base64 decoding failed: {exc}"
                ) from exc

        # JSON value: re-serialise to bytes.
        if isinstance(inner, (dict, list)):
            return json.dumps(inner).encode("utf-8"), media_type

        # Scalar string value.
        if isinstance(inner, str):
            return inner.encode("utf-8"), media_type

        return json.dumps(inner).encode("utf-8"), media_type

    # Inline value (no wrapper): value IS the FeatureCollection or other JSON.
    if isinstance(raw_value, (dict, list)):
        return json.dumps(raw_value).encode("utf-8"), "application/geo+json"

    # Scalar fallback: coerce to string bytes.
    return str(raw_value).encode("utf-8"), "text/plain"


def _apply_stored_references(
    job: Job,
    payloads: list[ResultPayload],
    references: list[StoredReference],
) -> Job:
    """Return a copy of the job with stored references applied.

    Two client-visible effects, both additive and idempotent (safe to call
    again for the same job, e.g. after an observer retry):

    1. ``job.stored_outputs[output_id]`` — the structured record ``GET
       /jobs/{id}/results`` (V-10) reads to build the OGC ``document``
       response, keyed by output_id so no href-parsing is needed.
    2. ``status_info.links`` — one ``rel="item"`` link per stored output, so
       the reference is already visible to a client polling ``GET
       /jobs/{id}`` without waiting for a ``/results`` call.

    Bugfix note: the previous implementation appended to ``job.links`` (the
    internal, non-client-facing field) while the API serves
    ``job.status_info.links`` to callers — the stored reference therefore
    never reached a client.  This corrects that wiring.

    ``store()`` returns references in the same order as the ``payloads`` list
    it was given (see ``LdproxyResultStorage.store``), so ``zip`` pairs each
    reference back up with the output_id that produced it.
    """
    new_stored_outputs = dict(job.stored_outputs or {})
    new_links = list((job.status_info.links if job.status_info else None) or [])
    existing_hrefs = {link.href for link in new_links}

    any_pending = False
    for payload, ref in zip(payloads, references):
        new_stored_outputs[payload.output_id] = {
            "collection_id": ref.collection_id,
            "collection_url": ref.collection_url,
            "items_url": ref.items_url,
            "publication_pending": ref.publication_pending,
        }
        any_pending = any_pending or ref.publication_pending
        if ref.items_url not in existing_hrefs:
            new_links.append(
                Link(
                    href=ref.items_url,
                    rel="item",
                    type="application/geo+json",
                    title=f"Stored result: {payload.output_id}",
                )
            )
            existing_hrefs.add(ref.items_url)

    updates: dict = {"stored_outputs": new_stored_outputs}
    if job.status_info is not None:
        status_updates: dict = {"links": new_links}
        # Honest signalling: the reference URLs are final and correct, but the
        # store has not yet confirmed the collection is queryable. Tell the
        # client it may need to retry for a moment rather than implying the
        # link is immediately live.
        if any_pending:
            status_updates["message"] = _append_message(
                job.status_info.message, _PUBLICATION_PENDING_MESSAGE
            )
        updates["status_info"] = job.status_info.model_copy(update=status_updates)

    return job.model_copy(update=updates)


def _append_message(existing: Optional[str], addition: str) -> str:
    """Append *addition* to an existing statusInfo message, or return it alone."""
    if not existing:
        return addition
    if addition in existing:
        return existing  # idempotent: do not duplicate on a re-run
    return f"{existing} {addition}"
