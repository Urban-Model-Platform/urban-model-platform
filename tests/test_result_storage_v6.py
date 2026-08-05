"""V-6: pin LdproxyResultStorage — the adapter that wires V-3/4/5 together.

All tests run against a temp directory with a real FilesystemEntityConfigBackend
and a real ServiceRegistry — no ldproxy and no Kubernetes required. GeoJSON
fixtures are reused from test_result_storage_v3.py so payload data stays in one
place.

The failure-path tests use a small backend wrapper that raises on the provider
write, to prove the transactional rollback: a store that fails after the
GeoPackage is written must leave nothing behind.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
import yaml

from tests.test_result_storage_v3 import _geojson_polygon_collection
from ump.adapters.result_storage.entity_config_backend import EntityConfigBackendPort
from ump.adapters.result_storage.entity_config_fs import FilesystemEntityConfigBackend
from ump.adapters.result_storage.ldproxy_result_storage import LdproxyResultStorage
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.core.interfaces.result_storage import (
    ResultPayload,
    ResultStorageError,
    UnsupportedResultError,
)

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


def _gpkg_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "resources" / "features" / f"{job_id}.gpkg"


def _provider_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "entities" / "instances" / "providers" / f"{job_id}.yml"


def _service(tmp_path):
    path = tmp_path / "entities" / "instances" / "services" / "ump-results.yml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


class TestStoreHappyPath:
    @pytest.mark.asyncio
    async def test_writes_gpkg(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi", 3)])
        path = _gpkg_path(tmp_path)
        assert path.exists()
        gdf = gpd.read_file(str(path), layer="voronoi", engine="pyogrio")
        assert len(gdf) == 3

    @pytest.mark.asyncio
    async def test_writes_provider_entity(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        provider = yaml.safe_load(_provider_path(tmp_path).read_text())
        assert provider["id"] == JOB_ID
        assert provider["connectionInfo"]["database"] == f"{JOB_ID}.gpkg"
        assert "voronoi" in provider["types"]

    @pytest.mark.asyncio
    async def test_registers_collection(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        service = _service(tmp_path)
        block = service["collections"][f"{JOB_ID}-voronoi"]
        api = block["api"][0]
        assert api["featureProvider"] == JOB_ID
        assert api["featureType"] == "voronoi"

    @pytest.mark.asyncio
    async def test_returns_references_in_order(self, tmp_path):
        storage = _make_storage(tmp_path)
        refs = await storage.store(JOB_ID, [_payload("voronoi"), _payload("buffer")])
        assert [r.collection_id for r in refs] == [
            f"{JOB_ID}-voronoi",
            f"{JOB_ID}-buffer",
        ]
        first = refs[0]
        assert first.collection_url == f"{BASE_URL}/collections/{JOB_ID}-voronoi"
        assert first.items_url == f"{BASE_URL}/collections/{JOB_ID}-voronoi/items"

    @pytest.mark.asyncio
    async def test_multiple_outputs_all_consistent(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi", 3), _payload("buffer", 2)])

        # One gpkg with both layers.
        path = _gpkg_path(tmp_path)
        assert len(gpd.read_file(str(path), layer="voronoi", engine="pyogrio")) == 3
        assert len(gpd.read_file(str(path), layer="buffer", engine="pyogrio")) == 2
        # One provider with both types.
        provider = yaml.safe_load(_provider_path(tmp_path).read_text())
        assert set(provider["types"]) == {"voronoi", "buffer"}
        # Two collections.
        service = _service(tmp_path)
        assert set(service["collections"]) == {
            f"{JOB_ID}-voronoi",
            f"{JOB_ID}-buffer",
        }

    @pytest.mark.asyncio
    async def test_store_is_idempotent(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        await storage.store(JOB_ID, [_payload("voronoi")])
        service = _service(tmp_path)
        assert len(service["collections"]) == 1


class TestExists:
    @pytest.mark.asyncio
    async def test_false_before_true_after(self, tmp_path):
        storage = _make_storage(tmp_path)
        assert await storage.exists(JOB_ID) is False
        await storage.store(JOB_ID, [_payload("voronoi")])
        assert await storage.exists(JOB_ID) is True


class TestDelete:
    @pytest.mark.asyncio
    async def test_removes_gpkg_and_provider_idempotent(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.store(JOB_ID, [_payload("voronoi")])
        await storage.delete(JOB_ID)
        assert not _gpkg_path(tmp_path).exists()
        assert not _provider_path(tmp_path).exists()
        await storage.delete(JOB_ID)  # second call must not raise


class TestUnsupportedInput:
    @pytest.mark.asyncio
    async def test_unsupported_media_type_propagates_and_leaves_nothing(self, tmp_path):
        storage = _make_storage(tmp_path)
        bad = ResultPayload(
            output_id="voronoi",
            body_bytes=b"not geospatial",
            media_type="application/json",
        )
        with pytest.raises(UnsupportedResultError):
            await storage.store(JOB_ID, [bad])
        # write_layers_to_gpkg validates before writing, so nothing lands.
        assert not _gpkg_path(tmp_path).exists()
        assert not _provider_path(tmp_path).exists()


class _FailingProviderBackend(FilesystemEntityConfigBackend):
    """Filesystem backend whose provider write always fails — to test rollback."""

    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        raise ResultStorageError("simulated provider write failure")


class TestTransactionalRollback:
    @pytest.mark.asyncio
    async def test_provider_failure_rolls_back_gpkg(self, tmp_path):
        backend = _FailingProviderBackend(tmp_path)
        storage = _make_storage(tmp_path, backend=backend)
        with pytest.raises(ResultStorageError):
            await storage.store(JOB_ID, [_payload("voronoi")])
        # The gpkg was written in stage 1 but must be rolled back.
        assert not _gpkg_path(tmp_path).exists()
        # exists() therefore stays honest: the job is NOT stored.
        assert await storage.exists(JOB_ID) is False

    @pytest.mark.asyncio
    async def test_no_temp_file_left_after_rollback(self, tmp_path):
        backend = _FailingProviderBackend(tmp_path)
        storage = _make_storage(tmp_path, backend=backend)
        with pytest.raises(ResultStorageError):
            await storage.store(JOB_ID, [_payload("voronoi")])
        features_dir = tmp_path / "resources" / "features"
        leftovers = list(features_dir.glob(".*")) if features_dir.exists() else []
        assert leftovers == []
