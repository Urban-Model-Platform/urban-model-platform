"""Tests for V-5a: EntityConfigBackendPort + FilesystemEntityConfigBackend.

All tests run against a temp directory — no ldproxy or Kubernetes required.
They pin the V-5a invariants only (paths, directory creation, optimistic
version handling, idempotent delete); the shared read-modify-write retry loop
belongs to the service registry (V-5c) and is tested there.
"""

from __future__ import annotations

import pytest

from ump.adapters.result_storage.entity_config_backend import ConfigConflict
from ump.adapters.result_storage.entity_config_fs import (
    FilesystemEntityConfigBackend,
)


@pytest.fixture
def backend(tmp_path):
    return FilesystemEntityConfigBackend(tmp_path / "entities" / "instances")


# ---------------------------------------------------------------------------
# Provider entity
# ---------------------------------------------------------------------------


class TestProviderEntity:
    def test_writes_to_expected_path(self, backend, tmp_path):
        backend.write_provider_entity("job-uuid", "id: job-uuid\n")
        path = tmp_path / "entities" / "instances" / "providers" / "job-uuid.yml"
        assert path.read_text() == "id: job-uuid\n"

    def test_creates_missing_directories(self, backend, tmp_path):
        # No entities/ tree exists yet — the backend must create it.
        assert not (tmp_path / "entities").exists()
        backend.write_provider_entity("j", "x: 1")
        assert (tmp_path / "entities" / "instances" / "providers" / "j.yml").exists()

    def test_overwrite_is_deterministic(self, backend, tmp_path):
        backend.write_provider_entity("j", "first")
        backend.write_provider_entity("j", "second")
        path = tmp_path / "entities" / "instances" / "providers" / "j.yml"
        assert path.read_text() == "second"

    def test_delete_removes_file(self, backend, tmp_path):
        backend.write_provider_entity("j", "x: 1")
        path = tmp_path / "entities" / "instances" / "providers" / "j.yml"
        assert path.exists()
        backend.delete_provider_entity("j")
        assert not path.exists()

    def test_delete_is_idempotent(self, backend):
        # Deleting a never-written entity must not raise.
        backend.delete_provider_entity("never-existed")

    def test_no_temp_file_left_after_write(self, backend, tmp_path):
        backend.write_provider_entity("j", "x: 1")
        providers = tmp_path / "entities" / "instances" / "providers"
        assert list(providers.glob(".*.tmp*")) == []


# ---------------------------------------------------------------------------
# Shared service entity — optimistic concurrency
# ---------------------------------------------------------------------------


class TestServiceEntity:
    def test_read_missing_returns_none(self, backend):
        assert backend.read_service_entity("ump-results") is None

    def test_create_when_absent(self, backend, tmp_path):
        backend.write_service_entity("ump-results", "collections: {}\n", None)
        path = tmp_path / "entities" / "instances" / "services" / "ump-results.yml"
        assert path.read_text() == "collections: {}\n"

    def test_read_returns_text_and_version(self, backend):
        backend.write_service_entity("ump-results", "a: 1", None)
        result = backend.read_service_entity("ump-results")
        assert result is not None
        text, version = result
        assert text == "a: 1"
        assert isinstance(version, str) and version

    def test_create_when_already_exists_conflicts(self, backend):
        backend.write_service_entity("ump-results", "a: 1", None)
        # A second create (expected_version=None) must be rejected.
        with pytest.raises(ConfigConflict):
            backend.write_service_entity("ump-results", "a: 2", None)

    def test_update_with_matching_version_succeeds(self, backend):
        backend.write_service_entity("ump-results", "a: 1", None)
        _, version = backend.read_service_entity("ump-results")
        backend.write_service_entity("ump-results", "a: 2", version)
        text, _ = backend.read_service_entity("ump-results")
        assert text == "a: 2"

    def test_update_with_stale_version_conflicts(self, backend):
        backend.write_service_entity("ump-results", "a: 1", None)
        _, stale = backend.read_service_entity("ump-results")
        # Someone else writes, moving the version forward.
        backend.write_service_entity("ump-results", "a: 2", stale)
        # Our write with the now-stale token must be rejected.
        with pytest.raises(ConfigConflict):
            backend.write_service_entity("ump-results", "a: 3", stale)

    def test_no_temp_file_left_after_write(self, backend, tmp_path):
        backend.write_service_entity("ump-results", "a: 1", None)
        services = tmp_path / "entities" / "instances" / "services"
        assert list(services.glob(".*.tmp*")) == []
