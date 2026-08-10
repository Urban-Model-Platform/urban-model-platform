"""Tests for V-4: ldproxy_entities.

Tests run against in-memory dicts — no filesystem or ldproxy required.
The strategy: build an entity dict, round-trip through to_yaml + yaml.safe_load,
and assert the structure.  This catches both wrong field names and serialisation
regressions.
"""

from __future__ import annotations

import pytest
import yaml

from ump.adapters.result_storage.gpkg_writer import GpkgLayerSchema
from ump.adapters.result_storage.ldproxy_entities import (
    DEFAULT_PROVIDER_DATABASE,
    DEFAULT_PROVIDER_TYPE,
    build_collection_block,
    build_default_provider_entity,
    build_provider_entity,
    build_service_skeleton,
    collection_id_for,
    to_yaml,
)

# ---------------------------------------------------------------------------
# Fixture — a minimal schema as returned by gpkg_writer
# ---------------------------------------------------------------------------


@pytest.fixture
def schema():
    return GpkgLayerSchema(
        geometry_type="POLYGON",
        properties={
            "level_cm": "INTEGER",
            "name": "STRING",
            "active": "BOOLEAN",
            "score": "FLOAT",
        },
        feature_count=5,
        crs_epsg=4326,
    )


# ---------------------------------------------------------------------------
# Provider entity
# ---------------------------------------------------------------------------


class TestBuildProviderEntity:
    def _parse(self, entity: dict) -> dict:
        """Round-trip through YAML to catch serialisation issues."""
        return yaml.safe_load(to_yaml(entity))

    def test_top_level_fields(self, schema):
        entity = build_provider_entity("abc-uuid", "flood_zones", schema)
        parsed = self._parse(entity)

        assert parsed["id"] == "abc-uuid"
        assert parsed["enabled"] is True
        assert parsed["providerType"] == "FEATURE"
        assert parsed["providerSubType"] == "SQL"
        assert parsed["typeValidation"] == "NONE"

    def test_native_crs(self, schema):
        entity = build_provider_entity("abc-uuid", "zones", schema, crs_epsg=25832)
        parsed = self._parse(entity)

        assert parsed["nativeCrs"]["code"] == 25832
        assert parsed["nativeCrs"]["forceAxisOrder"] == "LON_LAT"

    def test_connection_info(self, schema):
        entity = build_provider_entity("my-job-uuid", "layer", schema)
        parsed = self._parse(entity)

        ci = parsed["connectionInfo"]
        assert ci["dialect"] == "GPKG"
        assert ci["database"] == "my-job-uuid.gpkg"
        assert ci["pool"]["initFailFast"] is True

    def test_source_path_defaults(self, schema):
        entity = build_provider_entity("uuid", "layer", schema)
        parsed = self._parse(entity)

        assert parsed["sourcePathDefaults"]["primaryKey"] == "fid"
        assert parsed["sourcePathDefaults"]["sortKey"] == "fid"

    def test_feature_type_source_path(self, schema):
        entity = build_provider_entity("uuid", "flood_zones", schema)
        parsed = self._parse(entity)

        feature_type = parsed["types"]["flood_zones"]
        assert feature_type["sourcePath"] == "/flood_zones"

    def test_id_pseudo_property(self, schema):
        entity = build_provider_entity("uuid", "layer", schema)
        parsed = self._parse(entity)

        id_prop = parsed["types"]["layer"]["properties"]["id"]
        assert id_prop["sourcePath"] == "fid"
        assert id_prop["role"] == "ID"
        assert "RECEIVABLE" in id_prop["excludedScopes"]

    def test_geometry_pseudo_property(self, schema):
        entity = build_provider_entity("uuid", "layer", schema)
        parsed = self._parse(entity)

        geom = parsed["types"]["layer"]["properties"]["geometry"]
        assert geom["sourcePath"] == "geom"
        assert geom["role"] == "PRIMARY_GEOMETRY"
        assert geom["geometryType"] == "POLYGON"  # from fixture schema

    def test_data_properties_present(self, schema):
        entity = build_provider_entity("uuid", "layer", schema)
        parsed = self._parse(entity)

        props = parsed["types"]["layer"]["properties"]
        assert props["level_cm"]["type"] == "INTEGER"
        assert props["name"]["type"] == "STRING"
        assert props["active"]["type"] == "BOOLEAN"
        assert props["score"]["type"] == "FLOAT"

    def test_data_property_source_path_matches_name(self, schema):
        entity = build_provider_entity("uuid", "layer", schema)
        parsed = self._parse(entity)

        props = parsed["types"]["layer"]["properties"]
        for prop_name in ["level_cm", "name", "active", "score"]:
            assert props[prop_name]["sourcePath"] == prop_name

    def test_mixed_geometry_type(self):
        mixed_schema = GpkgLayerSchema(
            geometry_type="GEOMETRY",
            properties={"value": "INTEGER"},
            feature_count=3,
            crs_epsg=4326,
        )
        entity = build_provider_entity("uuid", "layer", mixed_schema)
        parsed = self._parse(entity)

        geom = parsed["types"]["layer"]["properties"]["geometry"]
        assert geom["geometryType"] == "GEOMETRY"


# ---------------------------------------------------------------------------
# Service skeleton
# ---------------------------------------------------------------------------


class TestBuildServiceSkeleton:
    def test_top_level_fields(self):
        skeleton = build_service_skeleton("ump-results")
        assert skeleton["id"] == "ump-results"
        assert skeleton["serviceType"] == "OGC_API"
        assert skeleton["enabled"] is True

    def test_collections_starts_empty(self):
        skeleton = build_service_skeleton()
        assert skeleton["collections"] == {}

    def test_api_building_blocks_present(self):
        skeleton = build_service_skeleton()
        block_names = {b["buildingBlock"] for b in skeleton["api"]}
        assert "SCHEMA" in block_names
        assert "FILTER" in block_names
        assert "QUERYABLES" in block_names
        assert "FLATGEOBUF" in block_names
        assert "CSV" in block_names
        assert "CRS" in block_names

    def test_yaml_roundtrip(self):
        skeleton = build_service_skeleton()
        parsed = yaml.safe_load(to_yaml(skeleton))
        assert parsed["collections"] == {}
        assert parsed["serviceType"] == "OGC_API"


# ---------------------------------------------------------------------------
# Default provider (required by ldproxy 3.x for the shared service to start)
# ---------------------------------------------------------------------------


class TestBuildDefaultProviderEntity:
    def _parse(self, entity: dict) -> dict:
        return yaml.safe_load(to_yaml(entity))

    def test_id_equals_service_id(self):
        parsed = self._parse(build_default_provider_entity("ump-results"))
        # ldproxy resolves the service's default provider by matching ids.
        assert parsed["id"] == "ump-results"

    def test_backed_by_seed_gpkg(self):
        parsed = self._parse(build_default_provider_entity())
        assert parsed["connectionInfo"]["database"] == DEFAULT_PROVIDER_DATABASE
        assert parsed["connectionInfo"]["dialect"] == "GPKG"

    def test_uses_fid_geom_columns(self):
        parsed = self._parse(build_default_provider_entity())
        assert parsed["sourcePathDefaults"]["primaryKey"] == "fid"
        typ = parsed["types"][DEFAULT_PROVIDER_TYPE]
        assert typ["properties"]["id"]["sourcePath"] == "fid"
        assert typ["properties"]["geometry"]["sourcePath"] == "geom"

    def test_single_default_type(self):
        parsed = self._parse(build_default_provider_entity())
        # Exactly one type, never registered as a collection, so it stays
        # invisible under /collections.
        assert list(parsed["types"].keys()) == [DEFAULT_PROVIDER_TYPE]

    def test_custom_service_id_and_crs(self):
        parsed = self._parse(build_default_provider_entity("custom-svc", 3857))
        assert parsed["id"] == "custom-svc"
        assert parsed["nativeCrs"]["code"] == 3857


# ---------------------------------------------------------------------------
# Collection block
# ---------------------------------------------------------------------------


class TestBuildCollectionBlock:
    def test_structure(self):
        block = build_collection_block("coll-id", "job-uuid", "flood_zones")
        assert "coll-id" in block
        entry = block["coll-id"]
        assert entry["id"] == "coll-id"
        assert entry["enabled"] is True

    def test_label_uses_output_id_not_collection_id(self):
        block = build_collection_block(
            "job-uuid-flood_zones", "job-uuid", "flood_zones"
        )
        # Label should be the readable output name, not the full collection id
        assert block["job-uuid-flood_zones"]["label"] == "flood_zones"

    def test_features_core_binding(self):
        block = build_collection_block("coll-id", "job-uuid", "flood_zones")
        api = block["coll-id"]["api"]
        core = next(b for b in api if b["buildingBlock"] == "FEATURES_CORE")
        assert core["featureProvider"] == "job-uuid"
        assert core["featureType"] == "flood_zones"
        assert core["itemType"] == "feature"

    def test_merge_into_service(self):
        """Collection block can be merged directly into the service collections map."""
        service = build_service_skeleton()
        service["collections"].update(
            build_collection_block("coll-a", "job-a", "zones")
        )
        assert "coll-a" in service["collections"]

    def test_yaml_roundtrip(self):
        block = build_collection_block("coll-id", "job-uuid", "layer")
        parsed = yaml.safe_load(to_yaml(block))
        assert parsed["coll-id"]["api"][0]["featureProvider"] == "job-uuid"


# ---------------------------------------------------------------------------
# collection_id_for helper
# ---------------------------------------------------------------------------


class TestCollectionIdFor:
    def test_always_combines_uuid_and_output(self):
        cid = collection_id_for("job-uuid", "flood_zones")
        assert cid == "job-uuid-flood_zones"

    def test_separator_is_hyphen(self):
        cid = collection_id_for("abc", "voronoi_diagram")
        assert cid == "abc-voronoi_diagram"


# ---------------------------------------------------------------------------
# to_yaml
# ---------------------------------------------------------------------------


class TestToYaml:
    def test_block_style(self):
        yml = to_yaml({"a": {"b": 1}})
        assert "{" not in yml  # no flow style

    def test_no_python_tags(self):
        yml = to_yaml({"key": True})
        assert "!!" not in yml

    def test_preserves_insertion_order(self):
        data = {"z": 1, "a": 2, "m": 3}
        yml = to_yaml(data)
        keys = [line.split(":")[0].strip() for line in yml.splitlines() if ":" in line]
        assert keys == ["z", "a", "m"]
