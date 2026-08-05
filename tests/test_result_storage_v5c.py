"""V-5c: pin ServiceRegistry's read-modify-write and conflict-retry behaviour.

Two backends are used:

  * ``FakeConflictingBackend`` — an in-memory ``EntityConfigBackendPort`` whose
    ``write_service_entity`` can be told to raise ``ConfigConflict`` a fixed
    number of times before succeeding, or forever (to test the retry-exhausted
    path). This isolates the retry loop from real I/O.
  * The real ``FilesystemEntityConfigBackend`` (V-5a) against ``tmp_path`` for
    ``test_concurrent_registers_all_land`` — the actual existence proof that
    the lock + optimistic-version retry together prevent lost updates.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from ump.adapters.result_storage.entity_config_backend import (
    ConfigConflict,
    EntityConfigBackendPort,
)
from ump.adapters.result_storage.entity_config_fs import FilesystemEntityConfigBackend
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.core.interfaces.result_storage import ResultStorageError

SERVICE_ID = "ump-results"


class FakeConflictingBackend(EntityConfigBackendPort):
    """In-memory backend that can be told to reject the next N writes."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._version = 0
        self.conflicts_remaining = 0
        self.write_calls = 0

    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        raise NotImplementedError  # not used by ServiceRegistry

    def delete_provider_entity(self, provider_id: str) -> None:
        raise NotImplementedError  # not used by ServiceRegistry

    def read_service_entity(self, service_id: str) -> tuple[str, str] | None:
        if self._text is None:
            return None
        return self._text, str(self._version)

    def write_service_entity(
        self, service_id: str, yaml_text: str, expected_version: str | None
    ) -> None:
        self.write_calls += 1
        if self.conflicts_remaining > 0:
            self.conflicts_remaining -= 1
            raise ConfigConflict("simulated contention")
        current = str(self._version) if self._text is not None else None
        if current != expected_version:
            raise ConfigConflict("version mismatch")
        self._text = yaml_text
        self._version += 1


@pytest.fixture
def fake_backend() -> FakeConflictingBackend:
    return FakeConflictingBackend()


@pytest.fixture
def registry(fake_backend: FakeConflictingBackend) -> ServiceRegistry:
    return ServiceRegistry(fake_backend, service_id=SERVICE_ID)


class TestBootstrapAndRegister:
    @pytest.mark.asyncio
    async def test_bootstrap_when_absent(self, registry, fake_backend):
        await registry.register_collection("job-1-out", "job-1", "out")
        service = yaml.safe_load(fake_backend._text)
        assert service["id"] == SERVICE_ID
        assert "job-1-out" in service["collections"]

    @pytest.mark.asyncio
    async def test_register_adds_collection_with_correct_fields(
        self, registry, fake_backend
    ):
        await registry.register_collection("job-1-out", "job-1", "out")
        service = yaml.safe_load(fake_backend._text)
        block = service["collections"]["job-1-out"]
        api = block["api"][0]
        assert api["featureProvider"] == "job-1"
        assert api["featureType"] == "out"

    @pytest.mark.asyncio
    async def test_register_is_idempotent(self, registry, fake_backend):
        await registry.register_collection("job-1-out", "job-1", "out")
        await registry.register_collection("job-1-out", "job-1", "out")
        service = yaml.safe_load(fake_backend._text)
        assert len(service["collections"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_registers_accumulate(self, registry, fake_backend):
        await registry.register_collection("job-1-out", "job-1", "out")
        await registry.register_collection("job-2-out", "job-2", "out")
        service = yaml.safe_load(fake_backend._text)
        assert set(service["collections"]) == {"job-1-out", "job-2-out"}


class TestDeregister:
    @pytest.mark.asyncio
    async def test_deregister_removes(self, registry, fake_backend):
        await registry.register_collection("job-1-out", "job-1", "out")
        await registry.register_collection("job-2-out", "job-2", "out")
        await registry.deregister_collection("job-1-out")
        service = yaml.safe_load(fake_backend._text)
        assert set(service["collections"]) == {"job-2-out"}

    @pytest.mark.asyncio
    async def test_deregister_missing_is_noop(self, registry, fake_backend):
        await registry.deregister_collection("does-not-exist")  # no raise
        service = yaml.safe_load(fake_backend._text)
        assert service["collections"] == {}


class TestConflictRetry:
    @pytest.mark.asyncio
    async def test_retries_on_conflict(self, registry, fake_backend):
        fake_backend.conflicts_remaining = 2
        await registry.register_collection("job-1-out", "job-1", "out")
        assert fake_backend.write_calls == 3  # 2 rejected + 1 accepted
        service = yaml.safe_load(fake_backend._text)
        assert "job-1-out" in service["collections"]

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self, fake_backend):
        registry = ServiceRegistry(fake_backend, service_id=SERVICE_ID, max_retries=3)
        fake_backend.conflicts_remaining = 999  # never succeeds
        with pytest.raises(ResultStorageError):
            await registry.register_collection("job-1-out", "job-1", "out")
        assert fake_backend.write_calls == 3


class TestConcurrencyAgainstRealBackend:
    @pytest.mark.asyncio
    async def test_concurrent_registers_all_land(self, tmp_path):
        # The real FilesystemEntityConfigBackend, exercised with genuine
        # concurrent asyncio tasks — this is the actual existence proof that
        # the lock + ConfigConflict retry loop prevent the lost-update hazard
        # instead of just asserting it in isolation.
        backend = FilesystemEntityConfigBackend(tmp_path)
        registry = ServiceRegistry(backend, service_id=SERVICE_ID)

        async def register(i: int) -> None:
            await registry.register_collection(f"job-{i}-out", f"job-{i}", "out")

        await asyncio.gather(*(register(i) for i in range(20)))

        service_path = (
            tmp_path / "entities" / "instances" / "services" / f"{SERVICE_ID}.yml"
        )
        service = yaml.safe_load(service_path.read_text())
        assert set(service["collections"]) == {f"job-{i}-out" for i in range(20)}
