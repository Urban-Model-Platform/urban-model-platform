"""Tests for ``InMemoryResultValueCache``.

The adapter backs ``ResultValueCachePort``, whose contract is deliberately
forgiving: a miss is normal, and neither operation may raise. These tests
therefore assert two things in equal measure — that a hit really avoids the
remote fetch (the whole point of the cache), and that every "cannot cache
this" path degrades to a plain miss rather than an error.

Expiry is exercised through a patched monotonic clock rather than real sleeps,
so the suite stays fast and deterministic under ``pytest-randomly``.
"""

from __future__ import annotations

import asyncio

import pytest

from ump.adapters.result_storage import inmemory_value_cache as module
from ump.adapters.result_storage.inmemory_value_cache import InMemoryResultValueCache
from ump.core.interfaces.result_value_cache import NullResultValueCache

# Comfortably above anything the small fixtures below serialise to, so size
# limits only come into play in the tests that set out to trigger them.
_AMPLE_BYTES = 10_000

_VALUES = {
    "classification_breaks": {"value": [0.012, 0.506], "mediaType": "application/json"}
}


def _cache(**overrides) -> InMemoryResultValueCache:
    kwargs = {"ttl_seconds": 3600.0, "max_item_bytes": _AMPLE_BYTES, "max_entries": 128}
    kwargs.update(overrides)
    return InMemoryResultValueCache(**kwargs)


class _FakeClock:
    """Stand-in for ``time.monotonic`` that only moves when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(module.time, "monotonic", fake)
    return fake


class TestBasicRoundTrip:
    @pytest.mark.asyncio
    async def test_unknown_job_is_a_miss(self):
        assert await _cache().get("never-stored") is None

    @pytest.mark.asyncio
    async def test_stored_values_are_returned(self):
        cache = _cache()
        await cache.put("job-1", _VALUES)
        assert await cache.get("job-1") == _VALUES

    @pytest.mark.asyncio
    async def test_jobs_are_isolated_from_each_other(self):
        cache = _cache()
        await cache.put("job-1", {"a": 1})
        await cache.put("job-2", {"b": 2})
        assert await cache.get("job-1") == {"a": 1}
        assert await cache.get("job-2") == {"b": 2}

    @pytest.mark.asyncio
    async def test_rewriting_a_job_replaces_its_values(self):
        cache = _cache()
        await cache.put("job-1", {"a": 1})
        await cache.put("job-1", {"a": 2})
        assert await cache.get("job-1") == {"a": 2}


class TestExpiry:
    @pytest.mark.asyncio
    async def test_entry_survives_until_the_ttl_elapses(self, clock):
        cache = _cache(ttl_seconds=60.0)
        await cache.put("job-1", _VALUES)

        clock.advance(59.0)

        assert await cache.get("job-1") == _VALUES

    @pytest.mark.asyncio
    async def test_entry_is_a_miss_once_the_ttl_elapsed(self, clock):
        cache = _cache(ttl_seconds=60.0)
        await cache.put("job-1", _VALUES)

        clock.advance(61.0)

        assert await cache.get("job-1") is None

    @pytest.mark.asyncio
    async def test_expired_entry_is_dropped_not_merely_hidden(self, clock):
        """An expired read must free the slot, or dead entries would linger
        until LRU pressure happened to reach them."""
        cache = _cache(ttl_seconds=60.0)
        await cache.put("job-1", _VALUES)

        clock.advance(61.0)
        await cache.get("job-1")

        assert "job-1" not in cache._entries

    @pytest.mark.asyncio
    async def test_reading_does_not_extend_the_ttl(self, clock):
        """Retention is measured from the write: a job polled repeatedly must
        still expire on schedule rather than being kept alive by reads."""
        cache = _cache(ttl_seconds=60.0)
        await cache.put("job-1", _VALUES)

        clock.advance(30.0)
        assert await cache.get("job-1") == _VALUES
        clock.advance(31.0)

        assert await cache.get("job-1") is None


class TestEviction:
    @pytest.mark.asyncio
    async def test_oldest_entry_is_evicted_when_capacity_is_exceeded(self):
        cache = _cache(max_entries=2)
        await cache.put("job-1", {"a": 1})
        await cache.put("job-2", {"b": 2})
        await cache.put("job-3", {"c": 3})

        assert await cache.get("job-1") is None
        assert await cache.get("job-2") == {"b": 2}
        assert await cache.get("job-3") == {"c": 3}

    @pytest.mark.asyncio
    async def test_reading_an_entry_protects_it_from_the_next_eviction(self):
        """Recency is what LRU is for: a job still being polled must outlive an
        untouched one."""
        cache = _cache(max_entries=2)
        await cache.put("job-1", {"a": 1})
        await cache.put("job-2", {"b": 2})

        await cache.get("job-1")  # job-2 is now the least recently used
        await cache.put("job-3", {"c": 3})

        assert await cache.get("job-1") == {"a": 1}
        assert await cache.get("job-2") is None

    @pytest.mark.asyncio
    async def test_capacity_is_never_exceeded(self):
        cache = _cache(max_entries=3)
        for i in range(20):
            await cache.put(f"job-{i}", {"i": i})

        assert len(cache._entries) == 3


class TestDeclinedEntries:
    """Values the adapter refuses to hold. Each must behave exactly like a
    miss, so the caller transparently falls back to the remote fetch."""

    @pytest.mark.asyncio
    async def test_oversized_values_are_not_cached(self):
        cache = _cache(max_item_bytes=100)
        await cache.put("job-1", {"blob": "x" * 500})
        assert await cache.get("job-1") is None

    @pytest.mark.asyncio
    async def test_values_within_the_limit_are_cached(self):
        cache = _cache(max_item_bytes=100)
        await cache.put("job-1", {"small": "x"})
        assert await cache.get("job-1") == {"small": "x"}

    @pytest.mark.asyncio
    async def test_empty_values_are_not_cached(self):
        """Caching ``{}`` would make a later hit indistinguishable from "this
        job has no inline outputs", silently suppressing the fallback."""
        cache = _cache()
        await cache.put("job-1", {})
        assert await cache.get("job-1") is None

    @pytest.mark.asyncio
    async def test_unserialisable_values_are_declined_without_raising(self):
        cache = _cache()
        await cache.put("job-1", {"bad": {1, 2, 3}})  # a set is not JSON
        assert await cache.get("job-1") is None

    @pytest.mark.asyncio
    async def test_an_oversized_write_leaves_a_previous_entry_intact(self):
        cache = _cache(max_item_bytes=100)
        await cache.put("job-1", {"small": "x"})
        await cache.put("job-1", {"blob": "x" * 500})
        assert await cache.get("job-1") == {"small": "x"}


class TestNeverRaises:
    """The port promises caching can never break the paths that use it."""

    @pytest.mark.asyncio
    async def test_get_returns_a_miss_if_the_store_misbehaves(self, monkeypatch):
        cache = _cache()
        await cache.put("job-1", _VALUES)
        monkeypatch.setattr(cache, "_entries", _RaisingMapping(), raising=False)
        assert await cache.get("job-1") is None

    @pytest.mark.asyncio
    async def test_put_swallows_a_misbehaving_store(self, monkeypatch):
        cache = _cache()
        monkeypatch.setattr(cache, "_entries", _RaisingMapping(), raising=False)
        await cache.put("job-1", _VALUES)  # must not raise


class _RaisingMapping(dict):
    """Mapping whose access blows up, simulating an unexpected internal fault."""

    def get(self, *args, **kwargs):
        raise RuntimeError("boom")

    def __setitem__(self, *args, **kwargs):
        raise RuntimeError("boom")


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writes_and_reads_stay_consistent(self):
        """Several requests may touch the cache at once; the lock must keep the
        multi-step get/put sequences from interleaving into a corrupt state."""
        cache = _cache(max_entries=50)

        async def write(i: int) -> None:
            await cache.put(f"job-{i}", {"i": i})

        async def read(i: int):
            return await cache.get(f"job-{i}")

        await asyncio.gather(*(write(i) for i in range(50)))
        results = await asyncio.gather(*(read(i) for i in range(50)))

        assert results == [{"i": i} for i in range(50)]
        assert len(cache._entries) == 50


class TestNullCache:
    """The disabled-cache adapter must make every read a miss, so behaviour is
    identical to having no cache at all."""

    @pytest.mark.asyncio
    async def test_put_then_get_is_still_a_miss(self):
        cache = NullResultValueCache()
        await cache.put("job-1", _VALUES)
        assert await cache.get("job-1") is None
