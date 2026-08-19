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


def _make_storage(tmp_path, backend=None, **kwargs) -> LdproxyResultStorage:
    backend = backend or FilesystemEntityConfigBackend(tmp_path)
    registry = ServiceRegistry(backend, service_id="ump-results")
    # Default the post-store publication-confirmation to a no-op in tests: no
    # ldproxy is running, so there is nothing to probe or wait for. Individual
    # tests that exercise the confirm/pending behaviour override these.
    params = {"confirm_base_wait": 0.0, "confirm_max_wait": 0.0}
    params.update(kwargs)
    return LdproxyResultStorage(
        backend=backend,
        service_registry=registry,
        root_path=tmp_path,
        base_url=BASE_URL,
        **params,
    )


def _gpkg_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "resources" / "features" / f"{job_id}.gpkg"


def _provider_path(tmp_path, job_id=JOB_ID):
    return tmp_path / "entities" / "instances" / "providers" / f"{job_id}.yml"


def _service(tmp_path) -> dict:
    path = tmp_path / "entities" / "instances" / "services" / "ump-results.yml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


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


class TestEnsureDefaultProvider:
    """The shared service's *default* feature provider (V-6 bootstrap).

    ldproxy 3.x refuses to start an OGC_API service unless it can resolve a
    default feature provider whose id equals the service id, backed by a real,
    connectable GeoPackage (``initFailFast``). ``ensure_default_provider`` must
    create both, idempotently, on every startup — even though no per-job
    collection ever uses this provider (each overrides ``featureProvider``).
    """

    def _seed_path(self, tmp_path):
        return tmp_path / "resources" / "features" / "__ump_default__.gpkg"

    def _default_provider_path(self, tmp_path):
        return tmp_path / "entities" / "instances" / "providers" / "ump-results.yml"

    @pytest.mark.asyncio
    async def test_writes_seed_gpkg_and_provider_entity(self, tmp_path):
        storage = _make_storage(tmp_path)
        await storage.ensure_default_provider()

        seed = self._seed_path(tmp_path)
        assert seed.exists(), "seed GeoPackage was not written"
        gdf = gpd.read_file(str(seed), engine="pyogrio")
        assert len(gdf) == 1

        provider = yaml.safe_load(self._default_provider_path(tmp_path).read_text())
        # Provider id MUST equal the service id — that is how ldproxy resolves
        # the service's default provider.
        assert provider["id"] == "ump-results"
        assert provider["connectionInfo"]["database"] == "__ump_default__.gpkg"

    @pytest.mark.asyncio
    async def test_default_provider_uses_fid_geom_columns(self, tmp_path):
        """The entity's columns must match what write_seed_gpkg (pyogrio/GDAL)
        actually emits — fid/geom — or ldproxy fails at query time with a
        missing-column SQL error (the OBJECTID/Shape class of bug)."""
        storage = _make_storage(tmp_path)
        await storage.ensure_default_provider()

        provider = yaml.safe_load(self._default_provider_path(tmp_path).read_text())
        assert provider["sourcePathDefaults"]["primaryKey"] == "fid"
        typ = provider["types"]["default"]
        assert typ["properties"]["id"]["sourcePath"] == "fid"
        assert typ["properties"]["geometry"]["sourcePath"] == "geom"

    @pytest.mark.asyncio
    async def test_default_type_is_not_a_collection(self, tmp_path):
        """The default provider's single type must never appear as a published
        collection, so it stays invisible under /collections."""
        storage = _make_storage(tmp_path)
        await storage.ensure_default_provider()

        service = _service(tmp_path)
        collections = (service or {}).get("collections") or {}
        assert "default" not in collections
        assert "ump-results-default" not in collections

    @pytest.mark.asyncio
    async def test_idempotent_and_seed_written_only_when_missing(self, tmp_path):
        """A second call must not fail and must not rewrite the existing seed
        (the write-only-when-missing optimisation), while still refreshing the
        provider entity via an atomic overwrite of identical content."""
        storage = _make_storage(tmp_path)
        await storage.ensure_default_provider()

        seed = self._seed_path(tmp_path)
        first_mtime = seed.stat().st_mtime_ns

        await storage.ensure_default_provider()  # must not raise
        assert seed.stat().st_mtime_ns == first_mtime, (
            "seed GeoPackage was rewritten on the second call; it should only "
            "be written when missing"
        )

    @pytest.mark.asyncio
    async def test_respects_custom_service_id(self, tmp_path):
        backend = FilesystemEntityConfigBackend(tmp_path)
        registry = ServiceRegistry(backend, service_id="custom-svc")
        storage = LdproxyResultStorage(
            backend=backend,
            service_registry=registry,
            root_path=tmp_path,
            base_url=BASE_URL,
            service_id="custom-svc",
        )
        await storage.ensure_default_provider()

        path = tmp_path / "entities" / "instances" / "providers" / "custom-svc.yml"
        provider = yaml.safe_load(path.read_text())
        assert provider["id"] == "custom-svc"


class TestConfirmPublication:
    """The post-store step that works around ldproxy's provider/service reload
    ordering: re-touch the service entity until each collection is confirmed
    live, and flag any that never confirm as ``publication_pending``."""

    @pytest.mark.asyncio
    async def test_no_probe_marks_nothing_pending(self, tmp_path):
        """Without an internal_url UMP cannot observe liveness, so it must not
        alarm the client — references are returned with pending=False."""
        storage = _make_storage(tmp_path)  # internal_url unset
        refs = await storage.store(JOB_ID, [_payload("voronoi")])
        assert refs[0].publication_pending is False

    @pytest.mark.asyncio
    async def test_confirms_live_after_retouch(self, tmp_path, monkeypatch):
        """When the probe reports the collection live, no reference is flagged
        pending and the confirm loop stops early."""
        storage = _make_storage(
            tmp_path,
            internal_url="http://ldproxy:7080/ump-results",
            confirm_max_attempts=5,
            confirm_base_wait=0.0,
            confirm_max_wait=0.0,
        )
        # Live on the very first probe.
        monkeypatch.setattr(storage, "_probe_collection_live", lambda cid: True)
        refs = await storage.store(JOB_ID, [_payload("voronoi")])
        assert refs[0].publication_pending is False

    @pytest.mark.asyncio
    async def test_flags_pending_when_never_confirmed(self, tmp_path, monkeypatch):
        """When the probe never reports live within the budget, the reference is
        flagged pending (honest signalling) but the store still succeeds."""
        storage = _make_storage(
            tmp_path,
            internal_url="http://ldproxy:7080/ump-results",
            confirm_max_attempts=3,
            confirm_base_wait=0.0,
            confirm_max_wait=0.0,
        )
        monkeypatch.setattr(storage, "_probe_collection_live", lambda cid: False)
        refs = await storage.store(JOB_ID, [_payload("voronoi")])
        assert refs[0].publication_pending is True
        # The data + entities are still fully written — a pending publication is
        # a success, not a failure.
        assert _gpkg_path(tmp_path).exists()
        assert f"{JOB_ID}-voronoi" in _service(tmp_path)["collections"]

    @pytest.mark.asyncio
    async def test_pending_only_for_unconfirmed_collection(self, tmp_path, monkeypatch):
        """With multiple outputs, only the collection the probe cannot confirm
        is flagged pending; a confirmed sibling stays live."""
        storage = _make_storage(
            tmp_path,
            internal_url="http://ldproxy:7080/ump-results",
            confirm_max_attempts=2,
            confirm_base_wait=0.0,
            confirm_max_wait=0.0,
        )

        def probe(collection_id: str) -> bool:
            return collection_id.endswith("voronoi")  # buffer never confirms

        monkeypatch.setattr(storage, "_probe_collection_live", probe)
        refs = await storage.store(JOB_ID, [_payload("voronoi"), _payload("buffer")])
        pending = {r.collection_id: r.publication_pending for r in refs}
        assert pending[f"{JOB_ID}-voronoi"] is False
        assert pending[f"{JOB_ID}-buffer"] is True


class TestLivenessUrl:
    """V-11: StoredReference.liveness_url is built from the internal base URL
    so the generic liveness probe works from inside the UMP container."""

    @pytest.mark.asyncio
    async def test_liveness_url_built_from_internal_base_url(self, tmp_path):
        storage = _make_storage(
            tmp_path, internal_url="http://ldproxy:7080/ump-results"
        )
        refs = await storage.store(JOB_ID, [_payload("voronoi")])

        assert refs[0].liveness_url == (
            f"http://ldproxy:7080/ump-results/collections/{JOB_ID}-voronoi"
            "/items?limit=1"
        )

    @pytest.mark.asyncio
    async def test_liveness_url_none_without_internal_url(self, tmp_path):
        """No internal URL configured -> no generic probe target; callers
        fall back to items_url/collection_url (see result_storage_coordinator)."""
        storage = _make_storage(tmp_path)  # internal_url unset

        refs = await storage.store(JOB_ID, [_payload("voronoi")])

        assert refs[0].liveness_url is None
