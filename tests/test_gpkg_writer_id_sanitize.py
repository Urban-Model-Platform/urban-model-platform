"""Tests for gpkg_writer's reserved-name sanitisation and ID-role resolution.

Covers ``_sanitize_and_resolve_id`` (Option B): reserved-name data columns are
renamed losslessly (never dropped), and the ldproxy ID role is backed by a real
``id`` column only when it is a unique, non-null identifier — otherwise that
column is kept as an attribute and the synthetic ``fid`` primary key is used.

These run against real GeoDataFrames (no GeoPackage file / ldproxy needed).
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Point

from ump.adapters.result_storage.gpkg_writer import (
    _derive_schema,
    _sanitize_and_resolve_id,
)


def _gdf(data: dict, n: int = 3) -> gpd.GeoDataFrame:
    geoms = [Point(i, i) for i in range(n)]
    return gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")


class TestIdResolution:
    def test_unique_string_id_is_promoted(self):
        gdf = _gdf({"id": ["a", "b", "c"], "height": [1.0, 2.0, 3.0]})

        out, id_source, id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert id_source == "id"
        assert id_type == "STRING"
        assert promoted == "id"
        # id column is retained in the frame (written to GPKG), not dropped.
        assert "id" in out.columns
        # And it is excluded from the plain data properties.
        schema = _derive_schema(out, 4326, id_source, id_type, promoted)
        assert "id" not in schema.properties
        assert schema.id_source_path == "id"
        assert schema.id_type == "STRING"
        assert schema.properties["height"] == "FLOAT"

    def test_unique_integer_id_is_promoted_as_integer(self):
        gdf = _gdf({"id": [10, 20, 30]})

        out, id_source, id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert id_source == "id"
        assert id_type == "INTEGER"
        assert promoted == "id"

    def test_non_unique_id_falls_back_to_fid_and_is_kept(self):
        gdf = _gdf({"id": ["dup", "dup", "x"], "v": [1, 2, 3]})

        out, id_source, id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert id_source == "fid"
        assert id_type == "INTEGER"
        assert promoted is None
        # The invalid id is NOT lost — renamed to an attribute.
        assert "id" not in out.columns
        assert "id_attr" in out.columns
        schema = _derive_schema(out, 4326, id_source, id_type, promoted)
        assert "id_attr" in schema.properties

    def test_null_containing_id_falls_back_to_fid(self):
        gdf = _gdf({"id": ["a", None, "c"]})

        out, id_source, _id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert id_source == "fid"
        assert promoted is None
        assert "id_attr" in out.columns

    def test_no_id_column_uses_fid(self):
        gdf = _gdf({"name": ["a", "b", "c"]})

        out, id_source, id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert id_source == "fid"
        assert id_type == "INTEGER"
        assert promoted is None
        assert "name" in out.columns

    def test_case_insensitive_id_match(self):
        gdf = _gdf({"ID": ["a", "b", "c"]})

        out, id_source, _id_type, promoted = _sanitize_and_resolve_id(gdf, "layer")

        # The actual column name is preserved as the sourcePath.
        assert id_source == "ID"
        assert promoted == "ID"


class TestReservedNameSanitisation:
    def test_reserved_data_columns_are_renamed_not_dropped(self):
        gdf = _gdf(
            {
                "geom": ["x", "y", "z"],  # attribute clashing with geometry col
                "fid": [1, 2, 3],  # clashing with primary key
                "real": [1.0, 2.0, 3.0],
            }
        )

        out, _id_source, _id_type, _promoted = _sanitize_and_resolve_id(gdf, "layer")

        cols = set(out.columns)
        # Reserved data columns renamed, nothing lost.
        assert "geom_attr" in cols
        assert "fid_attr" in cols
        assert "real" in cols
        # No plain data column named exactly like a reserved key remains.
        assert "geom" not in [c for c in out.columns if c != out.geometry.name]
        assert "fid" not in out.columns

    def test_active_geometry_is_never_renamed(self):
        gdf = _gdf({"real": [1.0, 2.0, 3.0]})
        geom_name = gdf.geometry.name

        out, _id_source, _id_type, _promoted = _sanitize_and_resolve_id(gdf, "layer")

        assert out.geometry.name == geom_name

    def test_rename_collision_is_uniquified(self):
        # Both a 'geom' attribute and an existing 'geom_attr' — the rename must
        # not collide with the pre-existing column.
        gdf = _gdf(
            {
                "geom": ["a", "b", "c"],
                "geom_attr": ["d", "e", "f"],
            }
        )

        out, _id_source, _id_type, _promoted = _sanitize_and_resolve_id(gdf, "layer")

        cols = set(out.columns)
        assert "geom_attr" in cols  # the original one survives
        assert "geom_attr_1" in cols  # the renamed reserved column
        # No data column named exactly 'geom' remains.
        assert "geom" not in [c for c in out.columns if c != out.geometry.name]

    def test_no_data_columns_lost_overall(self):
        data = {
            "id": ["a", "b", "c"],  # promoted
            "geom": ["p", "q", "r"],  # renamed
            "value": [1, 2, 3],  # untouched
        }
        gdf = _gdf(data)

        out, _id_source, _id_type, _promoted = _sanitize_and_resolve_id(gdf, "layer")

        # Every original attribute is still represented (possibly renamed),
        # plus the geometry column.
        non_geom = [c for c in out.columns if c != out.geometry.name]
        assert set(non_geom) == {"id", "geom_attr", "value"}
