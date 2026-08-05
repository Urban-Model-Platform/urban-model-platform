"""Where ldproxy entity YAML files physically live.

ldproxy is driven by two kinds of small YAML "entity" files:

  * one **provider** entity per job — tells ldproxy how to read that job's
    GeoPackage;
  * one **shared service** entity (``ump-results``) — the common API root whose
    ``collections`` map every job appends to.

*Where* those YAML files are written depends on the deployment:

  * local / Docker → plain files on the mounted store directory;
  * Kubernetes     → ConfigMaps managed through the k8s API.

This module defines the seam between "what YAML to write" (decided elsewhere:
``ldproxy_entities``) and "how to persist it" (the concrete backends).  The rest
of the result-storage code depends only on ``EntityConfigBackendPort`` and never
learns whether it is talking to a filesystem or to Kubernetes.

Concurrency model — optimistic, not lock-based
----------------------------------------------
The provider entity is written by exactly one job, so it needs no coordination.

The **service** entity is shared: many jobs append their collection to the same
document.  If two writers read-modify-write it concurrently one update can be
lost.  Rather than bolt an external lock onto every environment, we let each
backend use the concurrency primitive it already has:

  * Kubernetes gives every ConfigMap a ``resourceVersion``;
  * the filesystem backend derives a cheap version token from the file's
    modification time.

Both express the same contract through ``read_service_entity`` /
``write_service_entity``: read returns the current ``(text, version)``; write
only succeeds if the version still matches, otherwise it raises
``ConfigConflict`` and the caller (the service registry, V-5c) retries the
read-modify-write.  This single contract works unchanged for both backends, so
nothing downstream has to special-case the environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ConfigConflict(Exception):
    """The service entity changed since it was read — the write was rejected.

    Raised by ``write_service_entity`` when ``expected_version`` no longer
    matches the stored version.  The service registry catches this and retries
    its read-modify-write loop with the fresh version.
    """


class EntityConfigBackendPort(ABC):
    """Persist ldproxy entity YAML, abstracting filesystem vs. Kubernetes.

    Methods are synchronous: filesystem I/O and the official Kubernetes client
    are both blocking.  The async result-storage adapter (V-6) offloads calls
    to a worker thread so the event loop never stalls on store latency.
    """

    # -- Provider entity: single-writer, no version coordination needed -----

    @abstractmethod
    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        """Create or overwrite the provider entity for one job.

        Idempotent: re-writing the same ``provider_id`` replaces the previous
        content.  ``provider_id`` is the job UUID.
        """

    @abstractmethod
    def delete_provider_entity(self, provider_id: str) -> None:
        """Remove a job's provider entity.

        Idempotent: deleting an entity that does not exist is a no-op, so
        cleanup (V-9) can run without first checking for existence.
        """

    # -- Shared service entity: optimistic concurrency ----------------------

    @abstractmethod
    def read_service_entity(self, service_id: str) -> tuple[str, str] | None:
        """Return the shared service entity as ``(yaml_text, version)``.

        Returns ``None`` when the entity does not exist yet — the caller treats
        that as the signal to bootstrap it from a skeleton.  ``version`` is an
        opaque token whose only guarantee is that it changes on every write; it
        must be passed back to ``write_service_entity`` as ``expected_version``.
        """

    @abstractmethod
    def write_service_entity(
        self, service_id: str, yaml_text: str, expected_version: str | None
    ) -> None:
        """Write the shared service entity if the version still matches.

        ``expected_version`` semantics:

          * ``None``  — the caller believes the entity does not exist yet;
            create it.  If it already exists this raises ``ConfigConflict``.
          * a token   — replace only if the stored version equals this token;
            otherwise raise ``ConfigConflict``.

        On conflict the caller re-reads and retries.
        """
