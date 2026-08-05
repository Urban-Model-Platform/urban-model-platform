"""Tests for the V-6 groundwork added to gpkg_writer.py:

  - ``validate_output_id`` — the identifier safeguard shared by the
    GeoPackage layer name, the ldproxy provider ``types`` key, and the
    collection id (``{job_uuid}-{output_id}``).
  - ``write_layers_to_gpkg`` — writes multiple outputs of the same job as
    separate layers in one GeoPackage (the multi-output counterpart to the
    existing single-layer ``write_to_gpkg`` from V-3).

Reuses the GeoJSON fixture helpers from test_result_storage_v3.py so the two
suites stay in sync without duplicating fixture data.
"""

from __future__ import annotations

import geopandas as gpd
import pytest

from tests.test_result_storage_v3 import (
    _geojson_empty_collection,
    _geojson_polygon_collection,
)
from ump.adapters.result_storage.gpkg_writer import (
    validate_output_id,
    write_layers_to_gpkg,
)
from ump.core.interfaces.result_storage import UnsupportedResultError


class TestValidateOutputId:
    @pytest.mark.parametrize(
        "output_id", ["voronoi", "voronoi_diagram", "Zone1", "a", "zone_1"]
    )
    def test_valid_identifiers_pass(self, output_id):
        validate_output_id(output_id)  # no raise

    @pytest.mark.parametrize(
        "output_id",
        [
            "",  # empty
            "1starts_with_digit",
            "_leading_underscore",  # must start with a letter, not underscore
            "has space",
            "has.dot",
            "has/slash",
            "has-dash",
            "résumé",  # non-ASCII
        ],
    )
    def test_invalid_identifiers_raise(self, output_id):
        with pytest.raises(
            UnsupportedResultError, match="not a valid storage identifier"
        ):
            validate_output_id(output_id)


class TestWriteLayersToGpkg:
    def test_writes_multiple_layers_in_one_file(self, tmp_path):
        path = tmp_path / "job-1.gpkg"
        schemas = write_layers_to_gpkg(
            layers=[
                ("voronoi", _geojson_polygon_collection(3), "application/geo+json"),
                ("buffer", _geojson_polygon_collection(2), "application/geo+json"),
            ],
            output_path=path,
        )

        assert path.exists()
        assert set(schemas) == {"voronoi", "buffer"}
        assert schemas["voronoi"].feature_count == 3
        assert schemas["buffer"].feature_count == 2

        voronoi_gdf = gpd.read_file(str(path), layer="voronoi", engine="pyogrio")
        buffer_gdf = gpd.read_file(str(path), layer="buffer", engine="pyogrio")
        assert len(voronoi_gdf) == 3
        assert len(buffer_gdf) == 2

    def test_single_layer_also_works(self, tmp_path):
        path = tmp_path / "job-2.gpkg"
        schemas = write_layers_to_gpkg(
            layers=[
                ("voronoi", _geojson_polygon_collection(1), "application/geo+json")
            ],
            output_path=path,
        )
        assert set(schemas) == {"voronoi"}

    def test_empty_layers_list_raises(self, tmp_path):
        path = tmp_path / "job-3.gpkg"
        with pytest.raises(UnsupportedResultError, match="at least one layer"):
            write_layers_to_gpkg(layers=[], output_path=path)
        assert not path.exists()

    def test_invalid_output_id_raises_before_writing(self, tmp_path):
        path = tmp_path / "job-4.gpkg"
        with pytest.raises(
            UnsupportedResultError, match="not a valid storage identifier"
        ):
            write_layers_to_gpkg(
                layers=[
                    ("voronoi", _geojson_polygon_collection(1), "application/geo+json"),
                    ("bad id", _geojson_polygon_collection(1), "application/geo+json"),
                ],
                output_path=path,
            )
        assert (
            not path.exists()
        )  # all-or-nothing: first valid layer must not land either

    def test_one_empty_collection_aborts_whole_batch(self, tmp_path):
        path = tmp_path / "job-5.gpkg"
        with pytest.raises(UnsupportedResultError, match="empty"):
            write_layers_to_gpkg(
                layers=[
                    ("voronoi", _geojson_polygon_collection(1), "application/geo+json"),
                    ("empty_one", _geojson_empty_collection(), "application/geo+json"),
                ],
                output_path=path,
            )
        assert not path.exists()

    def test_no_temp_file_left_after_success(self, tmp_path):
        path = tmp_path / "job-6.gpkg"
        write_layers_to_gpkg(
            layers=[
                ("voronoi", _geojson_polygon_collection(1), "application/geo+json")
            ],
            output_path=path,
        )
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_no_temp_file_left_after_failure(self, tmp_path):
        path = tmp_path / "job-7.gpkg"
        with pytest.raises(UnsupportedResultError):
            write_layers_to_gpkg(
                layers=[("empty", _geojson_empty_collection(), "application/geo+json")],
                output_path=path,
            )
        assert list(tmp_path.glob(".*.tmp")) == []
