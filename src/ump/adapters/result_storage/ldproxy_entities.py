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

from ump.adapters.result_storage.gpkg_writer import GpkgLayerSchema

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
    - ``primaryKey`` and ``sortKey`` are both ``OBJECTID``, matching the FID
      column that geopandas writes to GeoPackage.
    - ``typeValidation: NONE`` keeps startup permissive during early iteration.
    - The ``id`` pseudo-property maps OBJECTID and is hidden from reads
      (``excludedScopes: [RECEIVABLE]``).
    - The ``geometry`` pseudo-property maps the ``Shape`` column that
      geopandas/pyogrio writes by default.
    """
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
            "primaryKey": "OBJECTID",
            "sortKey": "OBJECTID",
        },
        "queryGeneration": {
            "chunkSize": 10000,
            "computeNumberMatched": True,
        },
        "types": {
            output_id: _build_feature_type(output_id, schema),
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
    """Build a single collection entry to be merged into the service ``collections`` map.

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
    """
    properties: dict = {
        "id": {
            "sourcePath": "OBJECTID",
            "type": "INTEGER",
            "role": "ID",
            "excludedScopes": ["RECEIVABLE"],
        },
        "geometry": {
            "sourcePath": "Shape",
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
