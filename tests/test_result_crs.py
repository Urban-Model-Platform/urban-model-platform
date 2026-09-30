"""Result source CRS: declare the CRS the remote actually produced (REF-F5 plan
2026-09-30, AC1-AC12).

Fakes: ``Mock``/``AsyncMock`` stand in for the storage port, HTTP client and
providers port at the coordinator boundary; ``_providers_with_default`` stands
in for ``ProvidersPort`` in composition. GeoPackage writing uses real
geopandas/pyogrio against ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import geopandas as gpd
import pytest

from tests.test_result_storage_v6 import JOB_ID, _gpkg_path, _make_storage
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.job_repository_sql import JobRecord
from ump.adapters.result_storage.gpkg_writer import write_layers_to_gpkg, write_to_gpkg
from ump.composition.result_storage import validate_result_crs_defaults
from ump.core.config import JobManagerConfig
from ump.core.interfaces.result_storage import (
    ResultPayload,
    ResultStorageError,
    UnsupportedResultError,
)
from ump.core.managers.job_manager import JobExecutionContext
from ump.core.managers.steps.execution_steps import CreateLocalJobStep
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.providers_config import ProcessConfig, ProviderConfig
from ump.core.services.result_storage_coordinator import ResultStorageCoordinator

# A point in Hamburg: UTM 32N (EPSG:25832) and its WGS84 lon/lat (pyproj).
UTM32_XY = (565000.0, 5933000.0)
HAMBURG_LONLAT = (9.9809, 53.5419)
GEOJSON = "application/geo+json"


def _point_geojson(xy: tuple[float, float], crs: str | None = None) -> bytes:
    doc: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "a"},
                "geometry": {"type": "Point", "coordinates": list(xy)},
            }
        ],
    }
    if crs is not None:
        doc["crs"] = {"type": "name", "properties": {"name": crs}}
    return json.dumps(doc).encode()


def _write(tmp_path, body: bytes, source_crs: str | None) -> gpd.GeoDataFrame:
    path = tmp_path / "out.gpkg"
    write_to_gpkg(body, GEOJSON, "layer", path, source_crs=source_crs)
    return gpd.read_file(str(path), layer="layer", engine="pyogrio")


def _lonlat(gdf: gpd.GeoDataFrame) -> tuple[float, float]:
    point = gdf.geometry.iloc[0]
    return round(point.x, 4), round(point.y, 4)


def _process(**config: Any) -> ProcessConfig:
    return ProcessConfig.model_validate(
        {
            "id": "proc",
            "transmission-mode-policy": "emulate-ref",
            "result-storage": "ldproxy",
            **config,
        }
    )


# ---------------------------------------------------------------------------
# AC1–AC6: gpkg writer labels and reprojects by the declared source CRS
# ---------------------------------------------------------------------------


class TestSourceCrs:
    def test_projected_result_is_reprojected_to_lonlat(self, tmp_path):
        """AC1"""
        gdf = _write(tmp_path, _point_geojson(UTM32_XY), "EPSG:25832")

        assert _lonlat(gdf) == HAMBURG_LONLAT

    @pytest.mark.parametrize(
        "notation",
        [
            "EPSG:25832",
            "25832",
            "http://www.opengis.net/def/crs/EPSG/0/25832",
            "urn:ogc:def:crs:EPSG::25832",
        ],
    )
    def test_accepts_common_crs_notations(self, tmp_path, notation):
        """AC2"""
        gdf = _write(tmp_path, _point_geojson(UTM32_XY), notation)

        assert _lonlat(gdf) == HAMBURG_LONLAT

    def test_crs_declared_in_data_wins_over_configured(self, tmp_path):
        """AC3"""
        body = _point_geojson(UTM32_XY, crs="urn:ogc:def:crs:EPSG::25832")

        gdf = _write(tmp_path, body, "EPSG:3857")

        assert _lonlat(gdf) == HAMBURG_LONLAT

    def test_warns_when_data_and_configured_crs_disagree(self, tmp_path, caplog):
        """AC3"""
        body = _point_geojson(UTM32_XY, crs="urn:ogc:def:crs:EPSG::25832")

        with caplog.at_level(logging.WARNING):
            _write(tmp_path, body, "EPSG:3857")

        assert "EPSG:3857" in caplog.text

    def test_unparseable_source_crs_names_the_value(self, tmp_path):
        """AC4"""
        with pytest.raises(ResultStorageError, match="EPSG:999999"):
            _write(tmp_path, _point_geojson(UTM32_XY), "EPSG:999999")

    @pytest.mark.parametrize("source_crs", [None, "EPSG:4326"])
    def test_projected_coordinates_labelled_lonlat_are_refused(
        self, tmp_path, source_crs
    ):
        """AC5"""
        with pytest.raises(UnsupportedResultError, match="result-crs-default"):
            _write(tmp_path, _point_geojson(UTM32_XY), source_crs)

    def test_unknown_crs_keeps_lonlat_unchanged(self, tmp_path):
        """AC6"""
        gdf = _write(tmp_path, _point_geojson(HAMBURG_LONLAT), None)

        assert _lonlat(gdf) == HAMBURG_LONLAT

    def test_multilayer_applies_source_crs_per_layer(self, tmp_path):
        """AC1 (multi-output path)"""
        path = tmp_path / "multi.gpkg"

        write_layers_to_gpkg(
            [
                ("utm", _point_geojson(UTM32_XY), GEOJSON, "EPSG:25832"),
                ("wgs", _point_geojson(HAMBURG_LONLAT), GEOJSON),
            ],
            path,
        )

        layers = {
            name: _lonlat(gpd.read_file(str(path), layer=name, engine="pyogrio"))
            for name in ("utm", "wgs")
        }
        assert layers == {"utm": HAMBURG_LONLAT, "wgs": HAMBURG_LONLAT}


class TestLdproxyStoreUsesSourceCrs:
    @pytest.mark.asyncio
    async def test_payload_source_crs_reaches_the_geopackage(self, tmp_path):
        """AC1 (adapter wiring)"""
        payload = ResultPayload(
            output_id="noise",
            body_bytes=_point_geojson(UTM32_XY),
            media_type=GEOJSON,
            source_crs="EPSG:25832",
        )

        await _make_storage(tmp_path).store(JOB_ID, [payload])

        gdf = gpd.read_file(str(_gpkg_path(tmp_path)), layer="noise", engine="pyogrio")
        assert _lonlat(gdf) == HAMBURG_LONLAT


# ---------------------------------------------------------------------------
# AC7: resolving the CRS from the execute inputs
# ---------------------------------------------------------------------------


class TestResolveResultCrs:
    @pytest.mark.parametrize(
        ("config", "inputs", "expected"),
        [
            pytest.param(
                {"result-crs-input-field": "crs", "result-crs-default": "EPSG:4326"},
                {"crs": "EPSG:25832"},
                "EPSG:25832",
                id="input-beats-default",
            ),
            pytest.param(
                {"result-crs-input-field": "crs", "result-crs-default": "EPSG:4326"},
                {"other": 1},
                "EPSG:4326",
                id="default-when-input-omitted",
            ),
            pytest.param(
                {"result-crs-input-field": "crs"},
                {"crs": {"value": "EPSG:25832"}},
                "EPSG:25832",
                id="qualified-value-unwrapped",
            ),
            pytest.param(
                {"result-crs-input-field": "epsg"},
                {"epsg": 25832},
                "25832",
                id="numeric-value-stringified",
            ),
            pytest.param({}, {"crs": "EPSG:25832"}, None, id="neither-configured"),
            pytest.param(
                {"result-crs-input-field": "crs"}, None, None, id="no-inputs"
            ),
        ],
    )
    def test_resolves_crs(self, config, inputs, expected):
        """AC7"""
        process = _process(**config)

        result = process.resolve_result_crs(inputs)

        assert result == expected


# ---------------------------------------------------------------------------
# AC8: the create step captures the CRS even when inputs are not kept inline
# ---------------------------------------------------------------------------


def _create_context(inputs: dict[str, Any]) -> JobExecutionContext:
    ctx = JobExecutionContext(
        process_id="prov:proc",
        execute_payload={"inputs": inputs},
        provider=ProviderConfig.model_validate(
            {"name": "prov", "url": "http://prov.local/", "processes": []}
        ),
    )
    ctx.process_config = _process(**{"result-crs-input-field": "crs"})
    return ctx


class TestCreateLocalJobCapturesResultCrs:
    @pytest.mark.parametrize("inline_limit", [64 * 1024, 0], ids=["inline", "object"])
    @pytest.mark.asyncio
    async def test_job_carries_resolved_crs(self, inline_limit):
        """AC8"""
        ctx = _create_context({"crs": "EPSG:25832", "big": "x" * 100})
        step = CreateLocalJobStep(JobManagerConfig(inline_inputs_size_limit=inline_limit))

        await step.process(ctx)

        assert ctx.job is not None and ctx.job.result_crs == "EPSG:25832"


# ---------------------------------------------------------------------------
# AC9: the coordinator hands the job's CRS to the storage port
# ---------------------------------------------------------------------------


def _job(**overrides: Any) -> Job:
    return Job(
        id=JOB_ID,
        process_id="prov:proc",
        provider="prov",
        remote_job_id="remote-1",
        status="successful",
        status_info=JobStatusInfo(
            jobID=JOB_ID,
            status=StatusCode.successful,
            processID="prov:proc",
            created=datetime.now(timezone.utc),
            progress=0,
        ),
        **overrides,
    )


class TestCoordinatorPassesSourceCrs:
    @pytest.mark.asyncio
    async def test_payloads_carry_job_result_crs(self):
        """AC9"""
        job = _job(result_crs="EPSG:25832")
        repo = InMemoryJobRepository()
        await repo.create(job)
        storage = Mock()
        storage.exists = AsyncMock(return_value=False)
        storage.store = AsyncMock(return_value=[])
        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(_point_geojson(UTM32_XY), GEOJSON)
        )
        providers = Mock()
        providers.get_provider = Mock(return_value=Mock(url="https://remote.local"))
        coordinator = ResultStorageCoordinator(
            storage_port=storage, http_client=http_client, providers=providers
        )

        await coordinator.coordinate(job, _process(), repo)

        payloads = storage.store.call_args.args[1]
        assert [p.source_crs for p in payloads] == ["EPSG:25832"]


# ---------------------------------------------------------------------------
# AC10: persisted across the SQL mapping
# ---------------------------------------------------------------------------


class TestJobRecordResultCrs:
    def test_round_trips_through_record(self):
        """AC10"""
        job = _job(result_crs="EPSG:25832")

        restored = JobRecord.from_domain(job).to_domain()

        assert restored.result_crs == "EPSG:25832"


# ---------------------------------------------------------------------------
# AC11: config aliases and ignored-field warnings
# ---------------------------------------------------------------------------


class TestResultCrsConfig:
    def test_aliases_populate_fields(self):
        """AC11"""
        process = _process(
            **{"result-crs-input-field": "crs", "result-crs-default": "EPSG:25832"}
        )

        fields = (process.result_crs_input_field, process.result_crs_default)

        assert fields == ("crs", "EPSG:25832")

    @pytest.mark.parametrize("field", ["result-crs-input-field", "result-crs-default"])
    def test_warns_when_policy_never_stores(self, field):
        """AC11"""
        process = ProcessConfig.model_validate({"id": "proc", field: "x"})

        warnings = process.policy_warnings()

        assert any(field in w for w in warnings)

    def test_no_warning_when_policy_stores(self):
        """AC11"""
        process = _process(**{"result-crs-default": "EPSG:25832"})

        warnings = process.policy_warnings()

        assert not any("result-crs" in w for w in warnings)


# ---------------------------------------------------------------------------
# AC12: invalid default fails at startup
# ---------------------------------------------------------------------------


def _providers_with_default(default: str, storage: str = "ldproxy") -> Mock:
    process = Mock(id="proc", result_storage=storage, result_crs_default=default)
    provider = Mock(processes=[process])
    provider.name = "prov"
    return Mock(get_providers=Mock(return_value=[provider]))


class TestValidateResultCrsDefaults:
    def test_invalid_default_fails_naming_process_and_value(self):
        """AC12"""
        providers = _providers_with_default("EPSG:999999")

        with pytest.raises(ValueError, match="'proc'.*EPSG:999999"):
            validate_result_crs_defaults(providers)

    @pytest.mark.parametrize(
        ("default", "storage"),
        [("EPSG:25832", "ldproxy"), ("EPSG:999999", "remote")],
        ids=["valid", "not-ldproxy"],
    )
    def test_passes(self, default, storage):
        """AC12"""
        providers = _providers_with_default(default, storage)

        validate_result_crs_defaults(providers)
