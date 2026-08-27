"""Result value cache: coordinator write path and JobManager read path.

The adapter itself is covered by ``test_result_value_cache.py``. What is
verified here is the *wiring*: that the cache is populated exactly where it
should be, is read exactly where it should be, and that a miss degrades to the
pre-cache behaviour rather than to an error.

The single behaviour that motivates the whole feature is
``test_cache_hit_skips_the_remote_fetch``: under ``emulate-ref`` the read path
used to re-download the complete remote document on every ``GET /results``
call, purely to recover a few small inline values. A cache hit must remove that
round-trip entirely — asserted by making the HTTP client raise if it is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from unittest.mock import AsyncMock, Mock

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.core.config import JobManagerConfig
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.result_value_cache import (
    CachedValueOutputs,
    NullResultValueCache,
    ResultValueCachePort,
)
from ump.core.managers.job_manager import JobManager
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.providers_config import ProcessConfig
from ump.core.services.result_storage_coordinator import ResultStorageCoordinator

JOB_ID = "job-cache"

REMOTE_DOCUMENT = {
    "voronoi_diagram": {"type": "FeatureCollection", "features": []},
    "classification_breaks": [1, 2, 3],
    "run_count": 7,
}


class _RecordingCache(ResultValueCachePort):
    """Minimal in-test double that records writes and serves seeded reads.

    Deliberately not the real ``InMemoryResultValueCache``: these tests are
    about *whether and with what* the cache is called, not about TTL or
    eviction, and a real adapter would couple them to policies they do not
    assert.
    """

    def __init__(self, seeded: Optional[CachedValueOutputs] = None) -> None:
        self.puts: list[tuple[str, CachedValueOutputs]] = []
        self.gets: list[str] = []
        self._seeded = seeded

    async def get(self, job_id: str) -> Optional[CachedValueOutputs]:
        self.gets.append(job_id)
        return self._seeded

    async def put(self, job_id: str, values: CachedValueOutputs) -> None:
        self.puts.append((job_id, values))


class _NoOpValidator(ProcessIdValidatorPort):
    def validate(self, process_id_with_prefix: str) -> bool:
        return True

    def extract(self, process_id_with_prefix: str) -> tuple[str, str]:
        prefix, _, rest = process_id_with_prefix.partition(":")
        return prefix, rest

    def create(self, provider_prefix: str, process_id: str) -> str:
        return f"{provider_prefix}:{process_id}"


def _job(**overrides: Any) -> Job:
    defaults: dict[str, Any] = dict(
        id=JOB_ID,
        process_id="test:process",
        provider="test",
        remote_job_id="remote-1",
        status="successful",
        status_info=JobStatusInfo(
            jobID=JOB_ID,
            status=StatusCode.successful,
            processID="test:process",
            created=datetime.now(timezone.utc),
            progress=0,
        ),
    )
    defaults.update(overrides)
    return Job(**defaults)


def _process_config(policy: str, store_outputs: Optional[list[str]] = None):
    payload: dict[str, Any] = {
        "id": "process",
        "transmission-mode-policy": policy,
        "result-storage": "ldproxy",
    }
    if store_outputs is not None:
        payload["store-outputs"] = store_outputs
    return ProcessConfig.model_validate(payload)


def _providers(process_config=None) -> Mock:
    providers = Mock()
    providers.get_provider = Mock(return_value=Mock(url="https://remote.example.com"))
    if process_config is not None:
        providers.get_process_config = Mock(return_value=process_config)
    return providers


def _http_returning(document: dict, content_type: str = "application/json") -> Mock:
    client = Mock()
    client.get_content = AsyncMock(
        return_value=(json.dumps(document).encode("utf-8"), content_type)
    )
    return client


def _http_that_must_not_be_called(reason: str) -> Mock:
    client = Mock()
    client.get_content = AsyncMock(side_effect=AssertionError(reason))
    return client


async def _run_coordinator(
    cache: ResultValueCachePort,
    policy: str,
    *,
    store_outputs: Optional[list[str]] = None,
    document: dict | None = None,
    content_type: str = "application/json",
    raw_body: bytes | None = None,
) -> None:
    """Drive the coordinator far enough to exercise the cache write path.

    Storage is stubbed out: what happens to the payloads afterwards is
    irrelevant here, only that the document passed through the caching hook.
    """
    job = _job(outputs_spec={"voronoi_diagram": {"transmissionMode": "reference"}})
    repo = InMemoryJobRepository()
    await repo.create(job)

    body = (
        raw_body
        if raw_body is not None
        else json.dumps(document if document is not None else REMOTE_DOCUMENT).encode(
            "utf-8"
        )
    )
    http_client = Mock()
    http_client.get_content = AsyncMock(return_value=(body, content_type))

    storage = Mock()
    storage.exists = AsyncMock(return_value=False)
    storage.store = AsyncMock(return_value=[])

    coordinator = ResultStorageCoordinator(
        storage_port=storage,
        http_client=http_client,
        providers=_providers(),
        value_cache=cache,
    )
    await coordinator.coordinate(
        job, _process_config(policy, store_outputs), repo
    )


def _job_manager(
    providers,
    http_client,
    repo,
    value_cache: Optional[ResultValueCachePort] = None,
) -> JobManager:
    return JobManager(
        providers=providers,
        http_client=http_client,
        process_id_validator=_NoOpValidator(),
        job_repo=repo,
        config=JobManagerConfig(),
        value_cache=value_cache,
    )


def _stored_outputs() -> dict:
    return {
        "voronoi_diagram": {
            "collection_id": f"{JOB_ID}-voronoi_diagram",
            "collection_url": "https://geo/collections/v",
            "items_url": "https://geo/collections/v/items",
        }
    }


# ---------------------------------------------------------------------------
# Write path — ResultStorageCoordinator
# ---------------------------------------------------------------------------


class TestCoordinatorWritePath:
    @pytest.mark.asyncio
    async def test_emulate_ref_caches_the_complement_of_stored_outputs(self):
        """Only the outputs that stay inline are cached.

        The stored output is replaced by an href on read, so caching it would
        hold the large payload in memory for no benefit at all.
        """
        cache = _RecordingCache()

        await _run_coordinator(
            cache, "emulate-ref", store_outputs=["voronoi_diagram"]
        )

        assert len(cache.puts) == 1
        job_id, values = cache.puts[0]
        assert job_id == JOB_ID
        assert values == {"classification_breaks": [1, 2, 3], "run_count": 7}
        assert "voronoi_diagram" not in values

    @pytest.mark.asyncio
    async def test_emulate_ref_only_is_never_cached(self):
        """The read path never fetches under this policy, so an entry written
        here could only ever be dead weight."""
        cache = _RecordingCache()

        await _run_coordinator(
            cache, "emulate-ref-only", store_outputs=["voronoi_diagram"]
        )

        assert cache.puts == []

    @pytest.mark.asyncio
    async def test_dot_notation_store_ids_are_skipped(self):
        """A nested stored output makes its top-level key only partially a
        reference; splitting that correctly is not worth the risk."""
        cache = _RecordingCache()

        await _run_coordinator(
            cache,
            "emulate-ref",
            store_outputs=["results.voronoi"],
            document={"results": {"voronoi": {"type": "FeatureCollection"}}, "n": 1},
        )

        assert cache.puts == []

    @pytest.mark.asyncio
    async def test_raw_response_is_skipped(self):
        """With a raw body the bytes ARE the single output — there is no
        inline remainder to cache."""
        cache = _RecordingCache()

        await _run_coordinator(
            cache,
            "emulate-ref",
            store_outputs=["voronoi_diagram"],
            raw_body=b'{"type": "FeatureCollection", "features": []}',
            content_type="application/geo+json",
        )

        assert cache.puts == []

    @pytest.mark.asyncio
    async def test_geojson_mislabelled_as_json_is_skipped(self):
        """Servers often label a FeatureCollection ``application/json``. Its
        top-level keys are ``type``/``features``, not output IDs."""
        cache = _RecordingCache()

        await _run_coordinator(
            cache,
            "emulate-ref",
            store_outputs=["voronoi_diagram"],
            document={"type": "FeatureCollection", "features": []},
        )

        assert cache.puts == []

    @pytest.mark.asyncio
    async def test_nothing_cached_when_every_output_is_stored(self):
        """The complement is empty, so there is nothing worth an entry."""
        cache = _RecordingCache()

        await _run_coordinator(
            cache,
            "emulate-ref",
            store_outputs=["voronoi_diagram"],
            document={"voronoi_diagram": {"type": "FeatureCollection"}},
        )

        assert cache.puts == []


# ---------------------------------------------------------------------------
# Read path — JobManager
# ---------------------------------------------------------------------------


class TestJobManagerReadPath:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_the_remote_fetch(self):
        """The point of the whole feature: no upstream round-trip on a hit."""
        job = _job(stored_outputs=_stored_outputs())
        repo = InMemoryJobRepository()
        await repo.create(job)

        cache = _RecordingCache(seeded={"classification_breaks": [1, 2, 3]})
        http_client = _http_that_must_not_be_called(
            "remote must not be fetched when the value cache hits"
        )

        manager = _job_manager(
            _providers(_process_config("emulate-ref")), http_client, repo, cache
        )
        result = await manager.get_results(JOB_ID)

        body = json.loads(result["body_bytes"])
        assert body["classification_breaks"] == [1, 2, 3]
        http_client.get_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stored_href_still_wins_over_cached_value(self):
        """Stored outputs stay authoritative — a stale cached entry for the
        same output id must never shadow the reference link."""
        job = _job(stored_outputs=_stored_outputs())
        repo = InMemoryJobRepository()
        await repo.create(job)

        cache = _RecordingCache(
            seeded={"voronoi_diagram": {"stale": True}, "run_count": 7}
        )

        manager = _job_manager(
            _providers(_process_config("emulate-ref")),
            _http_that_must_not_be_called("cache hit"),
            repo,
            cache,
        )
        result = await manager.get_results(JOB_ID)

        body = json.loads(result["body_bytes"])
        assert body["voronoi_diagram"] == {
            "href": "https://geo/collections/v/items",
            "rel": "item",
            "type": "application/geo+json",
        }
        assert body["run_count"] == 7

    @pytest.mark.asyncio
    async def test_cache_miss_falls_back_to_the_remote_fetch(self):
        """A miss is the normal case after TTL expiry or a restart — it must
        reproduce the pre-cache behaviour exactly, not raise."""
        job = _job(stored_outputs=_stored_outputs())
        repo = InMemoryJobRepository()
        await repo.create(job)

        cache = _RecordingCache(seeded=None)
        http_client = _http_returning(REMOTE_DOCUMENT)

        manager = _job_manager(
            _providers(_process_config("emulate-ref")), http_client, repo, cache
        )
        result = await manager.get_results(JOB_ID)

        body = json.loads(result["body_bytes"])
        assert body["classification_breaks"] == [1, 2, 3]
        assert body["run_count"] == 7
        http_client.get_content.assert_awaited()

    @pytest.mark.asyncio
    async def test_emulate_ref_only_never_consults_the_cache(self):
        """No fetch happens under this policy, so there is nothing to save."""
        job = _job(stored_outputs=_stored_outputs())
        repo = InMemoryJobRepository()
        await repo.create(job)

        cache = _RecordingCache(seeded={"leaked": "value"})

        manager = _job_manager(
            _providers(_process_config("emulate-ref-only")),
            _http_that_must_not_be_called("emulate-ref-only never fetches"),
            repo,
            cache,
        )
        result = await manager.get_results(JOB_ID)

        assert cache.gets == []
        body = json.loads(result["body_bytes"])
        assert "leaked" not in body

    @pytest.mark.asyncio
    async def test_default_null_cache_preserves_legacy_behaviour(self):
        """Callers that inject no cache (all pre-existing ones) must keep
        fetching, which is what makes the feature backwards compatible."""
        job = _job(stored_outputs=_stored_outputs())
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = _http_returning(REMOTE_DOCUMENT)

        manager = _job_manager(
            _providers(_process_config("emulate-ref")), http_client, repo, None
        )
        result = await manager.get_results(JOB_ID)

        http_client.get_content.assert_awaited()
        body = json.loads(result["body_bytes"])
        assert body["run_count"] == 7


class TestNullCacheWiring:
    @pytest.mark.asyncio
    async def test_null_cache_never_reports_a_hit_after_a_write(self):
        """Guards the disabled-cache configuration: put() must not make a
        later get() return anything."""
        cache = NullResultValueCache()

        await cache.put(JOB_ID, {"a": 1})

        assert await cache.get(JOB_ID) is None
