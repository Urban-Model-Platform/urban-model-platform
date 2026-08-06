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

import base64
import json
import logging
from typing import Optional

from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.result_storage import (
    NullResultStorage,
    ResultPayload,
    ResultStorageError,
    ResultStoragePort,
    StoredReference,
    UnsupportedResultError,
)
from ump.core.models.job import Job
from ump.core.models.link import Link
from ump.core.models.providers_config import ProcessConfig

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._storage = storage_port
        self._http = http_client
        self._providers = providers
        self._remote_auth = remote_auth

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
        policy = process_config.transmission_mode_policy

        if policy == "emulate-ref-only":
            return True

        if policy == "emulate-ref":
            return _client_requested_reference(job.outputs_spec)

        return False

    async def coordinate(
        self,
        job: Job,
        process_config: ProcessConfig,
        repo: JobRepositoryPort,
    ) -> None:
        """Run the full store-result sequence for one completed job.

        Steps:
          1. Guard: skip if already stored (idempotency on retry).
          2. Fetch the result bytes from the remote server.
          3. Extract per-output ``ResultPayload`` objects from the response.
          4. Hand payloads to the storage port.
          5. Build reference links from the returned ``StoredReference`` list.
          6. Inject the links into the job record and persist.

        On failure the error is handled according to ``transmission_mode_policy``:
          - ``emulate-ref``:      log a warning and return quietly — the client
            will receive the value inline when they poll ``/results``.
          - ``emulate-ref-only``: re-raise so the caller can surface a 502.
        """
        policy = process_config.transmission_mode_policy

        if await self._storage.exists(job.id):
            logger.debug("[storage] already stored, skipping — job_id=%s", job.id)
            return

        try:
            payloads = await self._fetch_and_extract_payloads(job, process_config)
        except ResultStorageError as exc:
            return self._handle_storage_failure(exc, job.id, policy, step="fetch")

        if not payloads:
            logger.warning(
                "[storage] no storable payloads found for job_id=%s — "
                "check store-outputs config and output media types",
                job.id,
            )
            return

        try:
            references = await self._storage.store(job.id, payloads)
        except ResultStorageError as exc:
            return self._handle_storage_failure(exc, job.id, policy, step="store")

        if references:
            updated_job = _inject_reference_links(job, references)
            await repo.update(updated_job)
            logger.info(
                "[storage] stored %d collection(s) for job_id=%s",
                len(references),
                job.id,
            )

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

        body_bytes, content_type = await self._http.get_content(
            results_url, headers=auth_headers or None
        )

        return _extract_payloads(
            body_bytes=body_bytes,
            content_type=content_type,
            outputs_spec=job.outputs_spec,
            store_outputs_config=process_config.store_outputs,
        )

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
        """Decide what to do when storage fails, based on the configured policy.

        ``emulate-ref``:
            The client was allowed to request either value or reference.  If we
            cannot store, we fall back silently — the value will be returned
            inline when the client polls ``/results``.  Log a warning so
            operators know storage was attempted and failed.

            Known gap (V-10): ``should_store`` only returns True here when the
            client *explicitly* asked for ``transmissionMode: reference``, so
            this branch is exactly the case "client asked for a reference and
            silently gets a value".  The fallback itself is correct — the value
            channel is open under this policy, so delivering the result beats
            failing a successfully computed job.  But the downgrade is currently
            invisible outside the logs.  V-10 must report it back to the caller
            so it can be persisted on the job and made machine-detectable in the
            statusInfo / ``GET /results`` response.

        ``emulate-ref-only``:
            The policy promises that results are *always* stored.  We cannot
            satisfy that promise, so re-raise.  The caller surfaces this as a
            results-unavailable error to the client.
        """
        if policy == "emulate-ref":
            logger.warning(
                "[storage] %s failed for job_id=%s (%s) — "
                "falling back to inline value: %s",
                step,
                job_id,
                type(exc).__name__,
                exc,
            )
            return  # silent fallback; method returns None

        # emulate-ref-only: we promised a reference, so failure is fatal.
        raise exc


# ---------------------------------------------------------------------------
# Pure helper functions (no side effects, easy to unit-test)
# ---------------------------------------------------------------------------


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

    if "json" in normalised_ct:
        # Document response: one JSON object with output_id → value mapping.
        try:
            document = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResultStorageError(
                f"Could not parse document response body as JSON: {exc}"
            ) from exc
        return _extract_payloads_from_document(
            document, store_outputs_config, outputs_spec
        )
    else:
        # Raw response: the entire body is one output.
        return _extract_single_raw_payload(
            body_bytes, normalised_ct, store_outputs_config, outputs_spec
        )


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


def _inject_reference_links(job: Job, references: list[StoredReference]) -> Job:
    """Return a copy of the job with OGC ``rel=results`` links added.

    One link is added per ``StoredReference``, pointing at the OGC API Features
    ``/items`` endpoint.  This link is what clients receive when they poll
    ``GET /jobs/{id}`` after a reference-mode job completes.

    The original job object is not mutated; a ``model_copy`` is returned so
    callers can safely persist the new version.
    """
    new_links = list(job.links)

    for ref in references:
        new_links.append(
            Link(
                href=ref.items_url,
                rel="results",
                type="application/geo+json",
                title=f"Stored result collection: {ref.collection_id}",
            )
        )

    return job.model_copy(update={"links": new_links})
