"""V-9: manifest-based collection recovery in LdproxyResultStorage.delete().

Extends the V-6 delete-path coverage (test_result_storage_v6.py) with the V-9
manifest mechanism: store() writes a sidecar JSON manifest listing a job's
output ids, and delete() reads it back to know which collections to
deregister — without ever reading/re-parsing the provider entity YAML (see
the module docstring on ldproxy_result_storage.py for the full rationale).
"""

from __future__ import annotations

import json

import pytest
import yaml

from tests.test_result_storage_v3 import _geojson_polygon_collection
from ump.adapters.result_storage.entity_config_backend import EntityConfigBackendPort
from ump.adapters.result_storage.entity_config_fs import FilesystemEntityConfigBackend
from ump.adapters.result_storage.ldproxy_result_storage import LdproxyResultStorage
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.core.interfaces.result_storage import ResultPayload, ResultStorageError

JOB_ID = "job-abc"
BASE_URL = "https://geo.example.com/ump-results"


def _payload(output_id: str, n: int = 2) -> ResultPayload:
    return ResultPayload(
        output_id=output_id,
        body_bytes=_geojson_polygon_collection(n),
        media_type="application/geo+json",
    )


def _make_storage(tmp_path, backend=None) -> LdproxyResultStorage:
    backend = backend or FilesystemEntityConfigBackend(tmp_path)
    registry = ServiceRegistry(backend, service_id="ump-results")
    return LdproxyResultStorage(
        backend=backend,
        service_registry=registry,
        root_path=tmp_path,
        base_url=BASE_URL,
    )


def _manifest_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "resources" / "features" / f"{job_id}.manifest.json"


def _gpkg_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "resources" / "features" / f"{job_id}.gpkg"


def _service(tmp_path):
    path = tmp_path / "entities" / "instances" / "services" / "ump-results.yml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


class TestManifestWrite:
    @pytest.mark.asyncio
    async def test_store_writes_manifest_with_all_output_ids(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi"), _payload("buffer")])
        manifest = json.loads(_manifest_path(tmp_path).read_text())
        assert set(manifest["output_ids"]) == {"voronoi", "buffer"}


class TestDeleteUsesManifest:
    @pytest.mark.asyncio
    async def test_delete_deregisters_all_collections_from_manifest(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi"), _payload("buffer")])
        assert _service(tmp_path)["collections"]

        await storage.delete(JOB_ID)

        service = _service(tmp_path)
        assert service is None or not service.get("collections")

    @pytest.mark.asyncio
    async def test_delete_removes_manifest_file(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        await storage.delete(JOB_ID)
        assert not _manifest_path(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_delete_without_prior_store_is_safe(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.delete("never-stored")  # no manifest, no gpkg — must not raise

    @pytest.mark.asyncio
    async def test_delete_with_corrupt_manifest_still_cleans_up_gpkg(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        _manifest_path(tmp_path).write_text("not valid json {")

        await storage.delete(JOB_ID)  # must not raise despite corrupt manifest

        assert not _gpkg_path(tmp_path).exists()
        assert not _manifest_path(tmp_path).exists()


class _FailingDeregisterRegistry(ServiceRegistry):
    """Registry whose deregister always fails — to test best-effort continuation."""

    async def deregister_collection(self, collection_id: str) -> None:
        raise ResultStorageError("simulated deregister failure")


class TestDeleteBestEffort:
    @pytest.mark.asyncio
    async def test_deregister_failure_does_not_block_gpkg_and_provider_cleanup(
        self, tmp_path
    ):
        backend = FilesystemEntityConfigBackend(tmp_path)
        failing_registry = _FailingDeregisterRegistry(backend, service_id="ump-results")
        storage = LdproxyResultStorage(
            backend=backend,
            service_registry=failing_registry,
            root_path=tmp_path,
            base_url=BASE_URL,
        )
        await storage.store(JOB_ID, [_payload("voronoi")])

        await storage.delete(JOB_ID)  # deregister fails internally, must not raise

        assert not _gpkg_path(tmp_path).exists()
        assert not _manifest_path(tmp_path).exists()


class _FailingProviderDeleteBackend(FilesystemEntityConfigBackend):
    def delete_provider_entity(self, provider_id: str) -> None:
        raise ResultStorageError("simulated provider delete failure")


class TestRollbackCleansManifest:
    @pytest.mark.asyncio
    async def test_failed_store_rolls_back_manifest_too(self, tmp_path):
        class _FailingProviderWriteBackend(FilesystemEntityConfigBackend):
            def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
                raise ResultStorageError("simulated provider write failure")

        backend = _FailingProviderWriteBackend(tmp_path)
        storage = _make_storage(tmp_path, backend=backend)
        with pytest.raises(ResultStorageError):
            await storage.store(JOB_ID, [_payload("voronoi")])
        assert not _manifest_path(tmp_path).exists()
