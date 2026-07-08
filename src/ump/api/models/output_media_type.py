"""Output media-type detection for OGC API Processes results.

Centralises the constants and detection heuristic for OGC process output
values.  Keeping them here rather than on the ``Job`` model means other
components (routes, serialisers, tests) can import them without pulling in
the full ``Job`` class.
"""

from __future__ import annotations

from typing import Any

# Frozen sets for O(1) membership tests.
GEOJSON_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/geo+json",
        "application/json",
    }
)

FLATGEOBUF_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.flatgeobuf",
        "application/x-flatgeobuf",
    }
)


def detect_output_media_type(output_data: Any) -> str:
    """Detect the media type of an OGC API Processes output value.

    Detection priority:
    1. Explicit ``type`` / ``mediaType`` / ``mimeType`` field in the payload.
    2. File extension of an ``href`` link.
    3. Structural inference (FeatureCollection ``features`` key).
    4. Python ``bytes`` / ``bytearray`` → binary FlatGeobuf.
    5. JSON-looking string → GeoJSON.
    6. Fallback: ``"application/json"``.

    Args:
        output_data: Raw output value from an OGC results document.

    Returns:
        Lowercase media-type string without parameters, e.g.
        ``"application/geo+json"``.
    """
    if isinstance(output_data, dict):
        # 1. Explicit media-type field
        for key in ("type", "mediaType", "mimeType"):
            value = output_data.get(key)
            if isinstance(value, str) and "/" in value:
                return value.split(";")[0].strip().lower()

        # 2. Extension of an href link
        href = output_data.get("href")
        if isinstance(href, str):
            href_lower = href.lower()
            if href_lower.endswith(".fgb"):
                return "application/vnd.flatgeobuf"
            if href_lower.endswith(".geojson") or href_lower.endswith(".json"):
                return "application/geo+json"

        # 3. FeatureCollection structure
        if "features" in output_data:
            return "application/geo+json"

    # 4. Raw binary → assume FlatGeobuf
    if isinstance(output_data, (bytes, bytearray)):
        return "application/vnd.flatgeobuf"

    # 5. JSON-looking string
    if isinstance(output_data, str):
        stripped = output_data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "application/geo+json"

    return "application/json"
