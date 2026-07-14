"""FlatGeobuf ingest helpers for OGC API Processes reference outputs.

This module covers everything needed to get a FlatGeobuf payload from any of
the three OGC output representations into PostGIS via GeoServer:

- ``build_storage_job_id``: builds the output-specific storage key.
- ``try_decode_base64`` / ``extract_flatgeobuf_bytes``: extract raw bytes from
  inline payloads (raw bytes, base64 dict, or bare base64 string).
- ``store_flatgeobuf_ingest``: routes a payload to the correct GeoServer ingest
  path (URL streaming via ogr2ogr vs. temporary-file upload).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ump.geoserver.geoserver import Geoserver

logger = logging.getLogger(__name__)


def build_storage_job_id(job_id: str, output_id: str) -> str:
    """Build the output-specific PostGIS / GeoServer storage key.

    The identifier is used as both the PostGIS table name suffix and the
    GeoServer layer name, so it must be safe for SQL identifiers and URLs.

    Args:
        job_id:    Local job identifier, e.g. ``"job-abc123"``.
        output_id: Output name from the execute request, e.g. ``"dem"``.

    Returns:
        Combined key such as ``"job-abc123-dem"``.
    """
    safe_output = re.sub(r"[^a-zA-Z0-9_-]", "_", output_id)
    return f"{job_id}-{safe_output}"


def try_decode_base64(candidate: str) -> bytes | None:
    """Attempt to base64-decode a string, returning ``None`` on failure.

    Using ``validate=True`` ensures that non-base64 strings (plain GeoJSON,
    URLs, …) are rejected rather than silently producing garbage bytes.

    Args:
        candidate: Potentially base64-encoded string.

    Returns:
        Decoded bytes, or ``None`` if the string is not valid base64.
    """
    try:
        return base64.b64decode(candidate.strip(), validate=True)
    except (ValueError, binascii.Error):
        return None


def extract_flatgeobuf_bytes(output_data: Any) -> bytes | None:
    """Extract raw FlatGeobuf bytes from any of the supported payload forms.

    Supported forms (tried in order):

    1. ``bytes`` / ``bytearray``: returned directly.
    2. ``dict`` with a ``data`` / ``value`` / ``inlineValue`` / ``content``
       key whose value is a base64-encoded string.
    3. Bare base64 string.

    Args:
        output_data: OGC output value.

    Returns:
        FlatGeobuf bytes, or ``None`` if no extractable payload was found.
    """
    # Form 1: raw binary
    if isinstance(output_data, (bytes, bytearray)):
        return bytes(output_data)

    # Form 2: dict with an inline base64 payload under a well-known key
    if isinstance(output_data, dict):
        for key in ("data", "value", "inlineValue", "content"):
            candidate = output_data.get(key)
            if isinstance(candidate, str):
                decoded = try_decode_base64(candidate)
                if decoded is not None:
                    return decoded

    # Form 3: bare base64 string
    if isinstance(output_data, str):
        return try_decode_base64(output_data)

    return None


def store_flatgeobuf_ingest(
    geoserver: "Geoserver",
    storage_job_id: str,
    output_data: Any,
    job_id: str = "",
) -> bool:
    """Route a FlatGeobuf payload to the appropriate GeoServer ingest path.

    Two paths are supported:

    - **URL ingestion** (preferred): when ``output_data`` is a dict with an
      ``href`` field, ogr2ogr streams the remote file directly into PostGIS
      without loading the data into Python memory.
    - **Byte ingestion**: inline bytes or base64 payloads are extracted, written
      to a temporary file and imported via ogr2ogr.

    Args:
        geoserver:       Configured :class:`~ump.geoserver.geoserver.Geoserver`
                         instance.
        storage_job_id:  Output-specific storage key
                         (see :func:`build_storage_job_id`).
        output_data:     OGC output value — dict with ``href``, dict with
                         inline base64, raw ``bytes``, or bare base64 string.
        job_id:          Job identifier used only for log messages.

    Returns:
        ``True`` if ingest succeeded, ``False`` otherwise.
    """
    # Path 1: OGC link object → stream via URL (no download into memory)
    if isinstance(output_data, dict):
        href = output_data.get("href")
        if isinstance(href, str) and href:
            geoserver.save_flatgeobuf_results(storage_job_id, href)
            return True

    # Path 2: inline payload → extract bytes, use temporary file
    payload = extract_flatgeobuf_bytes(output_data)
    if not payload:
        logger.warning(
            "Job %s: could not extract FlatGeobuf payload for reference output",
            job_id,
        )
        return False

    geoserver.save_flatgeobuf_bytes(storage_job_id, payload)
    return True
