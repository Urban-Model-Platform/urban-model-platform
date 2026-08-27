"""In-memory adapter for ``ResultValueCachePort``.

Keeps a completed job's inline ``value`` outputs in the process for a short,
configurable retention so ``GET /jobs/{id}/results`` can answer without
re-fetching the whole document from the remote model server on every request.

Design notes
------------
*Bounded on two axes.*  A cache that can grow without limit inside the API pod
is a latent out-of-memory incident, and the UMP container is already memory
sensitive while writing large results.  Two independent limits keep it safe:

  - ``max_item_bytes`` rejects a single oversized entry outright.  Such a value
    is exactly the kind of payload we must not hold in the request path, and
    declining it costs nothing: the reader falls back to the remote fetch.
  - ``max_entries`` bounds how many jobs are held at once, evicting the least
    recently used first.  Combined with the per-item limit this caps the total
    footprint at a predictable ``max_entries * max_item_bytes`` worst case.

*Monotonic expiry.*  Retention is measured with ``time.monotonic`` rather than
wall-clock time so that an NTP correction or a daylight-saving jump can never
make entries live far too long or expire immediately.

*Lazy expiry.*  Entries are dropped when they are read (or when eviction walks
past them), not by a background sweeper.  There is no timer to own or shut
down, and a stale entry that is never read simply falls out via LRU pressure.

*Never raises.*  The port defines caching as best-effort: both operations
swallow their own failures and degrade to a miss, so neither the job-completion
path that writes nor the results request that reads can be broken by the cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from ump.core.interfaces.result_value_cache import (
    CachedValueOutputs,
    ResultValueCachePort,
)

logger = logging.getLogger(__name__)

# How many jobs may be held at once. Not exposed as a setting: together with the
# configurable per-item byte limit it only serves to bound the worst case, and a
# second knob for operators to balance against the first adds no real control.
# 128 concurrently-read jobs is generous for a gateway whose entries live ~1h.
MAX_ENTRIES = 128


@dataclass(slots=True)
class _Entry:
    """One cached job: its inline outputs and the monotonic deadline."""

    values: CachedValueOutputs
    expires_at: float


class InMemoryResultValueCache(ResultValueCachePort):
    """Process-local, TTL-bounded LRU cache of inline ``value`` outputs.

    Only populated for jobs that mix stored references with inline values
    (``transmission-mode-policy: emulate-ref``); see the port docstring.

    Being process-local, this cache is populated on the instance that completed
    the job. With several UMP replicas a request served elsewhere is a miss and
    pays the remote fetch once — correct, just not accelerated. Swap in a shared
    adapter (e.g. Redis/Dragonfly) if hit rates matter more than simplicity;
    no calling code changes.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_item_bytes: int,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        """
        Args:
            ttl_seconds:    How long an entry stays readable after it is written.
            max_item_bytes: Serialised size above which an entry is not cached.
            max_entries:    Maximum number of jobs held before LRU eviction.
        """
        self._ttl = ttl_seconds
        self._max_item_bytes = max_item_bytes
        self._max_entries = max_entries
        # Ordered by recency of use: oldest first, so eviction pops the front.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        # Guards the OrderedDict. Individual mutations are cheap, but a get is a
        # read-then-reorder and a put is a size-check-then-insert-then-evict;
        # concurrent requests must not interleave inside those sequences.
        self._lock = asyncio.Lock()

    async def get(self, job_id: str) -> Optional[CachedValueOutputs]:
        """Return the cached outputs for *job_id*, or ``None`` on any miss."""
        try:
            async with self._lock:
                entry = self._entries.get(job_id)
                if entry is None:
                    return None
                if entry.expires_at <= time.monotonic():
                    # Expired: drop it now rather than leaving it to LRU pressure.
                    del self._entries[job_id]
                    logger.debug("[value-cache] expired job_id=%s", job_id)
                    return None
                # Reading counts as use — move to the back so it evicts last.
                self._entries.move_to_end(job_id)
                logger.debug("[value-cache] hit job_id=%s", job_id)
                return entry.values
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            logger.warning("[value-cache] get failed job_id=%s err=%s", job_id, exc)
            return None

    async def put(self, job_id: str, values: CachedValueOutputs) -> None:
        """Cache *values* for *job_id*, unless they exceed ``max_item_bytes``."""
        try:
            if not values:
                # Nothing inline to serve later; caching an empty dict would make
                # a later hit indistinguishable from "this job has no values".
                return

            size = self._measure(values)
            if size is None:
                return
            if size > self._max_item_bytes:
                logger.info(
                    "[value-cache] not cached (too large) job_id=%s size=%d limit=%d",
                    job_id,
                    size,
                    self._max_item_bytes,
                )
                return

            async with self._lock:
                self._entries[job_id] = _Entry(
                    values=values,
                    expires_at=time.monotonic() + self._ttl,
                )
                self._entries.move_to_end(job_id)
                while len(self._entries) > self._max_entries:
                    evicted, _ = self._entries.popitem(last=False)
                    logger.debug("[value-cache] evicted job_id=%s", evicted)
                logger.debug(
                    "[value-cache] stored job_id=%s size=%d entries=%d",
                    job_id,
                    size,
                    len(self._entries),
                )
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            logger.warning("[value-cache] put failed job_id=%s err=%s", job_id, exc)

    def _measure(self, values: CachedValueOutputs) -> Optional[int]:
        """Serialised byte size of *values*, or ``None`` if it cannot be measured.

        The outputs come straight from a JSON results document, so their JSON
        encoding is both the natural size metric and a guarantee that what we
        cache can be rendered back into a response later. A value that fails to
        encode would break that response, so it is declined here instead.
        """
        try:
            return len(json.dumps(values).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            logger.warning("[value-cache] not cached (unserialisable) err=%s", exc)
            return None
