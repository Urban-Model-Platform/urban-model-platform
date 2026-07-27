"""Port: ResultStoragePort — the boundary between UMP core and result stores.

UMP core is responsible for deciding *what* to store and *when* to store it.
The result storage adapter is responsible for deciding *how*: which file format,
which directory layout, which external service.  This file defines the contract
between the two sides.

Three types live here:

ResultPayload
    A single output fetched from the remote server and ready to store.
    The body is raw bytes; the media_type is the IANA type as reported by
    the remote (e.g. ``application/geo+json``, ``application/flatgeobuf``).
    The output_id is the key that identified this output in the OGC document
    response.

StoredReference
    What the adapter returns after a successful store operation: the public
    URLs a client can use to access the stored result, plus the internal
    collection_id UMP uses to identify this result later (e.g. for cleanup).

ResultStoragePort
    The abstract interface.  The ldproxy adapter (Feature V) is the first
    concrete implementation.  A no-op NullResultStorage is also provided for
    development and test environments where no store is configured.

Exceptions
    ResultStorageError  — storage attempt failed (I/O error, API error, etc.)
    UnsupportedResultError — the output format is not in the store's whitelist
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data classes — plain, immutable value objects with no behaviour
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultPayload:
    """One output from the remote server, ready to be handed to a result store.

    Attributes:
        output_id:  The key used for this output in the OGC document response
                    (e.g. ``"voronoi_diagram"``).  Used as the collection name
                    in the result store.
        body_bytes: Raw bytes of the output as received from the remote server.
                    For ``response: document`` responses these are the bytes of
                    the entire JSON document; the store adapter is responsible
                    for extracting and decoding the value for this output_id.
                    For ``response: raw`` responses (single output) these are
                    the output bytes directly.
        media_type: IANA media type string (e.g. ``"application/geo+json"``).
                    The store adapter uses this to decide whether it can process
                    the payload and which parser to use.
    """

    output_id: str
    body_bytes: bytes
    media_type: str


@dataclass(frozen=True)
class StoredReference:
    """What the result store returns after successfully persisting one output.

    The URLs are public and suitable for inclusion in an OGC results response
    as a ``transmissionMode: reference`` link.

    Attributes:
        collection_id:  Stable identifier for this stored result within the
                        store (e.g. the UUID-based collection name in ldproxy).
                        Also used by ``delete()`` to remove the result later.
        collection_url: URL of the OGC API Features collection root,
                        e.g. ``https://geodata.example.com/ump-results/collections/{id}``.
        items_url:      URL of the items endpoint — the most useful link for
                        clients that want to fetch the actual features,
                        e.g. ``{collection_url}/items``.
    """

    collection_id: str
    collection_url: str
    items_url: str


# ---------------------------------------------------------------------------
# Exceptions — raised by adapters, caught by the ResultStorageCoordinator
# ---------------------------------------------------------------------------


class ResultStorageError(Exception):
    """Raised when the store adapter fails to persist a result.

    Covers I/O errors, network failures, filesystem permission problems, API
    errors from external services, etc.  The computation that produced the
    result succeeded; only the storage step failed.

    The ResultStorageCoordinator decides how to handle this based on the
    configured transmission_mode_policy:
      - emulate-ref:      fall back to returning the value inline to the client.
      - emulate-ref-only: propagate as a results-unavailable error (502).
    """


class UnsupportedResultError(ResultStorageError):
    """Raised when an output's media_type is not in the store's format whitelist.

    Supported formats for the ldproxy store (Feature V):
      - application/geo+json    (GeoJSON FeatureCollection)
      - application/flatgeobuf  (FlatGeobuf FeatureCollection, read via pyogrio)

    Any other media_type raises this exception.  Handling is the same as
    ResultStorageError: fall back to inline value for emulate-ref, or surface
    as a 502 for emulate-ref-only.
    """


# ---------------------------------------------------------------------------
# Port — the abstract interface implemented by storage adapters
# ---------------------------------------------------------------------------


class ResultStoragePort(ABC):
    """Contract for result store adapters.

    A concrete adapter (e.g. LdproxyResultStorage) implements all three
    methods.  A no-op adapter (NullResultStorage) is provided for development
    and test environments where result storage is not configured.

    All methods are async to accommodate adapters that write to remote services
    (Kubernetes API, SFTP, cloud object stores, etc.) as well as local
    filesystem writes.
    """

    @abstractmethod
    async def store(
        self,
        job_id: str,
        payloads: list[ResultPayload],
    ) -> list[StoredReference]:
        """Store one or more output payloads and return their public references.

        Args:
            job_id:   The UMP job UUID.  Used as the stable identity for this
                      result set within the store (provider id, gpkg filename,
                      ConfigMap name, etc.).
            payloads: One payload per output to store.  Each payload carries the
                      raw bytes and the media_type the adapter uses to decide how
                      to parse and persist the data.

        Returns:
            One StoredReference per successfully stored payload, in the same
            order as the input payloads.

        Raises:
            UnsupportedResultError: if a payload's media_type is not supported.
            ResultStorageError:     on any other storage failure.
        """

    @abstractmethod
    async def delete(self, job_id: str) -> None:
        """Remove all stored results for this job from the store.

        Idempotent: calling delete for a job_id that does not exist must not
        raise.  Called during anonymous-job cleanup and explicit result
        eviction.

        Args:
            job_id: The UMP job UUID whose results should be removed.
        """

    @abstractmethod
    async def exists(self, job_id: str) -> bool:
        """Return True if results for this job are already stored.

        Used by the ResultStorageCoordinator to skip a redundant store operation
        on retry (idempotency guard).

        Args:
            job_id: The UMP job UUID to check.
        """


# ---------------------------------------------------------------------------
# No-op adapter — used when no result store is configured
# ---------------------------------------------------------------------------


class NullResultStorage(ResultStoragePort):
    """No-op result store for development and test environments.

    Every method is a harmless no-op.  Injected when result_storage is
    ``remote`` for all processes or when no store is configured at the
    composition root.
    """

    async def store(
        self,
        job_id: str,
        payloads: list[ResultPayload],
    ) -> list[StoredReference]:
        # Nothing to store; return an empty reference list.
        return []

    async def delete(self, job_id: str) -> None:
        # Nothing to clean up.
        pass

    async def exists(self, job_id: str) -> bool:
        # Nothing has ever been stored by this adapter.
        return False
