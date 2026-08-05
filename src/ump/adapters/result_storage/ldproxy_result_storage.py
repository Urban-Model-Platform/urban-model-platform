"""LdproxyResultStorage: the concrete ResultStoragePort adapter (V-6).

This is where the independently-built and independently-tested pieces of
Feature V finally meet:

    write_layers_to_gpkg (V-3)  ->  build_provider_entity_multi (V-4)
        ->  EntityConfigBackendPort (V-5a/b)  ->  ServiceRegistry (V-5c)

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

The adapter is a pure orchestrator: it holds no ldproxy YAML knowledge itself
(that lives in ldproxy_entities) and no persistence knowledge (that lives in
the backend). Everything it needs is injected at the composition root (V-8).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ump.adapters.result_storage.entity_config_backend import EntityConfigBackendPort
from ump.adapters.result_storage.gpkg_writer import write_layers_to_gpkg
from ump.adapters.result_storage.ldproxy_entities import (
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
    ) -> None:
        self._backend = backend
        self._registry = service_registry
        self._root = Path(root_path)
        # Normalise once so URL construction never produces a double slash.
        self._base_url = base_url.rstrip("/")
        self._native_crs = native_crs_epsg

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
            references: list[StoredReference] = []
            for payload in payloads:
                collection_id = collection_id_for(job_id, payload.output_id)
                await self._registry.register_collection(
                    collection_id, job_id, payload.output_id
                )
                registered.append(collection_id)
                references.append(self._reference_for(collection_id))
        except BaseException:
            # Roll back everything this call created so `exists` stays honest
            # and no orphan survives for a later retry to trip over.
            await self._rollback(job_id, gpkg_path, registered)
            raise

        logger.info(
            "[ldproxy] stored %d collection(s) for job_id=%s",
            len(references),
            job_id,
        )
        return references

    async def delete(self, job_id: str) -> None:
        """Remove a job's GeoPackage and provider entity (idempotent).

        NOTE (V-6 scope): this does *not* yet deregister the job's collections
        from the shared service entity — that, plus the anonymous/expiry cleanup
        wiring, is V-9. Until then a deleted job leaves its collection entries
        behind; they resolve to a missing provider and are harmless, but should
        not be mistaken for a bug.
        """
        await asyncio.to_thread(self._gpkg_path(job_id).unlink, True)  # missing_ok
        await asyncio.to_thread(self._backend.delete_provider_entity, job_id)

    async def exists(self, job_id: str) -> bool:
        """Return True if this job is fully stored.

        The GeoPackage is written first and rolled back on any failure, so its
        presence is a reliable "fully stored" signal for the coordinator's
        idempotency guard — and it lives on the filesystem for both backends.
        """
        return self._gpkg_path(job_id).exists()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gpkg_path(self, job_id: str) -> Path:
        # ldproxy resolves the provider's `database: {job_id}.gpkg` relative to
        # its `resources/features/` directory, so the file must live here.
        return self._root / "resources" / "features" / f"{job_id}.gpkg"

    def _reference_for(self, collection_id: str) -> StoredReference:
        collection_url = f"{self._base_url}/collections/{collection_id}"
        return StoredReference(
            collection_id=collection_id,
            collection_url=collection_url,
            items_url=f"{collection_url}/items",
        )

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
