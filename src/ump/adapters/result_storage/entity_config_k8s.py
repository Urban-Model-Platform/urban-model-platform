"""Kubernetes implementation of ``EntityConfigBackendPort`` (production).

In production ldproxy does not read entity YAML from a shared volume but from
mounted ConfigMaps.  This backend maps the same port onto the Kubernetes API::

    all provider entities ->  ONE ConfigMap named ``provider_configmap``
                              with one key ``{provider_id}.yml`` per job
    service entity        ->  one ConfigMap named ``service_configmap``
                              with a single key ``{service_id}.yml``

**Why one shared providers ConfigMap and not one per job (V-13).**  A pod's
volumes are resolved *by name at admission time*, so a ConfigMap **object**
created after ldproxy started can never appear inside the running pod — only
new **keys in an already-mounted** ConfigMap are propagated by the kubelet.
An earlier design wrote one ConfigMap per job and was therefore undeliverable.
Collapsing providers into one object makes every job the case that does
propagate.  It also explains why "all providers in one place" cannot mean
concatenating YAML: ldproxy needs one entity *per file*, and the key→file
mapping of a directory mount is exactly what preserves that.

Two invariants make the mapping clean:

  * **Entity existence == key existence.**  ``read_service_entity`` returns
    ``None`` exactly when the ConfigMap is absent, which is the signal V-5c
    uses to bootstrap the skeleton.  Deployment must therefore *not*
    pre-create an empty service ConfigMap.
  * **Version token == ConfigMap ``resourceVersion``.**  Kubernetes already
    implements optimistic concurrency: a ``replace`` carrying a stale
    ``resourceVersion`` is rejected with HTTP 409.  For the *service* entity we
    surface that as ``ConfigConflict`` so the service registry's
    read-modify-write loop retries.  For the *providers* ConfigMap the port
    exposes no version token (provider writes are meant to be independent), so
    the retry loop lives here, inside ``_ConfigMapMutator``.

The ``kubernetes`` client is imported lazily so it stays an optional dependency
(the ``k8s`` extra); dev/Docker runs the filesystem backend and never imports
it.  Unit tests inject a mock ``core_v1_api`` and likewise avoid the import.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable

from ump.adapters.result_storage.entity_config_backend import (
    ConfigConflict,
    EntityConfigBackendPort,
)
from ump.core.interfaces.result_storage import ResultStorageError

logger = logging.getLogger(__name__)

# etcd rejects any object larger than ~1 MiB.  We guard well below that so the
# error names the real cause instead of the API server rejecting an opaque
# "request entity too large" at an arbitrary moment.  A provider entity is
# ~2 KB, so this still allows several hundred concurrently published results —
# the operational bound this design deliberately accepts (see REF-F5 V-13).
_MAX_CONFIGMAP_BYTES = 900_000

# The shared providers ConfigMap is contended by every completing job.  A retry
# is cheap (one GET + one PUT) and conflicts are rare, but a burst of parallel
# completions across replicas can chain a few, so the budget is generous.
_MAX_CAS_RETRIES = 10
_CAS_BACKOFF_BASE_SECONDS = 0.05
_CAS_BACKOFF_MAX_SECONDS = 1.0


def _api_status(exc: Exception) -> int | None:
    """HTTP status of a kubernetes ``ApiException``, else ``None``.

    Matching on the ``status`` attribute (instead of the ``ApiException`` type)
    keeps this module import-free of ``kubernetes`` and lets tests raise a
    lightweight stand-in.  Non-API errors have no int ``status`` and must be
    re-raised untouched rather than disguised as a storage failure.
    """
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _wrap(exc: Exception, action: str) -> ResultStorageError:
    """Build a ``ResultStorageError`` for a genuine Kubernetes API failure.

    Callers only reach this once they've confirmed ``_api_status(exc)`` is a
    real HTTP status and not one of the statuses handled specially (404/409).
    """
    return ResultStorageError(f"Kubernetes {action} failed: {exc}")


def _guard_size(name: str, data: dict[str, str]) -> None:
    """Fail loudly *before* etcd's ~1 MiB object limit is hit.

    Without this the API server rejects the write with an opaque error at an
    unpredictable job, which reads like a random storage outage.  Naming the
    cap turns it into an actionable operational signal instead.
    """
    size = sum(len(k.encode()) + len(v.encode()) for k, v in data.items())
    if size > _MAX_CONFIGMAP_BYTES:
        raise ResultStorageError(
            f"ConfigMap {name} would be {size} bytes, exceeding the "
            f"{_MAX_CONFIGMAP_BYTES} byte budget below etcd's ~1 MiB object "
            f"limit ({len(data)} entities). Shorten the result retention "
            f"interval, or set UMP_RESULTSTORE_CONFIG_BACKEND=filesystem."
        )


class _ConfigMapMutator:
    """Read → mutate ``data`` → compare-and-swap, retrying on conflict.

    Encapsulates the one non-obvious piece of this backend: mutating a shared
    ConfigMap safely from several ump-api replicas at once.  Kubernetes already
    offers atomic compare-and-swap through ``resourceVersion``, so no external
    lock or serialising worker is needed — a rejected write simply re-reads and
    re-applies its mutation.  Because the mutation is re-run against the fresh
    document on every attempt, it must be a pure function of that document.

    Both ConfigMaps this backend owns go through one instance, so the retry
    policy and the size guard exist exactly once.
    """

    def __init__(self, api: Any, namespace: str) -> None:
        self._api = api
        self._namespace = namespace

    def mutate(
        self,
        name: str,
        mutation: Callable[[dict[str, str]], dict[str, str]],
        *,
        action: str,
    ) -> None:
        """Apply ``mutation`` to ``name``'s data map, creating it if absent.

        ``mutation`` receives the current data map (empty if the ConfigMap does
        not exist yet) and returns the desired one.  Returning it unchanged is
        honoured as a no-op, so callers need not pre-check for existence.
        """
        for attempt in range(_MAX_CAS_RETRIES):
            current, version = self._read(name)
            desired = mutation(dict(current))
            if desired == current:
                # Nothing to do. Note this also covers "delete a key from a
                # ConfigMap that does not exist yet" — cleanup must not bring
                # the object into being just to leave it empty.
                return
            try:
                self._write(name, desired, version)
                return
            except ConfigConflict:
                # Another replica (or thread) won the race.  Re-read and
                # re-apply; the mutation is idempotent by contract.
                logger.debug(
                    "ConfigMap %s changed during %s, retrying (attempt %d)",
                    name,
                    action,
                    attempt + 1,
                )
                time.sleep(self._backoff(attempt))
        raise ResultStorageError(
            f"Kubernetes {action} failed: ConfigMap {name} stayed contended "
            f"after {_MAX_CAS_RETRIES} attempts."
        )

    def read(self, name: str) -> tuple[dict[str, str], str] | None:
        """Return ``(data, resourceVersion)``, or ``None`` if the object is absent."""
        data, version = self._read(name)
        return None if version is None else (data, version)

    def write(
        self, name: str, data: dict[str, str], expected_version: str | None
    ) -> None:
        """Compare-and-swap once, letting ``ConfigConflict`` escape.

        Used for the service entity, whose version token is part of the port's
        contract — the retry loop belongs to ``ServiceRegistry``, not here.
        """
        self._write(name, data, expected_version)

    # -- Internals ----------------------------------------------------------

    def _read(self, name: str) -> tuple[dict[str, str], str | None]:
        try:
            cm = self._api.read_namespaced_config_map(name, self._namespace)
        except Exception as exc:  # noqa: BLE001 — dispatched on HTTP status
            status = _api_status(exc)
            if status == 404:
                return {}, None  # absent == not bootstrapped yet
            if status is None:
                raise  # not an API error — a real bug, don't disguise it
            raise _wrap(exc, f"read ConfigMap {name}") from exc
        return dict(cm.data or {}), cm.metadata.resource_version

    def _write(
        self, name: str, data: dict[str, str], expected_version: str | None
    ) -> None:
        _guard_size(name, data)
        body: dict[str, Any] = {"metadata": {"name": name}, "data": data}
        try:
            if expected_version is None:
                # We believe the object does not exist; a concurrent creator
                # turns this into a 409, i.e. a conflict like any other.
                self._api.create_namespaced_config_map(self._namespace, body)
            else:
                body["metadata"]["resourceVersion"] = expected_version
                self._api.replace_namespaced_config_map(name, self._namespace, body)
        except Exception as exc:  # noqa: BLE001
            status = _api_status(exc)
            if status in (404, 409):
                # 409: stale version, or lost the create race.
                # 404: the object was deleted between our read and this write.
                raise ConfigConflict(
                    f"ConfigMap {name} changed (expected version {expected_version!r})"
                ) from exc
            if status is None:
                raise
            raise _wrap(exc, f"write ConfigMap {name}") from exc

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter.

        Jitter matters here: without it, writers that collide once tend to
        collide again at the same instant on every subsequent retry.
        """
        delay = min(_CAS_BACKOFF_BASE_SECONDS * 2**attempt, _CAS_BACKOFF_MAX_SECONDS)
        return random.uniform(0, delay)


class K8sConfigMapEntityConfigBackend(EntityConfigBackendPort):
    """Persist ldproxy entity YAML as Kubernetes ConfigMaps."""

    def __init__(
        self,
        namespace: str,
        service_configmap: str,
        provider_configmap: str,
        core_v1_api: Any | None = None,
    ) -> None:
        self._service_cm = service_configmap
        self._provider_cm = provider_configmap
        # Injectable for tests; in production the real client is built lazily so
        # `kubernetes` remains an optional dependency.
        api = core_v1_api if core_v1_api is not None else self._build_default_api()
        self._configmaps = _ConfigMapMutator(api, namespace)
        # Layer 1 of the concurrency model: serialise writers *inside* this
        # process so the common case costs zero conflict retries.  A plain
        # threading.Lock (not asyncio.Lock) because this port is synchronous —
        # LdproxyResultStorage calls it through asyncio.to_thread, so contending
        # writers are genuine OS threads, which an asyncio primitive would not
        # protect.  Layer 2 (resourceVersion CAS) covers the other replicas.
        self._provider_lock = threading.Lock()

    # -- Provider entities: one shared ConfigMap, one key per job -----------

    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        key = _entity_key(provider_id)
        with self._provider_lock:
            self._configmaps.mutate(
                self._provider_cm,
                lambda data: {**data, key: yaml_text},
                action=f"write provider entity {provider_id}",
            )

    def delete_provider_entity(self, provider_id: str) -> None:
        key = _entity_key(provider_id)
        with self._provider_lock:
            # Removing an absent key leaves the map unchanged, which `mutate`
            # short-circuits — so cleanup stays idempotent and costs one GET.
            self._configmaps.mutate(
                self._provider_cm,
                lambda data: {k: v for k, v in data.items() if k != key},
                action=f"delete provider entity {provider_id}",
            )

    # -- Shared service entity: optimistic concurrency ----------------------

    def read_service_entity(self, service_id: str) -> tuple[str, str] | None:
        found = self._configmaps.read(self._service_cm)
        if found is None:
            return None
        data, version = found
        # Whoever created the ConfigMap always wrote this key, so `.get(..., "")`
        # only guards against a manually corrupted ConfigMap, not normal flow.
        return data.get(_entity_key(service_id), ""), version

    def write_service_entity(
        self, service_id: str, yaml_text: str, expected_version: str | None
    ) -> None:
        self._configmaps.write(
            self._service_cm,
            {_entity_key(service_id): yaml_text},
            expected_version,
        )

    # -- Internal helpers ---------------------------------------------------

    def _build_default_api(self) -> Any:
        try:
            from kubernetes import client, config
        except ImportError as exc:
            # The client is an optional extra, so a mis-built image fails here
            # rather than at packaging time. Name the remedy: a bare
            # ModuleNotFoundError gives no hint that this is a *deployment*
            # problem tied to one specific setting.
            raise RuntimeError(
                "UMP_RESULTSTORE_CONFIG_BACKEND=k8s requires the Kubernetes "
                "client, which is not installed in this image. Install UMP with "
                "the 'k8s' extra (`poetry install --extras k8s`), or switch the "
                "backend to 'filesystem'."
            ) from exc

        # In-cluster config uses the pod's mounted service account token.
        config.load_incluster_config()
        return client.CoreV1Api()


def _entity_key(entity_id: str) -> str:
    """ConfigMap data key for an entity id — also its filename once mounted."""
    return f"{entity_id}.yml"
