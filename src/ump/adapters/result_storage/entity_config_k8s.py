"""Kubernetes implementation of ``EntityConfigBackendPort`` (production).

In production ldproxy does not read entity YAML from a shared volume but from
mounted ConfigMaps.  This backend maps the same port onto the Kubernetes API:

    provider entity  ->  one ConfigMap named ``{prefix}{provider_id}``
                         with a single key ``{provider_id}.yml``
    service entity   ->  one ConfigMap named ``service_configmap``
                         with a single key ``{service_id}.yml``

Two invariants make the mapping clean:

  * **Entity existence == ConfigMap existence.**  ``read_service_entity``
    returns ``None`` exactly when the ConfigMap is absent, which is the signal
    V-5c uses to bootstrap the skeleton.  Deployment must therefore *not*
    pre-create an empty service ConfigMap.
  * **Version token == ConfigMap ``resourceVersion``.**  Kubernetes already
    implements optimistic concurrency: a ``replace`` carrying a stale
    ``resourceVersion`` is rejected with HTTP 409.  We surface that as
    ``ConfigConflict`` so the service registry's read-modify-write loop retries
    — identical behaviour to the filesystem backend's content-hash guard.

The ``kubernetes`` client is imported lazily so it stays an optional dependency
(the ``k8s`` extra); dev/Docker runs the filesystem backend and never imports
it.  Unit tests inject a mock ``core_v1_api`` and likewise avoid the import.
"""

from __future__ import annotations

from typing import Any

from ump.adapters.result_storage.entity_config_backend import (
    ConfigConflict,
    EntityConfigBackendPort,
)
from ump.core.interfaces.result_storage import ResultStorageError


class K8sConfigMapEntityConfigBackend(EntityConfigBackendPort):
    """Persist entity YAML as Kubernetes ConfigMaps."""

    def __init__(
        self,
        namespace: str,
        service_configmap: str,
        provider_cm_prefix: str,
        core_v1_api: Any | None = None,
    ) -> None:
        self._namespace = namespace
        self._service_cm = service_configmap
        self._provider_prefix = provider_cm_prefix
        # Injectable for tests; in production we build the real client lazily so
        # `kubernetes` remains an optional dependency.
        self._api = (
            core_v1_api if core_v1_api is not None else self._build_default_api()
        )

    # -- Provider entity: single-writer, overwrite semantics ----------------

    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        # Re-storing the same job (Concern 3: idempotency) is the common case,
        # so we try `replace` first and only fall back to `create` when the
        # ConfigMap does not exist yet — one API call on the hot path.
        name = self._provider_cm_name(provider_id)
        body = self._configmap_body(name, {f"{provider_id}.yml": yaml_text})
        try:
            self._api.replace_namespaced_config_map(name, self._namespace, body)
        except Exception as exc:  # noqa: BLE001 — dispatched on HTTP status below
            status = self._status(exc)
            if status == 404:
                self._create(body)
            elif status is None:
                raise  # not an API error — a real bug, don't disguise it
            else:
                raise self._wrap(exc, f"write provider entity {name}") from exc

    def delete_provider_entity(self, provider_id: str) -> None:
        name = self._provider_cm_name(provider_id)
        try:
            self._api.delete_namespaced_config_map(name, self._namespace)
        except Exception as exc:  # noqa: BLE001
            status = self._status(exc)
            if status == 404:
                pass  # already gone — idempotent, cleanup (V-9) runs unconditionally
            elif status is None:
                raise
            else:
                raise self._wrap(exc, f"delete provider entity {name}") from exc

    # -- Shared service entity: optimistic concurrency ----------------------

    def read_service_entity(self, service_id: str) -> tuple[str, str] | None:
        try:
            cm = self._api.read_namespaced_config_map(self._service_cm, self._namespace)
        except Exception as exc:  # noqa: BLE001
            status = self._status(exc)
            if status == 404:
                return None  # absent ConfigMap == entity not bootstrapped yet
            if status is None:
                raise
            raise self._wrap(exc, f"read service entity {self._service_cm}") from exc
        data = cm.data or {}
        # Whoever created the ConfigMap always wrote this key, so `.get(..., "")`
        # only guards against a manually corrupted ConfigMap, not normal flow.
        text = data.get(f"{service_id}.yml", "")
        return text, cm.metadata.resource_version

    def write_service_entity(
        self, service_id: str, yaml_text: str, expected_version: str | None
    ) -> None:
        body = self._configmap_body(self._service_cm, {f"{service_id}.yml": yaml_text})
        try:
            if expected_version is None:
                # Caller believes the entity does not exist yet -> create.
                self._create(body)
            else:
                # Replace only if the stored resourceVersion still matches;
                # Kubernetes enforces this and returns 409 on mismatch.
                body["metadata"]["resourceVersion"] = expected_version
                self._api.replace_namespaced_config_map(
                    self._service_cm, self._namespace, body
                )
        except Exception as exc:  # noqa: BLE001
            status = self._status(exc)
            if status == 409:
                raise ConfigConflict(
                    f"Service entity {self._service_cm} changed "
                    f"(expected version {expected_version!r})"
                ) from exc
            if status is None:
                raise
            raise self._wrap(exc, f"write service entity {self._service_cm}") from exc

    # -- Internal helpers ---------------------------------------------------

    def _create(self, body: dict[str, Any]) -> None:
        self._api.create_namespaced_config_map(self._namespace, body)

    def _provider_cm_name(self, provider_id: str) -> str:
        return f"{self._provider_prefix}{provider_id}"

    @staticmethod
    def _configmap_body(name: str, data: dict[str, str]) -> dict[str, Any]:
        """Build a ConfigMap as a plain dict.

        The kubernetes client serialises dict bodies just like model objects, so
        building a dict keeps this module free of a hard ``kubernetes`` import
        and lets tests assert on the body without the client installed.
        """
        return {"metadata": {"name": name}, "data": data}

    @staticmethod
    def _status(exc: Exception) -> int | None:
        """HTTP status of a kubernetes ``ApiException``, else ``None``.

        Matching on the ``status`` attribute (instead of the ``ApiException``
        type) keeps this module import-free of ``kubernetes`` and lets tests
        raise a lightweight stand-in.  Non-API errors have no int ``status`` and
        are re-raised untouched.
        """
        status = getattr(exc, "status", None)
        return status if isinstance(status, int) else None

    def _wrap(self, exc: Exception, action: str) -> ResultStorageError:
        """Build a ``ResultStorageError`` for a real Kubernetes API failure.

        Callers only reach this once they've already confirmed ``_status(exc)``
        is a genuine HTTP status (not ``None``) and not one of the statuses
        handled specially (404/409) — so this never needs to re-raise itself.
        """
        return ResultStorageError(f"Kubernetes {action} failed: {exc}")

    def _build_default_api(self) -> Any:
        from kubernetes import client, config

        # In-cluster config uses the pod's mounted service account token.
        config.load_incluster_config()
        return client.CoreV1Api()
