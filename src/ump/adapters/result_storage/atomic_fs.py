"""Atomic filesystem write utilities.

Every file written by the ldproxy result storage adapter goes through one of
the functions here.  The pattern is always the same:

  1. Write the data to a temporary file in the *same directory* as the target.
     Same directory is critical: ``os.replace`` is only atomic when source and
     destination are on the same filesystem/device (guaranteed for same dir).

  2. Call ``os.replace(tmp, destination)``.  On POSIX this is a single syscall
     that is atomic and durable — readers either see the old file or the new one,
     never a half-written intermediate.

  3. On any failure, remove the temporary file so stale temps do not accumulate.

This guarantee matters because ldproxy watches the store directory for file
changes.  A half-written YAML or GeoPackage that ldproxy picks up mid-write
would cause confusing load errors.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically.

    The destination directory must already exist.  The caller is responsible
    for creating it beforehand (``path.parent.mkdir(parents=True, exist_ok=True)``).

    Raises:
        OSError: if the write or rename fails (e.g. disk full, permissions).
    """
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically.

    The destination directory must already exist.

    Raises:
        OSError: if the write or rename fails.
    """
    tmp = _tmp_path(path)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_write_path(final_path: Path) -> Generator[Path, None, None]:
    """Context manager for callers that write to a path themselves (e.g. geopandas).

    Yields a *temporary* path in the same directory as *final_path*.  On clean
    exit the temp file is atomically renamed to *final_path*.  On any exception
    the temp file is deleted and the exception re-raised.

    Usage::

        with atomic_write_path(target) as tmp:
            geodataframe.to_file(str(tmp), driver="GPKG", engine="pyogrio")
        # target now contains the fully written GeoPackage

    The destination directory must already exist.
    """
    tmp = _tmp_path(final_path)
    try:
        yield tmp
        os.replace(tmp, final_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tmp_path(path: Path) -> Path:
    """Return a hidden temp path adjacent to *path* (same directory).

    The original file suffix is preserved so format-aware tools (e.g. pyogrio
    for GeoPackage files) do not emit warnings about an unexpected extension.
    """
    return path.parent / f".{path.stem}.tmp{path.suffix}"
