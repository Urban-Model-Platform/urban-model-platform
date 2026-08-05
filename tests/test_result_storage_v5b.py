"""V-5b: pin the Kubernetes ConfigMap backend against a faithful mock API.

The mock ``FakeCoreV1Api`` reproduces exactly the Kubernetes behaviour the
backend relies on: an in-memory ConfigMap store, a monotonically increasing
``resourceVersion`` per object, 404 on absent objects, 409 on create-of-existing
and on replace-with-stale-resourceVersion.  ``FakeApiException`` stands in for
``kubernetes.client.rest.ApiException`` (it only needs a ``status``), so these
tests run without the optional ``kubernetes`` dependency installed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ump.adapters.result_storage.entity_config_backend import ConfigConflict
from ump.adapters.result_storage.entity_config_k8s import (
    K8sConfigMapEntityConfigBackend,
)
from ump.core.interfaces.result_storage import ResultStorageError

NAMESPACE = "ump"
SERVICE_CM = "ump-ldproxy-service"
PROVIDER_PREFIX = "ump-ldproxy-provider-"


class FakeApiException(Exception):
    """Minimal stand-in for kubernetes' ApiException (only ``status`` matters)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeCoreV1Api:
    """In-memory CoreV1Api reproducing ConfigMap concurrency semantics."""

    def __init__(self) -> None:
        # name -> {"data": dict, "rv": int}
        self._store: dict[str, dict] = {}

    def read_namespaced_config_map(self, name, namespace):
        entry = self._store.get(name)
        if entry is None:
            raise FakeApiException(404)
        return SimpleNamespace(
            data=dict(entry["data"]),
            metadata=SimpleNamespace(resource_version=str(entry["rv"])),
        )

    def create_namespaced_config_map(self, namespace, body):
        name = body["metadata"]["name"]
        if name in self._store:
            raise FakeApiException(409)
        self._store[name] = {"data": dict(body["data"]), "rv": 1}

    def replace_namespaced_config_map(self, name, namespace, body):
        entry = self._store.get(name)
        if entry is None:
            raise FakeApiException(404)
        expected = body.get("metadata", {}).get("resourceVersion")
        if expected is not None and expected != str(entry["rv"]):
            raise FakeApiException(409)
        entry["data"] = dict(body["data"])
        entry["rv"] += 1

    def delete_namespaced_config_map(self, name, namespace):
        if name not in self._store:
            raise FakeApiException(404)
        del self._store[name]


@pytest.fixture
def api() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def backend(api: FakeCoreV1Api) -> K8sConfigMapEntityConfigBackend:
    return K8sConfigMapEntityConfigBackend(
        namespace=NAMESPACE,
        service_configmap=SERVICE_CM,
        provider_cm_prefix=PROVIDER_PREFIX,
        core_v1_api=api,
    )


class TestProviderEntity:
    def test_write_creates_configmap_with_id_key(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        entry = api._store[f"{PROVIDER_PREFIX}job-1"]
        assert entry["data"] == {"job-1.yml": "yaml: one"}

    def test_write_is_idempotent_overwrite(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.write_provider_entity("job-1", "yaml: two")
        entry = api._store[f"{PROVIDER_PREFIX}job-1"]
        assert entry["data"] == {"job-1.yml": "yaml: two"}

    def test_delete_removes_configmap(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.delete_provider_entity("job-1")
        assert f"{PROVIDER_PREFIX}job-1" not in api._store

    def test_delete_missing_is_noop(self, backend):
        backend.delete_provider_entity("does-not-exist")  # no raise

    def test_api_error_is_wrapped(self, backend, api):
        def boom(name, namespace, body):
            raise FakeApiException(500)

        api.replace_namespaced_config_map = boom
        api.create_namespaced_config_map = boom  # both paths fail
        with pytest.raises(ResultStorageError):
            backend.write_provider_entity("job-1", "yaml: one")


class TestServiceEntity:
    def test_read_missing_returns_none(self, backend):
        assert backend.read_service_entity("ump-results") is None

    def test_create_when_absent(self, backend):
        backend.write_service_entity("ump-results", "yaml: base", expected_version=None)
        result = backend.read_service_entity("ump-results")
        assert result is not None
        text, version = result
        assert text == "yaml: base"
        assert version == "1"

    def test_create_when_already_exists_conflicts(self, backend):
        backend.write_service_entity("ump-results", "yaml: base", expected_version=None)
        with pytest.raises(ConfigConflict):
            backend.write_service_entity(
                "ump-results", "yaml: other", expected_version=None
            )

    def test_update_with_matching_version_succeeds(self, backend):
        backend.write_service_entity("ump-results", "yaml: base", expected_version=None)
        _, version = backend.read_service_entity("ump-results")
        backend.write_service_entity(
            "ump-results", "yaml: updated", expected_version=version
        )
        text, new_version = backend.read_service_entity("ump-results")
        assert text == "yaml: updated"
        assert new_version != version

    def test_update_with_stale_version_conflicts(self, backend):
        backend.write_service_entity("ump-results", "yaml: base", expected_version=None)
        _, stale = backend.read_service_entity("ump-results")
        # A concurrent writer advances the version behind our back.
        backend.write_service_entity(
            "ump-results", "yaml: concurrent", expected_version=stale
        )
        with pytest.raises(ConfigConflict):
            backend.write_service_entity(
                "ump-results", "yaml: mine", expected_version=stale
            )

    def test_read_api_error_is_wrapped(self, backend, api):
        def boom(name, namespace):
            raise FakeApiException(403)

        api.read_namespaced_config_map = boom
        with pytest.raises(ResultStorageError):
            backend.read_service_entity("ump-results")
