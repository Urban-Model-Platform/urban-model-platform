"""ServiceRegistry: race-safe read-modify-write of the shared service entity.

Every stored job output must be registered as a collection in the shared
``ump-results`` ldproxy service entity.  Two or more jobs can complete at the
same instant, so registering a collection is a read-modify-write against a
*shared* resource — the classic lost-update hazard.

This module owns the read -> mutate collections map -> write sequence and the
retry loop that makes it safe. Two independent safety layers work together
(see REF-F5-result-storage.md, concern 2 decision note):

  1. An in-process ``asyncio.Lock`` serialises concurrent coroutines *within
     this process* so their read/write phases never interleave.
  2. The backend's optimistic-concurrency version token (mtime-hash for the
     filesystem backend, ``resourceVersion`` for Kubernetes) catches writers
     in *other* processes/pods. A stale write raises ``ConfigConflict``, which
     this module turns into a bounded retry: re-read, re-apply the mutation,
     write again.

Neither layer alone is sufficient in production (multiple pods each have
their own lock), so both are always active — the lock is cheap and shortens
the common case to zero retries even under single-process load (tests,
local dev, or a single-replica deployment).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

import yaml

from ump.adapters.result_storage.entity_config_backend import (
    ConfigConflict,
    EntityConfigBackendPort,
)
from ump.adapters.result_storage.ldproxy_entities import (
    build_collection_block,
    build_service_skeleton,
    to_yaml,
)
from ump.core.interfaces.result_storage import ResultStorageError

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Registers and deregisters ldproxy collections in the shared service entity.

    One instance per running UMP process, constructed once at the composition
    root (V-8) and shared by every job's storage flow — the ``asyncio.Lock``
    only serialises correctly if all callers share the same instance.
    """

    def __init__(
        self,
        backend: EntityConfigBackendPort,
        service_id: str = "ump-results",
        max_retries: int = 5,
    ) -> None:
        self._backend = backend
        self._service_id = service_id
        self._max_retries = max_retries
        # Guards the read-modify-write below; see module docstring.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def register_collection(
        self, collection_id: str, job_uuid: str, output_id: str
    ) -> None:
        """Add one collection to the shared service entity.

        Idempotent: registering the same ``collection_id`` again overwrites it
        with an identical block, so a retried V-6 storage step never produces
        a duplicate or inconsistent entry.
        """

        def mutate(service: dict) -> None:
            service.setdefault("collections", {}).update(
                build_collection_block(collection_id, job_uuid, output_id)
            )

        await self._read_modify_write(mutate)

    async def deregister_collection(self, collection_id: str) -> None:
        """Remove one collection from the shared service entity.

        Idempotent: removing a ``collection_id`` that isn't registered is a
        no-op, so cleanup (V-9) can run unconditionally without first checking
        whether the job was ever stored.
        """

        def mutate(service: dict) -> None:
            service.setdefault("collections", {}).pop(collection_id, None)

        await self._read_modify_write(mutate)

    # ------------------------------------------------------------------
    # Internal: the retry loop
    # ------------------------------------------------------------------

    async def _read_modify_write(self, mutate: Callable[[dict], None]) -> None:
        """Run one read -> mutate -> write cycle, retrying on ``ConfigConflict``.

        ``mutate`` receives the parsed service entity dict and edits it in
        place; it must not perform I/O itself.
        """
        async with self._lock:
            for attempt in range(1, self._max_retries + 1):
                current = await asyncio.to_thread(
                    self._backend.read_service_entity, self._service_id
                )
                if current is None:
                    # Absent entity == not bootstrapped yet (V-5a/V-5b
                    # contract). expected_version=None tells the backend to
                    # create rather than replace.
                    service = build_service_skeleton(self._service_id)
                    expected_version = None
                else:
                    text, expected_version = current
                    service = yaml.safe_load(text)
                    # Guard against a hand-edited entity missing the key;
                    # this is the one real invariant, not a defensive habit.
                    service.setdefault("collections", {})

                mutate(service)

                try:
                    await asyncio.to_thread(
                        self._backend.write_service_entity,
                        self._service_id,
                        to_yaml(service),
                        expected_version,
                    )
                    return
                except ConfigConflict:
                    logger.debug(
                        "[service_registry] version conflict on %s, "
                        "retrying (attempt %d/%d)",
                        self._service_id,
                        attempt,
                        self._max_retries,
                    )
                    continue

            raise ResultStorageError(
                f"Service entity {self._service_id!r} contention: "
                f"gave up after {self._max_retries} retries"
            )
