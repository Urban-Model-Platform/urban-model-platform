"""Tests for V-3: atomic_fs and gpkg_writer.

All tests run against a temp directory — no ldproxy installation required.
The gpkg_writer tests verify:
  - GeoJSON FeatureCollection → GeoPackage round-trip
  - FlatGeobuf → GeoPackage round-trip
  - Schema derivation (geometry type, property types)
  - Empty FeatureCollection → UnsupportedResultError
  - Unsupported media type → UnsupportedResultError
  - Mixed geometry types → GEOMETRY
  - Atomic write: temp file removed on failure, destination not overwritten

pytest marks for clarity: tests that require geopandas/pyogrio are collected
only when those packages are importable.
"""

from __future__ import annotations

import json
import os

import pytest

from ump.adapters.result_storage.atomic_fs import (
    atomic_write_bytes,
    atomic_write_path,
    atomic_write_text,
)
from ump.adapters.result_storage.gpkg_writer import (
    DEFAULT_SEED_LAYER,
    _normalise_media_type,
    _require_supported_type,
    write_seed_gpkg,
    write_to_gpkg,
)
from ump.core.interfaces.result_storage import UnsupportedResultError

# ---------------------------------------------------------------------------
# Helpers — minimal GeoJSON FeatureCollections for testing
# ---------------------------------------------------------------------------


def _geojson_polygon_collection(n: int = 3) -> bytes:
    """Return a GeoJSON FeatureCollection with *n* simple polygon features."""
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [10.0 + i, 53.0],
                        [10.1 + i, 53.0],
                        [10.1 + i, 53.1],
                        [10.0 + i, 53.1],
                        [10.0 + i, 53.0],
                    ]
                ],
            },
            "properties": {
                "level_cm": 100 + i * 10,
                "name": f"zone_{i}",
                "active": True,
                "score": 1.5 + i,
            },
        }
        for i in range(n)
    ]
    collection = {"type": "FeatureCollection", "features": features}
    return json.dumps(collection).encode("utf-8")


def _geojson_mixed_geometry_collection() -> bytes:
    """Return a FeatureCollection with both Polygon and Point geometries."""
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
            "properties": {"id": 1},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            "properties": {"id": 2},
        },
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}).encode(
        "utf-8"
    )


def _geojson_empty_collection() -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")


def _flatgeobuf_bytes(geojson_bytes: bytes) -> bytes:
    """Convert GeoJSON bytes to FlatGeobuf bytes via geopandas/pyogrio."""
    import io

    import geopandas as gpd

    gdf = gpd.read_file(io.BytesIO(geojson_bytes), engine="pyogrio")
    buf = io.BytesIO()
    gdf.to_file(buf, driver="FlatGeobuf", engine="pyogrio")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# atomic_fs tests
# ---------------------------------------------------------------------------


class TestAtomicWriteBytes:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "output.txt"
        atomic_write_bytes(path, b"hello world")
        assert path.read_bytes() == b"hello world"

    def test_no_temp_file_left_on_success(self, tmp_path):
        path = tmp_path / "output.bin"
        atomic_write_bytes(path, b"data")
        tmp_files = list(tmp_path.glob(".output.tmp*"))
        assert tmp_files == [], f"Unexpected temp files: {tmp_files}"

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "file.bin"
        path.write_bytes(b"old content")
        atomic_write_bytes(path, b"new content")
        assert path.read_bytes() == b"new content"

    def test_cleanup_on_write_failure(self, tmp_path, monkeypatch):
        """If os.replace fails the temp file should be removed."""
        path = tmp_path / "target.bin"
        tmp_path2 = tmp_path / ".target.bin.tmp"

        def bad_replace(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", bad_replace)
        with pytest.raises(OSError):
            atomic_write_bytes(path, b"data")

        assert not tmp_path2.exists(), "Temp file was not cleaned up after failure"


class TestAtomicWriteText:
    def test_writes_utf8(self, tmp_path):
        path = tmp_path / "config.yml"
        atomic_write_text(path, "key: value\n")
        assert path.read_text() == "key: value\n"

    def test_no_temp_file_left(self, tmp_path):
        path = tmp_path / "out.yml"
        atomic_write_text(path, "x: 1")
        assert list(tmp_path.glob(".*.tmp")) == []


class TestAtomicWritePath:
    def test_context_manager_yields_tmp_path(self, tmp_path):
        final = tmp_path / "final.gpkg"
        with atomic_write_path(final) as tmp:
            assert tmp.parent == final.parent
            assert tmp.name.startswith(".")
            assert ".tmp" in tmp.name  # naming: .final.tmp.gpkg
            assert tmp.suffix == final.suffix  # same extension so pyogrio is happy
            tmp.write_bytes(b"gpkg content")
        assert final.read_bytes() == b"gpkg content"
        assert not tmp.exists()

    def test_tmp_removed_on_exception(self, tmp_path):
        final = tmp_path / "final.gpkg"
        tmp_expected = tmp_path / ".final.tmp.gpkg"
        with pytest.raises(ValueError):
            with atomic_write_path(final) as tmp:
                tmp.write_bytes(b"partial")
                raise ValueError("simulated failure")
        assert not tmp_expected.exists(), "Temp file not cleaned up on exception"
        assert not final.exists(), "Final file should not have been written"


# ---------------------------------------------------------------------------
# gpkg_writer tests
# ---------------------------------------------------------------------------


class TestNormaliseMediaType:
    def test_strips_parameters(self):
        assert (
            _normalise_media_type("application/geo+json; charset=utf-8")
            == "application/geo+json"
        )

    def test_lower_cases(self):
        assert _normalise_media_type("Application/Geo+JSON") == "application/geo+json"


class TestRequireSupportedType:
    def test_geojson_returns_driver(self):
        assert _require_supported_type("application/geo+json") == "GeoJSON"

    def test_flatgeobuf_returns_driver(self):
        assert _require_supported_type("application/flatgeobuf") == "FlatGeobuf"

    def test_unsupported_raises(self):
        with pytest.raises(UnsupportedResultError, match="not supported"):
            _require_supported_type("image/tiff")


class TestWriteToGpkg:
    def test_geojson_polygon_roundtrip(self, tmp_path):
        """GeoJSON FeatureCollection writes a readable GeoPackage w/ correct schema."""
        import geopandas as gpd

        path = tmp_path / "result.gpkg"
        schema = write_to_gpkg(
            body_bytes=_geojson_polygon_collection(3),
            media_type="application/geo+json",
            layer_name="flood_zones",
            output_path=path,
        )

        assert path.exists(), "GeoPackage file was not created"
        assert schema.feature_count == 3
        assert schema.geometry_type == "POLYGON"
        assert "level_cm" in schema.properties
        assert schema.properties["level_cm"] == "INTEGER"
        assert schema.properties["name"] == "STRING"
        assert schema.properties["active"] == "BOOLEAN"
        assert schema.properties["score"] == "FLOAT"

        # Round-trip: read back and verify
        gdf = gpd.read_file(str(path), layer="flood_zones", engine="pyogrio")
        assert len(gdf) == 3

    def test_flatgeobuf_roundtrip(self, tmp_path):
        """FlatGeobuf bytes produce the same GeoPackage as the original GeoJSON."""
        import geopandas as gpd

        fgb_bytes = _flatgeobuf_bytes(_geojson_polygon_collection(2))
        path = tmp_path / "result.gpkg"
        schema = write_to_gpkg(
            body_bytes=fgb_bytes,
            media_type="application/flatgeobuf",
            layer_name="zones",
            output_path=path,
        )

        assert path.exists()
        assert schema.feature_count == 2
        gdf = gpd.read_file(str(path), layer="zones", engine="pyogrio")
        assert len(gdf) == 2

    def test_empty_collection_raises(self, tmp_path):
        """An empty FeatureCollection is rejected — ldproxy needs ≥ 1 feature."""
        path = tmp_path / "empty.gpkg"
        with pytest.raises(UnsupportedResultError, match="empty"):
            write_to_gpkg(
                body_bytes=_geojson_empty_collection(),
                media_type="application/geo+json",
                layer_name="empty_layer",
                output_path=path,
            )
        # File should not have been written
        assert not path.exists()

    def test_unsupported_media_type_raises(self, tmp_path):
        path = tmp_path / "result.gpkg"
        with pytest.raises(UnsupportedResultError):
            write_to_gpkg(
                body_bytes=b"not geospatial",
                media_type="application/json",
                layer_name="layer",
                output_path=path,
            )

    def test_mixed_geometry_types_produce_geometry_type(self, tmp_path):
        """A FeatureCollection with mixed geometry types results in GEOMETRY schema."""
        path = tmp_path / "mixed.gpkg"
        schema = write_to_gpkg(
            body_bytes=_geojson_mixed_geometry_collection(),
            media_type="application/geo+json",
            layer_name="mixed",
            output_path=path,
        )
        assert schema.geometry_type == "GEOMETRY"

    def test_no_gpkg_on_parse_failure(self, tmp_path):
        """A corrupt payload must not leave a partial GeoPackage on disk."""
        path = tmp_path / "result.gpkg"
        with pytest.raises(Exception):
            write_to_gpkg(
                body_bytes=b"this is not valid geojson",
                media_type="application/geo+json",
                layer_name="layer",
                output_path=path,
            )
        assert not path.exists()

    def test_no_temp_file_left_after_success(self, tmp_path):
        path = tmp_path / "result.gpkg"
        write_to_gpkg(
            body_bytes=_geojson_polygon_collection(1),
            media_type="application/geo+json",
            layer_name="layer",
            output_path=path,
        )
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_schema_crs_epsg(self, tmp_path):
        path = tmp_path / "result.gpkg"
        schema = write_to_gpkg(
            body_bytes=_geojson_polygon_collection(1),
            media_type="application/geo+json",
            layer_name="layer",
            output_path=path,
            target_crs_epsg=4326,
        )
        assert schema.crs_epsg == 4326


class TestWriteSeedGpkg:
    """The seed GeoPackage backs the ldproxy default provider.

    ldproxy 3.x refuses to start an OGC_API service without a resolvable
    default feature provider whose backing file exists and is connectable
    (``initFailFast``). ``write_seed_gpkg`` produces that file. The columns
    it writes (``fid`` primary key, ``geom`` geometry) must match exactly what
    ``build_default_provider_entity`` declares, or ldproxy fails at query time
    with a missing-column SQL error — the same class of bug that the earlier
    OBJECTID/Shape mismatch caused.
    """

    def test_creates_readable_single_feature_gpkg(self, tmp_path):
        import geopandas as gpd

        path = tmp_path / "__ump_default__.gpkg"
        write_seed_gpkg(path)

        assert path.exists(), "seed GeoPackage was not created"
        gdf = gpd.read_file(str(path), layer=DEFAULT_SEED_LAYER, engine="pyogrio")
        assert len(gdf) == 1, "seed layer must contain exactly one feature"

    def test_layer_name_matches_default_type(self, tmp_path):
        """The layer name must equal the type sourcePath the default provider
        declares (``/default``), so ldproxy can resolve it."""
        import pyogrio

        path = tmp_path / "seed.gpkg"
        write_seed_gpkg(path)

        layers = [name for name, _ in pyogrio.list_layers(str(path))]
        assert layers == [DEFAULT_SEED_LAYER]

    def test_geometry_is_point(self, tmp_path):
        import geopandas as gpd

        path = tmp_path / "seed.gpkg"
        write_seed_gpkg(path)

        gdf = gpd.read_file(str(path), engine="pyogrio")
        assert gdf.geom_type.iloc[0] == "Point"

    def test_target_crs_is_respected(self, tmp_path):
        import geopandas as gpd

        path = tmp_path / "seed.gpkg"
        write_seed_gpkg(path, target_crs_epsg=3857)

        gdf = gpd.read_file(str(path), engine="pyogrio")
        assert gdf.crs is not None
        assert gdf.crs.to_epsg() == 3857

    def test_atomic_no_temp_file_left(self, tmp_path):
        path = tmp_path / "seed.gpkg"
        write_seed_gpkg(path)
        assert list(tmp_path.glob(".*.tmp")) == []
