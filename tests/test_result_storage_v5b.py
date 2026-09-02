"""V-5b + V-13: pin the Kubernetes ConfigMap backend against a faithful mock API.

The mock ``FakeCoreV1Api`` reproduces exactly the Kubernetes behaviour the
backend relies on: an in-memory ConfigMap store, a monotonically increasing
``resourceVersion`` per object, 404 on absent objects, 409 on create-of-existing
and on replace-with-stale-resourceVersion.  ``FakeApiException`` stands in for
``kubernetes.client.rest.ApiException`` (it only needs a ``status``), so these
tests run without the optional ``kubernetes`` dependency installed.

Since V-13 all provider entities share **one** ConfigMap, one data key per job.
That makes provider writes a contended read-modify-write, so the interesting
tests here are no longer "is the object shaped right" but "can two writers lose
each other's key".  ``TestProviderConcurrency`` proves they cannot, driving real
threads through the same lock + compare-and-swap path production uses (the port
is synchronous and is called via ``asyncio.to_thread``, so threads — not
coroutines — are the realistic contention model).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from ump.adapters.result_storage.entity_config_backend import ConfigConflict
from ump.adapters.result_storage.entity_config_k8s import (
    _MAX_CONFIGMAP_BYTES,
    K8sConfigMapEntityConfigBackend,
)
from ump.core.interfaces.result_storage import ResultStorageError

NAMESPACE = "ump"
SERVICE_CM = "ump-ldproxy-service"
PROVIDER_CM = "ump-ldproxy-providers"


class FakeApiException(Exception):
    """Minimal stand-in for kubernetes' ApiException (only ``status`` matters)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeCoreV1Api:
    """In-memory CoreV1Api reproducing ConfigMap concurrency semantics.

    Every operation is guarded by a lock so the fake itself is linearizable,
    like a real API server.  Any lost update a test observes therefore comes
    from the backend, not from the fake.
    """

    def __init__(self) -> None:
        # name -> {"data": dict, "rv": int}
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.conflicts = 0

    def read_namespaced_config_map(self, name, namespace):
        with self._lock:
            entry = self._store.get(name)
            if entry is None:
                raise FakeApiException(404)
            return SimpleNamespace(
                data=dict(entry["data"]),
                metadata=SimpleNamespace(resource_version=str(entry["rv"])),
            )

    def create_namespaced_config_map(self, namespace, body):
        with self._lock:
            name = body["metadata"]["name"]
            if name in self._store:
                self.conflicts += 1
                raise FakeApiException(409)
            self._store[name] = {"data": dict(body["data"]), "rv": 1}

    def replace_namespaced_config_map(self, name, namespace, body):
        with self._lock:
            entry = self._store.get(name)
            if entry is None:
                raise FakeApiException(404)
            expected = body.get("metadata", {}).get("resourceVersion")
            if expected is not None and expected != str(entry["rv"]):
                self.conflicts += 1
                raise FakeApiException(409)
            entry["data"] = dict(body["data"])
            entry["rv"] += 1

    def delete_namespaced_config_map(self, name, namespace):
        with self._lock:
            if name not in self._store:
                raise FakeApiException(404)
            del self._store[name]


@pytest.fixture
def api() -> FakeCoreV1Api:
    return FakeCoreV1Api()


def make_backend(api: FakeCoreV1Api) -> K8sConfigMapEntityConfigBackend:
    return K8sConfigMapEntityConfigBackend(
        namespace=NAMESPACE,
        service_configmap=SERVICE_CM,
        provider_configmap=PROVIDER_CM,
        core_v1_api=api,
    )


@pytest.fixture
def backend(api: FakeCoreV1Api) -> K8sConfigMapEntityConfigBackend:
    return make_backend(api)


def provider_data(api: FakeCoreV1Api) -> dict[str, str]:
    return api._store[PROVIDER_CM]["data"]


def _run_in_parallel(calls) -> None:
    """Run every call on its own thread, failing the test on any exception.

    A barrier makes the threads start together, which is what actually produces
    the interleaving these tests are about; without it they tend to run
    sequentially and prove nothing.
    """
    barrier = threading.Barrier(len(calls))
    errors: list[BaseException] = []

    def run(call):
        barrier.wait()
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,)) for c in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


class TestProviderEntity:
    def test_first_write_creates_the_shared_configmap(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        assert provider_data(api) == {"job-1.yml": "yaml: one"}

    def test_second_job_is_added_as_another_key(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.write_provider_entity("job-2", "yaml: two")
        # The decisive V-13 property: one object, one key per job — this is what
        # lets the kubelet deliver a new job to an already-running ldproxy.
        assert provider_data(api) == {
            "job-1.yml": "yaml: one",
            "job-2.yml": "yaml: two",
        }

    def test_rewrite_replaces_only_its_own_key(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.write_provider_entity("job-2", "yaml: two")
        backend.write_provider_entity("job-1", "yaml: one-again")
        assert provider_data(api) == {
            "job-1.yml": "yaml: one-again",
            "job-2.yml": "yaml: two",
        }

    def test_identical_rewrite_does_not_touch_the_object(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        rv_before = api._store[PROVIDER_CM]["rv"]
        backend.write_provider_entity("job-1", "yaml: one")
        # A no-op mutation must not bump resourceVersion: every write of this
        # shared object costs a kubelet resync for the ldproxy pod.
        assert api._store[PROVIDER_CM]["rv"] == rv_before

    def test_delete_removes_only_its_own_key(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.write_provider_entity("job-2", "yaml: two")
        backend.delete_provider_entity("job-1")
        assert provider_data(api) == {"job-2.yml": "yaml: two"}

    def test_delete_missing_key_is_noop(self, backend, api):
        backend.write_provider_entity("job-1", "yaml: one")
        backend.delete_provider_entity("does-not-exist")  # no raise
        assert provider_data(api) == {"job-1.yml": "yaml: one"}

    def test_delete_before_any_write_is_noop(self, backend, api):
        # V-9 cleanup runs unconditionally, including for jobs never stored.
        backend.delete_provider_entity("job-1")
        assert PROVIDER_CM not in api._store

    def test_api_error_is_wrapped(self, backend, api):
        def boom(name, namespace):
            raise FakeApiException(403)

        api.read_namespaced_config_map = boom
        with pytest.raises(ResultStorageError):
            backend.write_provider_entity("job-1", "yaml: one")

    def test_non_api_error_is_not_disguised(self, backend, api):
        def boom(name, namespace):
            raise RuntimeError("a genuine bug")

        api.read_namespaced_config_map = boom
        with pytest.raises(RuntimeError):
            backend.write_provider_entity("job-1", "yaml: one")


class TestProviderConcurrency:
    """No registration may be lost when many jobs finish at once."""

    def test_parallel_writes_through_one_backend_keep_every_key(self, backend, api):
        jobs = [f"job-{i}" for i in range(30)]
        _run_in_parallel(
            [lambda j=j: backend.write_provider_entity(j, f"yaml: {j}") for j in jobs]
        )
        assert provider_data(api) == {f"{j}.yml": f"yaml: {j}" for j in jobs}

    def test_parallel_writes_across_replicas_keep_every_key(self, api):
        # Separate backend instances == separate pods: the in-process lock does
        # not span them, so this exercises the resourceVersion CAS retry loop —
        # the only thing standing between two replicas and a lost update.
        replicas = [make_backend(api) for _ in range(4)]
        jobs = [f"job-{i}" for i in range(40)]
        _run_in_parallel(
            [
                lambda j=j, b=replicas[i % len(replicas)]: b.write_provider_entity(
                    j, f"yaml: {j}"
                )
                for i, j in enumerate(jobs)
            ]
        )
        assert provider_data(api) == {f"{j}.yml": f"yaml: {j}" for j in jobs}

    def test_write_survives_a_conflict_and_preserves_the_other_replica_key(self, api):
        """The CAS retry, forced deterministically rather than by timing.

        Real thread interleaving is too coarse to reliably produce a 409 against
        an in-memory fake, so the conflict is injected: the first replace is
        rejected *after* another replica has slipped its own key in. A backend
        that simply re-sent its stale document would drop that key — which is
        precisely the lost update this design exists to prevent.
        """
        mine, other = make_backend(api), make_backend(api)
        mine.write_provider_entity("job-existing", "yaml: existing")

        real_replace = api.replace_namespaced_config_map
        rejected = False

        def replace_once_rejected(name, namespace, body):
            nonlocal rejected
            if not rejected:
                rejected = True
                other.write_provider_entity("job-other", "yaml: other")
                raise FakeApiException(409)
            return real_replace(name, namespace, body)

        api.replace_namespaced_config_map = replace_once_rejected
        mine.write_provider_entity("job-mine", "yaml: mine")

        assert provider_data(api) == {
            "job-existing.yml": "yaml: existing",
            "job-other.yml": "yaml: other",
            "job-mine.yml": "yaml: mine",
        }

    def test_write_gives_up_with_a_clear_error_under_permanent_contention(
        self, backend, api
    ):
        def always_conflict(name, namespace, body):
            raise FakeApiException(409)

        backend.write_provider_entity("job-1", "yaml: one")
        api.replace_namespaced_config_map = always_conflict
        with pytest.raises(ResultStorageError, match="contended"):
            backend.write_provider_entity("job-2", "yaml: two")

    def test_parallel_delete_and_write_do_not_interfere(self, api):
        writer, deleter = make_backend(api), make_backend(api)
        preexisting = [f"old-{i}" for i in range(20)]
        for job in preexisting:
            writer.write_provider_entity(job, f"yaml: {job}")

        fresh = [f"new-{i}" for i in range(20)]
        _run_in_parallel(
            [lambda j=j: writer.write_provider_entity(j, f"yaml: {j}") for j in fresh]
            + [lambda j=j: deleter.delete_provider_entity(j) for j in preexisting]
        )
        assert provider_data(api) == {f"{j}.yml": f"yaml: {j}" for j in fresh}


class TestSizeGuard:
    def test_oversized_write_names_the_cap(self, backend):
        with pytest.raises(ResultStorageError, match="etcd"):
            backend.write_provider_entity("job-1", "x" * (_MAX_CONFIGMAP_BYTES + 1))

    def test_guard_rejects_the_accumulated_total(self, backend, api):
        half = "x" * (_MAX_CONFIGMAP_BYTES // 2 + 1)
        backend.write_provider_entity("job-1", half)
        # Each entity fits on its own; together they do not. The cap is on the
        # object, which is the limit etcd actually enforces.
        with pytest.raises(ResultStorageError, match="etcd"):
            backend.write_provider_entity("job-2", half)
        assert set(provider_data(api)) == {"job-1.yml"}


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

    def test_service_and_provider_entities_are_separate_objects(self, backend, api):
        backend.write_service_entity("ump-results", "yaml: svc", expected_version=None)
        backend.write_provider_entity("job-1", "yaml: one")
        assert api._store[SERVICE_CM]["data"] == {"ump-results.yml": "yaml: svc"}
        assert provider_data(api) == {"job-1.yml": "yaml: one"}

    def test_read_api_error_is_wrapped(self, backend, api):
        def boom(name, namespace):
            raise FakeApiException(403)

        api.read_namespaced_config_map = boom
        with pytest.raises(ResultStorageError):
            backend.read_service_entity("ump-results")
