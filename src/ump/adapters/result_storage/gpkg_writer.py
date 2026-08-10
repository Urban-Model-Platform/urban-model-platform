"""GeoPackage writer for the ldproxy result storage adapter.

Responsibility: take raw bytes in a supported geo-feature format and write
them to a GeoPackage file on disk.  Also derive the layer schema (geometry
type + property types) that the ldproxy provider YAML builder (V-4) needs.

Supported input formats
-----------------------
application/geo+json    GeoJSON FeatureCollection — geopandas reads natively.
application/flatgeobuf  FlatGeobuf FeatureCollection — pyogrio reads natively.

Any other media_type raises ``UnsupportedResultError``.

Why GeoPackage?
---------------
ldproxy supports GeoPackage as a native GPKG provider backend.  Every stored
result gets its own ``.gpkg`` file and its own ldproxy provider entity.
Using one file per job keeps the store simple to clean up and avoids
table-name collisions inside a shared database.

Why this module is separate from the ldproxy adapter
----------------------------------------------------
The conversion from raw bytes → GeoPackage is a pure, independently testable
operation.  It has no knowledge of ldproxy entity files, ConfigMaps, or the
ldproxy URL.  Tests can run it over a temp directory with no ldproxy installed.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from ump.adapters.result_storage.atomic_fs import atomic_write_path
from ump.core.interfaces.result_storage import UnsupportedResultError

logger = logging.getLogger(__name__)

# Media types this writer can convert to GeoPackage.
# The key is the normalised IANA type; the value is the driver name passed to
# geopandas/pyogrio when reading.
SUPPORTED_MEDIA_TYPES: dict[str, str] = {
    "application/geo+json": "GeoJSON",
    "application/flatgeobuf": "FlatGeobuf",
}

# Mapping from pandas dtype kinds to ldproxy property type strings.
# Used by the schema derivation step so V-4 (ldproxy_entities) can emit
# correct YAML without re-reading the GeoPackage.
_DTYPE_TO_LDPROXY: dict[str, str] = {
    "i": "INTEGER",  # signed integer (int8 … int64)
    "u": "INTEGER",  # unsigned integer
    "f": "FLOAT",  # float32, float64
    "b": "BOOLEAN",  # bool
    "M": "DATETIME",  # datetime64
    "O": "STRING",  # object (string, mixed — treat as STRING)
    "S": "STRING",  # bytes string (rare)
    "U": "STRING",  # unicode string
}

# Mapping from Shapely geometry type names to ldproxy geometryType strings.
_GEOM_TYPE_TO_LDPROXY: dict[str, str] = {
    "Point": "POINT",
    "MultiPoint": "MULTI_POINT",
    "LineString": "LINE_STRING",
    "MultiLineString": "MULTI_LINE_STRING",
    "Polygon": "POLYGON",
    "MultiPolygon": "MULTI_POLYGON",
    "GeometryCollection": "GEOMETRY",
}

# An output_id becomes, in order: a GeoPackage layer name (effectively a SQL
# table name), an ldproxy provider `types` key, a `sourcePath` segment, and
# half of a collection id (`{job_uuid}-{output_id}`, see ldproxy_entities).
# Every one of those contexts is safe for a simple identifier — letters,
# digits, underscore, not starting with a digit — so we validate once, here,
# at the narrowest point, rather than trusting the remote server's output
# naming or defensively re-checking it in every consumer.
_OUTPUT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def validate_output_id(output_id: str) -> None:
    """Raise ``UnsupportedResultError`` if *output_id* is not a safe identifier.

    A malformed output_id (empty, containing ``/``, ``.``, spaces, etc.) would
    otherwise corrupt the GeoPackage layer name, the ldproxy provider's
    ``types`` key/``sourcePath``, and the collection id derived from it. This
    is treated the same as any other unsupported-result condition: the
    ResultStorageCoordinator falls back to the inline value under
    ``emulate-ref`` or surfaces a 502 under ``emulate-ref-only`` — the
    computation still succeeded, only this output cannot be stored.
    """
    if not _OUTPUT_ID_RE.match(output_id):
        raise UnsupportedResultError(
            f"Output id {output_id!r} is not a valid storage identifier "
            "(must start with a letter and contain only letters, digits, "
            "or underscores) — cannot use it as a GeoPackage layer name."
        )


@dataclass(frozen=True)
class GpkgLayerSchema:
    """Describes the schema of one layer inside a written GeoPackage.

    Used by V-4 (ldproxy_entities) to generate the provider YAML without
    having to re-open the GeoPackage.

    Attributes:
        geometry_type: ldproxy geometry type string, e.g. ``"MULTI_POLYGON"``.
                       ``"GEOMETRY"`` is used for mixed or unknown types.
        properties:    Mapping of property name → ldproxy type string.
                       Does not include the ``id`` or ``geometry`` pseudo-properties
                       (those are handled separately in the provider YAML builder).
        feature_count: Number of features written.  Zero means the
                       FeatureCollection was empty; the caller should raise or
                       skip storage accordingly.
        crs_epsg:      EPSG code of the CRS the GeoDataFrame was written in.
    """

    geometry_type: str
    properties: dict[str, str]
    feature_count: int
    crs_epsg: int


def write_to_gpkg(
    body_bytes: bytes,
    media_type: str,
    layer_name: str,
    output_path: Path,
    target_crs_epsg: int = 4326,
) -> GpkgLayerSchema:
    """Convert *body_bytes* to a GeoPackage file and return the layer schema.

    This is the main entry point for the adapter.  It does four things:

    1. Validate that the media_type is supported.
    2. Read the bytes into a GeoDataFrame via geopandas/pyogrio.
    3. Reproject to *target_crs_epsg* if the source CRS differs.
    4. Write the GeoDataFrame to *output_path* atomically (temp-file + rename).

    Args:
        body_bytes:      Raw feature data in a supported format.
        media_type:      IANA media type of *body_bytes*
                         (e.g. ``application/geo+json``).
        layer_name:      Name of the layer inside the GeoPackage.  Also used as
                         the ldproxy ``featureType`` identifier.
        output_path:     Destination ``.gpkg`` file path.  The parent directory
                         must already exist.  Written atomically.
        target_crs_epsg: EPSG code to reproject to if necessary.
                         Defaults to 4326 (WGS84) because OGC GeoJSON is WGS84
                         by RFC 7946 and the ldproxy provider defaults to 4326.

    Returns:
        A ``GpkgLayerSchema`` describing the written layer.

    Raises:
        UnsupportedResultError: if *media_type* is not in the supported list,
                                or if the FeatureCollection is empty.
        ResultStorageError:     if the GeoDataFrame cannot be read or the file
                                cannot be written (re-raised from underlying I/O).
    """
    normalised_type = _normalise_media_type(media_type)
    driver = _require_supported_type(normalised_type)

    gdf = _read_geodataframe(body_bytes, driver, layer_name)

    if gdf.empty:
        raise UnsupportedResultError(
            f"Layer '{layer_name}': FeatureCollection contains no features. "
            "An empty GeoPackage cannot be registered with ldproxy."
        )

    gdf = _ensure_crs(gdf, target_crs_epsg, layer_name)

    schema = _derive_schema(gdf, target_crs_epsg)

    _write_gpkg(gdf, layer_name, output_path)

    logger.debug(
        "[gpkg] wrote %d features to %s (layer=%s geometry=%s)",
        schema.feature_count,
        output_path.name,
        layer_name,
        schema.geometry_type,
    )
    return schema


def write_layers_to_gpkg(
    layers: list[tuple[str, bytes, str]],
    output_path: Path,
    target_crs_epsg: int = 4326,
) -> dict[str, GpkgLayerSchema]:
    """Write multiple outputs of the *same job* as separate layers in one GeoPackage.

    A job can produce several storable outputs (e.g. ``voronoi_diagram`` and
    ``buffer_zones``). The ldproxy entity model (V-4) is one provider per job
    with one ``types`` entry per output, backed by one GeoPackage with one
    layer per output — so multiple outputs must land in a single file, not one
    file each. This is the multi-output counterpart to ``write_to_gpkg``.

    Args:
        layers:          One ``(output_id, body_bytes, media_type)`` tuple per
                         output to store. ``output_id`` becomes the GeoPackage
                         layer name — see ``validate_output_id``.
        output_path:     Destination ``.gpkg`` file path. The parent directory
                         must already exist. Written atomically: either every
                         layer ends up in the file, or none does.
        target_crs_epsg: EPSG code every layer is reprojected to if necessary.

    Returns:
        A ``dict`` mapping each ``output_id`` to its ``GpkgLayerSchema``, so
        the caller can build one provider ``types`` entry per output without
        re-opening the file.

    Raises:
        UnsupportedResultError: if *layers* is empty, an ``output_id`` is not a
                                valid identifier, a ``media_type`` is
                                unsupported, or a FeatureCollection is empty.
        ResultStorageError:     if a GeoDataFrame cannot be read.

    All inputs are validated and parsed *before* anything is written, so a
    problem with the third output never leaves a partial file containing the
    first two — the same all-or-nothing guarantee ``write_to_gpkg`` gives for
    a single layer, extended to the whole batch.
    """
    if not layers:
        raise UnsupportedResultError(
            "write_layers_to_gpkg: at least one layer is required"
        )

    schemas: dict[str, GpkgLayerSchema] = {}
    prepared: list[tuple[str, gpd.GeoDataFrame]] = []

    for output_id, body_bytes, media_type in layers:
        validate_output_id(output_id)
        normalised_type = _normalise_media_type(media_type)
        driver = _require_supported_type(normalised_type)

        gdf = _read_geodataframe(body_bytes, driver, output_id)
        if gdf.empty:
            raise UnsupportedResultError(
                f"Layer '{output_id}': FeatureCollection contains no features. "
                "An empty GeoPackage cannot be registered with ldproxy."
            )
        gdf = _ensure_crs(gdf, target_crs_epsg, output_id)
        schemas[output_id] = _derive_schema(gdf, target_crs_epsg)
        prepared.append((output_id, gdf))

    _write_gpkg_layers(prepared, output_path)

    logger.debug(
        "[gpkg] wrote %d layer(s) to %s (layers=%s)",
        len(prepared),
        output_path.name,
        ", ".join(output_id for output_id, _ in prepared),
    )
    return schemas


# ---------------------------------------------------------------------------
# Internal steps — each does one thing and is independently testable
# ---------------------------------------------------------------------------


def _normalise_media_type(media_type: str) -> str:
    """Strip parameters (e.g. ``; charset=utf-8``) and lower-case."""
    return media_type.split(";")[0].strip().lower()


def _require_supported_type(normalised_type: str) -> str:
    """Return the driver name for *normalised_type*, or raise UnsupportedResultError."""
    driver = SUPPORTED_MEDIA_TYPES.get(normalised_type)
    if driver is None:
        supported = ", ".join(sorted(SUPPORTED_MEDIA_TYPES))
        raise UnsupportedResultError(
            f"Media type '{normalised_type}' is not supported for GeoPackage "
            f"conversion.  Supported types: {supported}."
        )
    return driver


def _read_geodataframe(
    body_bytes: bytes, driver: str, layer_name: str
) -> gpd.GeoDataFrame:
    """Parse *body_bytes* into a GeoDataFrame using pyogrio as the I/O engine.

    pyogrio auto-detects the format from the content's magic bytes, so we do
    not pass the driver explicitly (pyogrio emits a warning if we do).
    """
    try:
        gdf = gpd.read_file(io.BytesIO(body_bytes), engine="pyogrio")
    except Exception as exc:
        from ump.core.interfaces.result_storage import ResultStorageError

        raise ResultStorageError(
            f"Layer '{layer_name}': could not parse {driver} bytes: {exc}"
        ) from exc
    return gdf


def _ensure_crs(
    gdf: gpd.GeoDataFrame,
    target_epsg: int,
    layer_name: str,
) -> gpd.GeoDataFrame:
    """Reproject *gdf* to *target_epsg* if necessary.

    OGC GeoJSON is always WGS84 by RFC 7946 so reprojection is a no-op for
    the common case.  FlatGeobuf may carry an explicit CRS that differs.
    If the GeoDataFrame has no CRS we assume WGS84 (the RFC 7946 default).
    """
    if gdf.crs is None:
        logger.debug(
            "[gpkg] layer '%s' has no CRS — assuming EPSG:%d (RFC 7946 default)",
            layer_name,
            target_epsg,
        )
        return gdf.set_crs(epsg=target_epsg)

    if gdf.crs.to_epsg() == target_epsg:
        return gdf  # already correct

    logger.debug(
        "[gpkg] layer '%s' reprojecting %s → EPSG:%d",
        layer_name,
        gdf.crs.to_string(),
        target_epsg,
    )
    return gdf.to_crs(epsg=target_epsg)


def _derive_schema(gdf: gpd.GeoDataFrame, crs_epsg: int) -> GpkgLayerSchema:
    """Inspect *gdf* and return the schema needed for ldproxy provider YAML.

    Geometry type: determined by the unique Shapely geometry type names present
    in the geometry column.  Mixed types collapse to ``"GEOMETRY"``.

    Property types: each non-geometry column is mapped to an ldproxy type string
    via ``_DTYPE_TO_LDPROXY``.  Columns whose dtype kind is not in the map
    default to ``"STRING"`` (safe / human-readable fallback).
    """
    geometry_type = _detect_geometry_type(gdf)
    properties = _detect_property_types(gdf)
    return GpkgLayerSchema(
        geometry_type=geometry_type,
        properties=properties,
        feature_count=len(gdf),
        crs_epsg=crs_epsg,
    )


def _detect_geometry_type(gdf: gpd.GeoDataFrame) -> str:
    """Return the ldproxy geometry type string for the features in *gdf*."""
    unique_types = set(
        geom.geom_type
        for geom in gdf.geometry
        if geom is not None and not _is_null(geom)
    )

    if len(unique_types) == 1:
        geom_type_name = next(iter(unique_types))
        return _GEOM_TYPE_TO_LDPROXY.get(geom_type_name, "GEOMETRY")

    # Zero non-null geometries or mixed types → generic GEOMETRY
    return "GEOMETRY"


def _detect_property_types(gdf: gpd.GeoDataFrame) -> dict[str, str]:
    """Map non-geometry column names to ldproxy type strings."""
    properties: dict[str, str] = {}
    for col in gdf.columns:
        if col == gdf.geometry.name:
            continue  # handled separately in the provider YAML
        dtype = gdf[col].dtype
        ldproxy_type = _DTYPE_TO_LDPROXY.get(dtype.kind, "STRING")
        properties[col] = ldproxy_type
    return properties


def _write_gpkg(gdf: gpd.GeoDataFrame, layer_name: str, output_path: Path) -> None:
    """Write *gdf* to *output_path* as a GeoPackage layer, atomically."""
    with atomic_write_path(output_path) as tmp:
        gdf.to_file(str(tmp), driver="GPKG", layer=layer_name, engine="pyogrio")


# Layer name inside the seed GeoPackage that backs the ldproxy default provider.
DEFAULT_SEED_LAYER = "default"


def write_seed_gpkg(output_path: Path, target_crs_epsg: int = 4326) -> None:
    """Write the minimal 1-feature GeoPackage that backs the ldproxy default provider.

    ldproxy (verified on 3.6.x and 4.6.x) will not start an ``OGC_API`` service
    unless it can resolve a *default* feature provider whose id equals the
    service id — even when every published collection overrides
    ``featureProvider`` per-collection (see
    ``ldproxy_entities.build_default_provider_entity``). That default provider
    is a GPKG provider, and ldproxy validates the backing file exists at
    startup (``initFailFast``), so a real, connectable file must be present.

    This writes a tiny single-point layer named ``DEFAULT_SEED_LAYER``. It is
    never registered as a collection, so it stays invisible under
    ``/collections`` while satisfying the startup requirement. The columns
    (``fid`` primary key, ``geom`` geometry) are exactly what pyogrio/GDAL
    emit, matching the entity the default provider declares.
    """
    gdf = gpd.GeoDataFrame(
        {"note": ["ump default provider seed"]},
        geometry=gpd.points_from_xy([0.0], [0.0]),
        crs=f"EPSG:{target_crs_epsg}",
    )
    _write_gpkg(gdf, DEFAULT_SEED_LAYER, output_path)


def _write_gpkg_layers(
    prepared: list[tuple[str, gpd.GeoDataFrame]], output_path: Path
) -> None:
    """Write each ``(layer_name, gdf)`` pair into one GeoPackage, atomically.

    The first layer creates the file (default ``to_file`` mode); every
    subsequent layer is appended via ``mode="a"`` — GPKG is a SQLite container
    that natively supports multiple tables/layers in one file. All writes
    happen inside the same ``atomic_write_path`` temp file, so a crash
    mid-batch leaves no file at the destination at all, never a half-written
    one with only some layers.
    """
    with atomic_write_path(output_path) as tmp:
        for index, (layer_name, gdf) in enumerate(prepared):
            mode = "w" if index == 0 else "a"
            gdf.to_file(
                str(tmp), driver="GPKG", layer=layer_name, engine="pyogrio", mode=mode
            )


def _is_null(geom: object) -> bool:
    """Return True if *geom* is a null/None/NaN geometry value."""
    try:
        import math

        if isinstance(geom, float) and math.isnan(geom):
            return True
    except TypeError:
        pass
    return geom is None
