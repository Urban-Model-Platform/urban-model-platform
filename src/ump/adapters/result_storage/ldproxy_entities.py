"""ldproxy entity builders for the result storage adapter.

Every stored job needs two ldproxy entity files:

  provider  — tells ldproxy *how to read* the GeoPackage (connection info,
               layer/table layout, property types, CRS).
  service   — tells ldproxy *what to publish* (API building blocks, which
               collections exist, and which provider backs each one).

Rather than one monolithic function, the builders are split into small
composable pieces that return plain Python dicts.  The caller serialises them
to YAML with ``to_yaml()`` and writes them with ``atomic_fs``.  This keeps
each piece independently testable without touching the filesystem.

Public API
----------
``build_provider_entity(job_uuid, output_id, schema, crs_epsg) -> dict``
    Full provider entity dict for ``{job_uuid}.yml``.

``build_service_skeleton(service_id) -> dict``
    Skeleton service entity with empty ``collections`` map.  Written once when
    the shared ``ump-results.yml`` does not yet exist.

``build_collection_block(collection_id, job_uuid, output_id) -> dict``
    Single-key dict ``{collection_id: {...}}`` ready to be merged into the
    service entity's ``collections`` map.

``collection_id_for(job_uuid, output_id) -> str``
    Canonical collection ID: ``{job_uuid}-{output_id}``.

``to_yaml(data) -> str``
    Serialise a dict to a YAML string with consistent settings (block style,
    preserved insertion order, no !! type tags).
"""

from __future__ import annotations

import yaml

from ump.adapters.result_storage.gpkg_writer import GpkgLayerSchema, validate_output_id
from ump.core.interfaces.result_storage import UnsupportedResultError

# ---------------------------------------------------------------------------
# Public builders — all return plain dicts
# ---------------------------------------------------------------------------


def build_provider_entity(
    job_uuid: str,
    output_id: str,
    schema: GpkgLayerSchema,
    crs_epsg: int = 4326,
) -> dict:
    """Build the ldproxy GPKG feature provider entity for one stored job.

    The resulting dict is written to
    ``{root}/entities/instances/providers/{job_uuid}.yml``.

    Field names follow the ldproxy documented example exactly.  Key decisions:

    - ``database`` is ``{job_uuid}.gpkg`` — ldproxy resolves this relative to
      its configured ``resources/features/`` directory.
    - ``primaryKey`` and ``sortKey`` are both ``fid``, the integer primary-key
      column that pyogrio/GDAL writes to every GeoPackage layer by default.
    - ``typeValidation: NONE`` keeps startup permissive during early iteration.
    - The ``id`` pseudo-property maps ``fid`` and is hidden from reads
      (``excludedScopes: [RECEIVABLE]``).
    - The ``geometry`` pseudo-property maps the ``geom`` column that
      pyogrio/GDAL writes to GeoPackage by default.
    """
    validate_output_id(output_id)
    return build_provider_entity_multi(job_uuid, {output_id: schema}, crs_epsg)


def build_provider_entity_multi(
    job_uuid: str,
    schemas: dict[str, GpkgLayerSchema],
    crs_epsg: int = 4326,
) -> dict:
    """Build one provider entity carrying one ``types`` entry per output.

    A job can produce several storable outputs, all backed by a single
    GeoPackage (one layer per output — see ``write_layers_to_gpkg``). The
    ldproxy model is correspondingly *one provider per job* with one
    ``types.{output_id}`` block per output, so this is the multi-output
    counterpart to ``build_provider_entity`` (which delegates here for the
    single-output case, keeping one source of truth for the entity shape).

    Args:
        job_uuid: The job UUID — the provider ``id`` and ``{job_uuid}.gpkg``.
        schemas:  Mapping of ``output_id`` → ``GpkgLayerSchema``, exactly what
                  ``write_layers_to_gpkg`` returns. Insertion order is
                  preserved so the YAML ``types`` order matches the layer order.
        crs_epsg: Native CRS EPSG code for all layers.

    Raises:
        UnsupportedResultError: if ``schemas`` is empty or any ``output_id`` is
                                not a valid storage identifier.
    """
    if not schemas:
        raise UnsupportedResultError(
            "build_provider_entity_multi: at least one output schema is required"
        )
    for output_id in schemas:
        validate_output_id(output_id)

    return {
        "id": job_uuid,
        "enabled": True,
        "entityStorageVersion": 2,
        "providerType": "FEATURE",
        "providerSubType": "SQL",
        "nativeCrs": {
            "code": crs_epsg,
            "forceAxisOrder": "LON_LAT",
        },
        "typeValidation": "NONE",
        "connectionInfo": {
            "dialect": "GPKG",
            "database": f"{job_uuid}.gpkg",
            "pool": {
                "maxConnections": -1,
                "minConnections": 1,
                "initFailFast": True,
                "idleTimeout": "10m",
                "shared": False,
            },
        },
        "sourcePathDefaults": {
            "primaryKey": "fid",
            "sortKey": "fid",
        },
        "queryGeneration": {
            "chunkSize": 10000,
            "computeNumberMatched": True,
        },
        "types": {
            output_id: _build_feature_type(output_id, schema)
            for output_id, schema in schemas.items()
        },
    }


# Filename (relative to resources/features/) of the seed GeoPackage that backs
# the default provider, and the single type it exposes. Kept in sync with
# gpkg_writer.DEFAULT_SEED_LAYER.
DEFAULT_PROVIDER_DATABASE = "__ump_default__.gpkg"
DEFAULT_PROVIDER_TYPE = "default"


def build_default_provider_entity(
    service_id: str = "ump-results", crs_epsg: int = 4326
) -> dict:
    """Build the *default* feature provider entity the shared service requires.

    ldproxy (verified on 3.6.x and 4.6.x) fails to start an ``OGC_API`` service
    unless it can resolve a default feature provider whose id equals the service
    id — even when every published collection overrides ``featureProvider``
    per-collection. The
    provider id therefore equals *service_id*, and it is backed by a tiny seed
    GeoPackage (see ``gpkg_writer.write_seed_gpkg``) exposing a single
    ``default`` type. That type is never registered as a collection, so it
    stays invisible under ``/collections`` while satisfying the requirement.

    The column names (``fid`` primary key, ``geom`` geometry) match exactly
    what pyogrio/GDAL write, identical to the per-job provider entities.
    """
    return {
        "id": service_id,
        "enabled": True,
        "entityStorageVersion": 2,
        "providerType": "FEATURE",
        "providerSubType": "SQL",
        "nativeCrs": {"code": crs_epsg, "forceAxisOrder": "LON_LAT"},
        "typeValidation": "NONE",
        "connectionInfo": {
            "dialect": "GPKG",
            "database": DEFAULT_PROVIDER_DATABASE,
            "pool": {
                "maxConnections": -1,
                "minConnections": 1,
                "initFailFast": True,
                "idleTimeout": "10m",
                "shared": False,
            },
        },
        "sourcePathDefaults": {"primaryKey": "fid", "sortKey": "fid"},
        "types": {
            DEFAULT_PROVIDER_TYPE: {
                "sourcePath": f"/{DEFAULT_PROVIDER_TYPE}",
                "properties": {
                    "id": {
                        "sourcePath": "fid",
                        "type": "INTEGER",
                        "role": "ID",
                        "excludedScopes": ["RECEIVABLE"],
                    },
                    "geometry": {
                        "sourcePath": "geom",
                        "type": "GEOMETRY",
                        "role": "PRIMARY_GEOMETRY",
                        "geometryType": "POINT",
                    },
                    "note": {"sourcePath": "note", "type": "STRING"},
                },
            }
        },
    }


def build_service_skeleton(service_id: str = "ump-results") -> dict:
    """Build the initial shared service entity with an empty collections map.

    Written once to ``{root}/entities/instances/services/ump-results.yml``
    when the file does not yet exist.  Subsequent jobs only add entries to
    the ``collections`` key via the service registry.

    Building blocks enabled:
      SCHEMA, QUERYABLES, FILTER, FLATGEOBUF, CSV, PROJECTIONS, CRS
    These give clients filtering, subsetting, and alternative output formats —
    the core motivation for result storage.  TILES is omitted (needs a
    separate tile provider).
    """
    return {
        "id": service_id,
        "label": service_id,
        "enabled": True,
        "serviceType": "OGC_API",
        "apiValidation": "NONE",
        "api": [
            {"buildingBlock": "SCHEMA", "enabled": True},
            {"buildingBlock": "QUERYABLES", "enabled": True, "included": ["*"]},
            {"buildingBlock": "FILTER", "enabled": True},
            {"buildingBlock": "FLATGEOBUF", "enabled": True},
            {"buildingBlock": "CSV", "enabled": True},
            {"buildingBlock": "PROJECTIONS", "enabled": True},
            {
                "buildingBlock": "CRS",
                "additionalCrs": [
                    {"code": 4326, "forceAxisOrder": "NONE"},
                    {"code": 3857, "forceAxisOrder": "NONE"},
                ],
            },
        ],
        "collections": {},
    }


def build_collection_block(
    collection_id: str,
    job_uuid: str,
    output_id: str,
) -> dict:
    """Build a single collection entry for the service ``collections`` map.

    Returns a one-key dict ``{collection_id: {...}}`` so the caller can do::

        service["collections"].update(build_collection_block(...))

    The ``featureProvider`` is the job UUID (the provider entity ID).
    The ``featureType`` is the output ID (the type key inside the provider).
    The label uses the human-readable output name (e.g. "voronoi_diagram"),
    not the full collection ID, so the ldproxy UI stays readable.
    """
    return {
        collection_id: {
            "id": collection_id,
            "label": output_id,
            "enabled": True,
            "api": [
                {
                    "buildingBlock": "FEATURES_CORE",
                    "enabled": True,
                    "featureProvider": job_uuid,
                    "featureType": output_id,
                    "itemType": "feature",
                }
            ],
        }
    }


def collection_id_for(job_uuid: str, output_id: str) -> str:
    """Return the canonical ldproxy collection ID for one stored output.

    Pattern: ``{job_uuid}-{output_id}``

    The UUID prefix makes every collection ID globally unguessable within the
    shared ``ump-results`` service (the only access control in v1).  The
    output name suffix makes the ID self-descriptive in ldproxy's UI and logs.
    """
    validate_output_id(output_id)
    return f"{job_uuid}-{output_id}"


def to_yaml(data: dict) -> str:
    """Serialise *data* to a YAML string with consistent ldproxy-friendly settings.

    Settings:
      - block style (``default_flow_style=False``) — human-readable, line-per-key.
      - insertion order preserved (``sort_keys=False``) — fields appear in the
        order the builders define them, matching the documented examples.
      - no ``!!python/…`` type tags (``Dumper=yaml.SafeDumper``) — ldproxy
        does not understand Python-specific YAML extensions.
      - Unicode passed through (``allow_unicode=True``) — names and labels
        may contain non-ASCII characters.
    """
    return yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        Dumper=yaml.SafeDumper,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_feature_type(output_id: str, schema: GpkgLayerSchema) -> dict:
    """Build the ``types.{output_id}`` section of the provider entity.

    The fixed pseudo-properties ``id`` and ``geometry`` always come first,
    followed by the data properties in schema insertion order.

    Reserved-name collisions and the choice of ID column are resolved upstream
    in ``gpkg_writer._sanitize_and_resolve_id`` — by the time the schema reaches
    here, ``schema.id_source_path`` names the GeoPackage column that backs the
    ID role, ``schema.properties`` never contains that column or any reserved
    name, and the active geometry is always the physical ``geom`` column. This
    builder therefore just wires those decisions into the entity shape.
    """
    id_property: dict = {
        "sourcePath": schema.id_source_path,
        "type": schema.id_type,
        "role": "ID",
    }
    # The synthetic GeoPackage primary key is internal plumbing, never client
    # input; a promoted real ``id`` column carries no such restriction.
    if schema.id_source_path == "fid":
        id_property["excludedScopes"] = ["RECEIVABLE"]

    properties: dict = {
        "id": id_property,
        "geometry": {
            "sourcePath": "geom",
            "type": "GEOMETRY",
            "role": "PRIMARY_GEOMETRY",
            "geometryType": schema.geometry_type,
        },
    }

    for prop_name, ldproxy_type in schema.properties.items():
        properties[prop_name] = {
            "sourcePath": prop_name,
            "type": ldproxy_type,
        }

    return {
        "sourcePath": f"/{output_id}",
        "properties": properties,
    }
