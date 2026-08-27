"""Port: ResultValueCachePort — short-lived cache for inline ``value`` outputs.

Why this exists
---------------
Under ``transmission-mode-policy: emulate-ref`` a job can mix both output
channels: the client asks for some outputs as ``transmissionMode: reference``
(stored in the result store, e.g. ldproxy) and receives the rest as inline
``value`` entries.

The reference outputs need no remote call at ``GET /jobs/{id}/results`` — their
links are built from data UMP already holds.  The *value* outputs, however, only
exist in the remote server's results document, so every single ``/results``
request re-fetched that whole document from the model server just to copy a few
inline values out of it.  For a large result this dominates the response time,
and it happens again on every repeat request even though a ``successful`` job is
terminal and its result no longer changes.

This port lets UMP keep those inline values for a short while after the job
completes, so ``/results`` can answer without touching the remote server.

Best-effort by design
---------------------
A cache miss is **never** an error and never changes the response *content*:
the caller falls back to the existing remote fetch.  That is what makes this
safe to run with several UMP replicas, where an in-memory adapter is only
populated on the pod that completed the job — a request served by another pod
(or by a pod that restarted, or after the entry expired) simply pays the old
cost once.  Implementations must therefore never raise on a miss and should
degrade silently rather than propagate storage problems into the request path.

Scope
-----
Only jobs under ``emulate-ref`` *with* at least one reference output populate
this cache; those are exactly the jobs whose ``/results`` performs the mixed
document merge.  ``emulate-ref-only`` (no inline values at all) and the
non-storing policies never use it.  See ``ResultStorageCoordinator`` (writer)
and ``JobManager._build_stored_results_document`` (reader).

Retention, size limits and eviction are deliberately **not** part of this
contract: they are adapter policy, configured at the composition root.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

# One cache entry: the inline ``value`` outputs of a single job, keyed by
# output id, exactly as they appear in the remote OGC document response
# (e.g. ``{"classification_breaks": {"value": [...], "mediaType": "..."}}``).
CachedValueOutputs = dict[str, Any]


class ResultValueCachePort(ABC):
    """Contract for caching a completed job's inline ``value`` outputs.

    Entries are written once, when the job reaches a terminal successful state
    and its result document has just been fetched for storage, and read back on
    every ``GET /results`` for that job until they expire.  Because a
    ``successful`` job is terminal, a cached entry can never become stale — it
    can only be *absent*.

    Both methods are async so adapters may be backed by an external store
    (Redis/Dragonfly, a shared database) without changing callers.
    """

    @abstractmethod
    async def get(self, job_id: str) -> Optional[CachedValueOutputs]:
        """Return the cached inline outputs for *job_id*, or ``None`` on a miss.

        ``None`` covers every "not available" case — never cached, expired,
        evicted, or rejected as too large — because the caller treats them
        identically: fall back to fetching the remote document.

        Must not raise: a failing cache degrades to a miss.
        """
        ...

    @abstractmethod
    async def put(self, job_id: str, values: CachedValueOutputs) -> None:
        """Cache the inline *values* of *job_id* for this adapter's retention.

        Storing is best-effort and advisory: an implementation may legitimately
        decline (e.g. the entry exceeds a configured size limit), in which case
        a later ``get`` simply reports a miss.  Callers must not depend on a
        subsequent ``get`` succeeding.

        Must not raise: caching is an optimisation and must never fail the job
        completion path that writes it.
        """
        ...


class NullResultValueCache(ResultValueCachePort):
    """No-op cache — every read is a miss, every write is discarded.

    Injected when value caching is disabled, so the reader always takes the
    remote-fetch fallback and behaviour matches the pre-cache implementation
    exactly. Lets the rest of the code stay free of ``if cache is not None``
    branches.
    """

    async def get(self, job_id: str) -> Optional[CachedValueOutputs]:
        return None

    async def put(self, job_id: str, values: CachedValueOutputs) -> None:
        return None
