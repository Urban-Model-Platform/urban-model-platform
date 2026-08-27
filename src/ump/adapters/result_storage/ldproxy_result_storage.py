"""LdproxyResultStorage: the concrete ResultStoragePort adapter.

This is where the independently-built and independently-tested pieces of
Feature V finally meet:

    write_layers_to_gpkg ->  build_provider_entity_multi
        ->  EntityConfigBackendPort  ->  ServiceRegistry

Storing one job's results is a three-stage write whose *order* is the whole
point:

    1. write the GeoPackage      (the data every reference points at)
    2. write the provider entity (references the .gpkg + one type per output)
    3. register each collection  (references the provider)

Each stage references only the previous one, so a crash between stages can
only ever leave a *forward*-dangling artifact (an orphan .gpkg, or an orphan
provider) — never a collection pointing at something that doesn't exist. ldproxy,
which hot-reloads the store, therefore never publishes a broken collection.

To keep that guarantee even for *partial success*, ``store`` is transactional:
if stage 2 or 3 fails, it rolls back the .gpkg and provider it already wrote
before re-raising. That way ``exists`` (which tests for the .gpkg) stays an
honest "fully stored" signal and no orphan survives to confuse a later retry.

``delete`` reverses the write order — deregister collections, then the
provider entity, then the .gpkg — so ldproxy never sees a collection whose
provider has already vanished. Reconstructing *which* collections belong to a
job requires knowing its output ids, which nothing else persists; rather than
add a ``read_provider_entity`` method to ``EntityConfigBackendPort`` (forcing
both backends to support reading back and re-parsing YAML they only ever
wrote) ``store`` also writes a tiny sidecar **manifest** — a JSON file listing
the job's output ids — next to the GeoPackage. The manifest is always a plain
file on the shared filesystem, regardless of which ``EntityConfigBackendPort``
is configured, exactly like the GeoPackage itself (see notes: binary
/ large artifacts always live on the file share, never in a ConfigMap).

The adapter is a pure orchestrator: it holds no ldproxy YAML knowledge itself
(that lives in ldproxy_entities) and no persistence knowledge (that lives in
the backend). Everything it needs is injected at the composition root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from ump.adapters.result_storage.atomic_fs import atomic_write_text
from ump.adapters.result_storage.entity_config_backend import EntityConfigBackendPort
from ump.adapters.result_storage.gpkg_writer import (
    write_layers_to_gpkg,
    write_seed_gpkg,
)
from ump.adapters.result_storage.ldproxy_entities import (
    DEFAULT_PROVIDER_DATABASE,
    build_default_provider_entity,
    build_provider_entity_multi,
    collection_id_for,
    to_yaml,
)
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.core.interfaces.result_storage import (
    ResultPayload,
    ResultStoragePort,
    StoredReference,
)

logger = logging.getLogger(__name__)


class LdproxyResultStorage(ResultStoragePort):
    """Store job results as a GeoPackage + ldproxy entities, publish via ldproxy."""

    def __init__(
        self,
        backend: EntityConfigBackendPort,
        service_registry: ServiceRegistry,
        root_path: str | Path,
        base_url: str,
        native_crs_epsg: int = 4326,
        service_id: str = "ump-results",
        internal_url: str | None = None,
        confirm_max_attempts: int = 6,
        confirm_base_wait: float = 1.5,
        confirm_max_wait: float = 8.0,
    ) -> None:
        self._backend = backend
        self._registry = service_registry
        self._root = Path(root_path)
        # Normalise once so URL construction never produces a double slash.
        self._base_url = base_url.rstrip("/")
        self._native_crs = native_crs_epsg
        self._service_id = service_id
        # URL ldproxy is reachable at *from inside UMP* (container-network
        # name), used only for the post-store liveness probe below. It differs
        # from ``base_url``, which is the public/client-facing URL baked into
        # returned references (e.g. ``localhost:7080`` for a browser) and is
        # typically NOT reachable from within the UMP container. When unset,
        # confirmation degrades to a single best-effort re-touch (no probe).
        self._internal_url = internal_url.rstrip("/") if internal_url else None
        # Post-store publication-confirmation budget. ldproxy watches the store
        # and reloads the *service* and *provider* entities independently.
        # Because ``store`` writes the provider a few milliseconds before the
        # service that references it, ldproxy can evaluate the service's
        # collection while the provider is still a temp file — it then disables
        # that collection ("feature type not found in the provider schema") and
        # does NOT re-evaluate it until the service entity changes again. We
        # re-touch the service entity (with backoff) after registration until
        # the collection is confirmed live (via the probe) or this budget is
        # exhausted (see _confirm_publication).
        self._confirm_max_attempts = confirm_max_attempts
        self._confirm_base_wait = confirm_base_wait
        self._confirm_max_wait = confirm_max_wait

    # ------------------------------------------------------------------
    # ResultStoragePort
    # ------------------------------------------------------------------

    async def store(
        self, job_id: str, payloads: list[ResultPayload]
    ) -> list[StoredReference]:
        """Store all outputs of *job_id* and return one reference per output.

        Transactional: on any failure after the GeoPackage is written, the
        .gpkg and provider entity written so far are removed before the error
        propagates, so a failed store leaves nothing behind.
        """
        gpkg_path = self._gpkg_path(job_id)
        gpkg_path.parent.mkdir(parents=True, exist_ok=True)

        # Stage 1: one GeoPackage with one layer per output (atomic in itself).
        layers = [(p.output_id, p.body_bytes, p.media_type) for p in payloads]
        schemas = await asyncio.to_thread(
            write_layers_to_gpkg, layers, gpkg_path, self._native_crs
        )

        # Record which output ids this job stored, so `delete` can later
        # reconstruct the collection ids to deregister without having to read
        # and re-parse the provider entity YAML (see module docstring).
        output_ids = [p.output_id for p in payloads]
        await asyncio.to_thread(self._write_manifest, job_id, output_ids)

        registered: list[str] = []
        try:
            # Stage 2: one provider entity with one `types` entry per output.
            provider_yaml = to_yaml(
                build_provider_entity_multi(job_id, schemas, self._native_crs)
            )
            await asyncio.to_thread(
                self._backend.write_provider_entity, job_id, provider_yaml
            )

            # Stage 3: register one collection per output in the shared service.
            ordered_collection_ids: list[str] = []
            for payload in payloads:
                collection_id = collection_id_for(job_id, payload.output_id)
                await self._registry.register_collection(
                    collection_id, job_id, payload.output_id
                )
                registered.append(collection_id)
                ordered_collection_ids.append(collection_id)
        except BaseException:
            # Roll back everything this call created so `exists` stays honest
            # and no orphan survives for a later retry to trip over.
            await self._rollback(job_id, gpkg_path, registered)
            raise

        # Confirm ldproxy actually published each collection before reporting
        # success. ldproxy hot-reloads provider and service entities
        # independently, so a collection can be transiently disabled until the
        # provider finishes loading (see _confirm_publication). This runs in the
        # background completion observer, not the client request path, so the
        # bounded wait costs the client nothing and buys them a link that works
        # on first use.
        pending = await self._confirm_publication(
            job_id, [p.output_id for p in payloads], ordered_collection_ids
        )

        references = [
            self._reference_for(cid, publication_pending=cid in pending)
            for cid in ordered_collection_ids
        ]

        logger.info(
            "[ldproxy] stored %d collection(s) for job_id=%s%s",
            len(references),
            job_id,
            f" ({len(pending)} still finalizing)" if pending else "",
        )
        return references

    async def delete(self, job_id: str) -> None:
        """Remove everything ``store`` created for *job_id* (idempotent).

        Reverses the write order from ``store``: deregister collections
        first, then the provider entity, then the GeoPackage and the
        manifest that named its collections. This ordering matters for the
        same reason it does when writing — ldproxy must never observe a
        collection whose provider (or provider's data) has already vanished.

        Idempotent and safe to call unconditionally, including for a job that
        was never stored (e.g. ``NullResultStorage`` was active at the time,
        or the job failed before storage): a missing manifest means there is
        nothing to deregister, and every underlying removal call is itself
        idempotent (see ``EntityConfigBackendPort`` docstrings).

        Best-effort per step: a failure deregistering one collection does not
        prevent attempting the rest, or the provider/gpkg/manifest removal —
        cleanup should make maximum forward progress rather than abandon
        everything on the first error. Each failure is logged; the caller
        (the cleanup service, V-9) treats the overall operation as best-effort
        and proceeds with deleting the job record regardless.
        """
        output_ids = await asyncio.to_thread(self._read_manifest, job_id)
        for output_id in output_ids:
            collection_id = collection_id_for(job_id, output_id)
            try:
                await self._registry.deregister_collection(collection_id)
            except Exception:
                logger.warning(
                    "[ldproxy] delete: could not deregister collection %s "
                    "for job_id=%s",
                    collection_id,
                    job_id,
                )

        try:
            await asyncio.to_thread(self._backend.delete_provider_entity, job_id)
        except Exception:
            logger.warning(
                "[ldproxy] delete: could not remove provider entity for job_id=%s",
                job_id,
            )

        await asyncio.to_thread(self._gpkg_path(job_id).unlink, True)  # missing_ok
        await asyncio.to_thread(self._manifest_path(job_id).unlink, True)  # missing_ok

    async def exists(self, job_id: str) -> bool:
        """Return True if this job is fully stored.

        The GeoPackage is written first and rolled back on any failure, so its
        presence is a reliable "fully stored" signal for the coordinator's
        idempotency guard — and it lives on the filesystem for both backends.
        """
        return self._gpkg_path(job_id).exists()

    async def ensure_default_provider(self) -> None:
        """Create the shared service's default feature provider if absent.

        ldproxy (verified on 3.6.x and 4.6.x) will not start an ``OGC_API``
        service unless it can resolve a default feature provider whose id equals
        the service id, backed by a real, connectable GeoPackage
        (``initFailFast`` validates the file at startup). Per-job providers
        alone do not satisfy this, even though every published collection
        overrides ``featureProvider`` per-collection — so without this the
        service never starts and no stored result is reachable.

        Idempotent and cheap to call on every startup: the seed GeoPackage is
        written only when missing, and the provider entity write is an atomic
        overwrite of identical content. The seed's single ``default`` type is
        never registered as a collection, so it stays invisible under
        ``/collections``.
        """
        seed_path = self._root / "resources" / "features" / DEFAULT_PROVIDER_DATABASE
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        if not seed_path.exists():
            await asyncio.to_thread(write_seed_gpkg, seed_path, self._native_crs)

        provider_yaml = to_yaml(
            build_default_provider_entity(self._service_id, self._native_crs)
        )
        await asyncio.to_thread(
            self._backend.write_provider_entity, self._service_id, provider_yaml
        )
        logger.info(
            "[ldproxy] ensured default feature provider '%s' (seed=%s)",
            self._service_id,
            seed_path.name,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gpkg_path(self, job_id: str) -> Path:
        # ldproxy resolves the provider's `database: {job_id}.gpkg` relative to
        # its `resources/features/` directory, so the file must live here.
        return self._root / "resources" / "features" / f"{job_id}.gpkg"

    def _manifest_path(self, job_id: str) -> Path:
        # Kept next to the GeoPackage — same directory, same always-on-the-
        # filesystem guarantee, regardless of the configured entity backend.
        return self._root / "resources" / "features" / f"{job_id}.manifest.json"

    def _write_manifest(self, job_id: str, output_ids: list[str]) -> None:
        path = self._manifest_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"output_ids": output_ids}))

    def _read_manifest(self, job_id: str) -> list[str]:
        """Return the stored output ids for *job_id*, or [] if never stored.

        A missing or unreadable manifest is treated as "nothing to clean up"
        rather than an error — ``delete`` must remain safe to call for jobs
        that were never stored (see its docstring), and a corrupted manifest
        should not block the rest of cleanup (gpkg/provider removal still run).
        """
        path = self._manifest_path(job_id)
        try:
            data = json.loads(path.read_text())
            return list(data.get("output_ids", []))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "[ldproxy] delete: could not read manifest for job_id=%s: %s",
                job_id,
                exc,
            )
            return []

    def _reference_for(
        self, collection_id: str, publication_pending: bool = False
    ) -> StoredReference:
        collection_url = f"{self._base_url}/collections/{collection_id}"
        # liveness_url (V-11): built from the *internal* base URL so the probe
        # works from inside the UMP container, where the public base_url is
        # typically unreachable (e.g. it points at localhost for a browser).
        # None when no internal URL is configured — callers then fall back to
        # items_url, or rely on this adapter's own ``publication_pending``
        # signal (which already reflects an internal probe when available).
        liveness_url = (
            f"{self._internal_url}/collections/{collection_id}/items?limit=1"
            if self._internal_url
            else None
        )
        return StoredReference(
            collection_id=collection_id,
            collection_url=collection_url,
            items_url=f"{collection_url}/items",
            publication_pending=publication_pending,
            liveness_url=liveness_url,
        )

    # ------------------------------------------------------------------
    # ldproxy reload-ordering: confirm publication
    # ------------------------------------------------------------------

    async def _confirm_publication(
        self,
        job_id: str,
        output_ids: list[str],
        collection_ids: list[str],
    ) -> set[str]:
        """Wait until ldproxy publishes each collection; return the unconfirmed.

        Why this is needed
        ------------------
        ldproxy hot-reloads provider and service entities independently by
        watching the store directory. ``store`` writes the provider entity a
        few milliseconds before the service entity that references it, so
        ldproxy can evaluate the service's new collection while the provider is
        still only a temp file. It then *disables* that collection ("feature
        type not found in the provider schema") and does NOT re-evaluate it
        until the service entity changes again — even though the provider
        becomes AVAILABLE moments later. Confirmed in production logs.

        Strategy
        --------
        Re-touch the service entity (an idempotent re-registration — the exact
        "service change" ldproxy needs to re-evaluate) with exponential
        backoff, probing after each attempt, until every collection is live or
        the budget is exhausted. This runs in the background completion
        observer, not the client request path, so the bounded wait is free to
        the client and yields a link that works on first use.

        Honest fallback
        ---------------
        Any collection still not confirmed live when the budget is exhausted is
        returned in the pending set. The caller flags its ``StoredReference`` as
        ``publication_pending`` so the coordinator can tell the client the
        result is still finalizing, rather than pretending it is immediately
        queryable. The collection still self-heals on the next service change.

        Degraded mode (no probe)
        ------------------------
        When ``internal_url`` is unset UMP cannot observe liveness. It then does
        a single best-effort re-touch after a short wait (enough for a typical
        single-provider load) and reports nothing as pending — we must not
        alarm the client about a state we cannot actually verify.

        Never raises: a store that persisted its data and entities is a
        success; confirmation is a best-effort refinement on top.
        """
        if not collection_ids:
            return set()

        pairs = list(zip(collection_ids, output_ids))

        if self._internal_url is None:
            await asyncio.sleep(self._confirm_base_wait)
            await self._retouch(job_id, pairs)
            return set()

        pending = await self._probe_pending(pairs)
        for attempt in range(1, self._confirm_max_attempts + 1):
            if not pending:
                return set()

            wait = min(
                self._confirm_base_wait * (2 ** (attempt - 1)),
                self._confirm_max_wait,
            )
            logger.debug(
                "[ldproxy] confirming job_id=%s: %d collection(s) not yet live, "
                "re-touching service (attempt %d/%d, next check in %.1fs)",
                job_id,
                len(pending),
                attempt,
                self._confirm_max_attempts,
                wait,
            )
            await self._retouch(job_id, [pair for pair in pairs if pair[0] in pending])
            await asyncio.sleep(wait)
            pending = await self._probe_pending(pairs)

        if pending:
            logger.warning(
                "[ldproxy] job_id=%s: %d collection(s) not confirmed live after "
                "%d attempt(s); flagged publication_pending, will self-heal on "
                "the next reload",
                job_id,
                len(pending),
                self._confirm_max_attempts,
            )
        return pending

    async def _retouch(self, job_id: str, pairs: list[tuple[str, str]]) -> None:
        """Re-register (idempotently) each collection to nudge ldproxy's reload."""
        for collection_id, output_id in pairs:
            try:
                await self._registry.register_collection(
                    collection_id, job_id, output_id
                )
            except Exception:
                logger.warning(
                    "[ldproxy] confirm: re-touch failed for collection %s",
                    collection_id,
                )

    async def _probe_pending(self, pairs: list[tuple[str, str]]) -> set[str]:
        """Return the collection_ids not currently published by ldproxy."""
        pending: set[str] = set()
        for collection_id, _output_id in pairs:
            live = await asyncio.to_thread(self._probe_collection_live, collection_id)
            if not live:
                pending.add(collection_id)
        return pending

    def _probe_collection_live(self, collection_id: str) -> bool:
        """Return True if ldproxy currently publishes *collection_id*.

        Blocking; always called via ``asyncio.to_thread``. Uses the internal
        (container-network) ldproxy URL. Any error or non-2xx response is
        treated as "not live yet" so the caller keeps re-touching.
        """
        url = f"{self._internal_url}/collections/{collection_id}?f=json"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    async def _rollback(
        self, job_id: str, gpkg_path: Path, registered: list[str]
    ) -> None:
        """Best-effort removal of everything a failed ``store`` created.

        Undoes stage 3 (any collections already registered), then stage 2
        (provider entity), then stage 1 (the .gpkg). Rollback must not mask the
        original error, so each step swallows its own exceptions and only logs —
        the caller re-raises the real cause.
        """
        for collection_id in registered:
            try:
                await self._registry.deregister_collection(collection_id)
            except Exception:
                logger.warning(
                    "[ldproxy] rollback: could not deregister collection %s",
                    collection_id,
                )
        try:
            await asyncio.to_thread(self._backend.delete_provider_entity, job_id)
        except Exception:
            logger.warning(
                "[ldproxy] rollback: could not remove provider entity for %s", job_id
            )
        try:
            await asyncio.to_thread(gpkg_path.unlink, True)  # missing_ok
        except OSError:
            logger.warning("[ldproxy] rollback: could not remove %s", gpkg_path)
        try:
            await asyncio.to_thread(self._manifest_path(job_id).unlink, True)
        except OSError:
            logger.warning(
                "[ldproxy] rollback: could not remove manifest for job_id=%s", job_id
            )
