"""V-10: reference-link wiring, GET /results document response, downgrade transparency.

Three independent pieces, tested separately:

1. ``_apply_stored_references`` (coordinator) — the bugfix that writes stored
   references into ``status_info.links`` (client-visible) and
   ``job.stored_outputs`` (structured record for GET /results), instead of the
   previous no-op write to ``job.links``. Idempotent across repeated calls.
2. ``ResultStorageCoordinator._handle_storage_failure`` /
   ``_record_downgrade`` — the emulate-ref silent-downgrade transparency fix:
   a client that explicitly asked for ``transmissionMode: reference`` but hit
   a storage failure must be able to detect it via
   ``JobStatusInfo.transmissionModeApplied``.
3. ``JobManager.get_results`` — always returns an OGC ``document`` once a job
   has any stored output, merging stored ``href`` links with inline values
   fetched from the remote for outputs that were not stored. Jobs that never
   stored anything keep the pre-V-10 raw-proxy behaviour untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.core.config import JobManagerConfig
from ump.core.interfaces.result_storage import (
    ResultPayload,
    ResultStorageError,
    StoredReference,
)
from ump.core.managers.job_manager import JobManager
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.link import Link
from ump.core.models.providers_config import ProcessConfig
from ump.core.services.result_storage_coordinator import (
    ResultStorageCoordinator,
    _apply_stored_references,
)

JOB_ID = "job-v10"


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
        ),
    )
    defaults.update(overrides)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# 1. _apply_stored_references — the links + stored_outputs bugfix
# ---------------------------------------------------------------------------


class TestApplyStoredReferences:
    def test_writes_to_status_info_links_not_job_links(self):
        """The core bugfix: the client-visible field must be populated."""
        job = _job()
        payloads = [
            ResultPayload(
                output_id="voronoi", body_bytes=b"{}", media_type="application/geo+json"
            )
        ]
        references = [
            StoredReference(
                collection_id=f"{JOB_ID}-voronoi",
                collection_url="https://geo.example.com/ump-results/collections/"
                f"{JOB_ID}-voronoi",
                items_url="https://geo.example.com/ump-results/collections/"
                f"{JOB_ID}-voronoi/items",
            )
        ]

        updated = _apply_stored_references(job, payloads, references)

        assert updated.links == []  # job.links (the ineffective field) untouched
        assert updated.status_info is not None
        rels = {link.rel for link in updated.status_info.links}
        assert "item" in rels
        item_link = next(
            link for link in updated.status_info.links if link.rel == "item"
        )
        assert item_link.href == references[0].items_url
        assert item_link.type == "application/geo+json"

    def test_populates_stored_outputs_keyed_by_output_id(self):
        job = _job()
        payloads = [
            ResultPayload(
                output_id="a", body_bytes=b"{}", media_type="application/geo+json"
            ),
            ResultPayload(
                output_id="b", body_bytes=b"{}", media_type="application/geo+json"
            ),
        ]
        references = [
            StoredReference(
                collection_id=f"{JOB_ID}-a",
                collection_url="https://geo/collections/x-a",
                items_url="https://geo/collections/x-a/items",
            ),
            StoredReference(
                collection_id=f"{JOB_ID}-b",
                collection_url="https://geo/collections/x-b",
                items_url="https://geo/collections/x-b/items",
            ),
        ]

        updated = _apply_stored_references(job, payloads, references)

        assert set(updated.stored_outputs.keys()) == {"a", "b"}
        assert (
            updated.stored_outputs["a"]["items_url"]
            == "https://geo/collections/x-a/items"
        )
        assert updated.stored_outputs["b"]["collection_id"] == f"{JOB_ID}-b"

    def test_idempotent_on_repeated_application(self):
        """Re-running (e.g. an observer retry) must not duplicate links."""
        job = _job()
        payloads = [
            ResultPayload(
                output_id="a", body_bytes=b"{}", media_type="application/geo+json"
            )
        ]
        references = [
            StoredReference(
                collection_id=f"{JOB_ID}-a",
                collection_url="https://geo/collections/x-a",
                items_url="https://geo/collections/x-a/items",
            )
        ]

        once = _apply_stored_references(job, payloads, references)
        twice = _apply_stored_references(once, payloads, references)

        assert len(twice.status_info.links) == len(once.status_info.links) == 1
        assert twice.stored_outputs == once.stored_outputs

    def test_preserves_existing_links(self):
        """A pre-existing self/results link must survive the merge."""
        job = _job()
        job.status_info.links = [
            Link(href=f"/jobs/{JOB_ID}", rel="self", type="application/json")
        ]
        payloads = [
            ResultPayload(
                output_id="a", body_bytes=b"{}", media_type="application/geo+json"
            )
        ]
        references = [
            StoredReference(
                collection_id=f"{JOB_ID}-a",
                collection_url="https://geo/collections/x-a",
                items_url="https://geo/collections/x-a/items",
            )
        ]

        updated = _apply_stored_references(job, payloads, references)

        rels = [link.rel for link in updated.status_info.links]
        assert rels == ["self", "item"]


# ---------------------------------------------------------------------------
# 2. emulate-ref downgrade transparency
# ---------------------------------------------------------------------------


def _coordinator(
    storage=None, http_client=None, providers=None
) -> ResultStorageCoordinator:
    return ResultStorageCoordinator(
        storage_port=storage or Mock(),
        http_client=http_client or Mock(),
        providers=providers or Mock(),
    )


class TestDowngradeTransparency:
    @pytest.mark.asyncio
    async def test_emulate_ref_storage_failure_marks_status_info(self):
        job = _job(
            outputs_spec={"voronoi": {"transmissionMode": "reference"}},
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        storage = Mock()
        storage.exists = AsyncMock(return_value=False)
        storage.store = AsyncMock(side_effect=ResultStorageError("disk full"))

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(
                b'{"voronoi": {"type": "FeatureCollection", "features": []}}',
                "application/json",
            )
        )

        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        coordinator = _coordinator(
            storage=storage, http_client=http_client, providers=providers
        )
        process_config = ProcessConfig(
            id="process",
            **{"transmission-mode-policy": "emulate-ref", "result-storage": "ldproxy"},
        )

        await coordinator.coordinate(job, process_config, repo)

        persisted = await repo.get(JOB_ID)
        assert persisted.status_info.transmissionModeApplied == "value"
        assert "inline" in persisted.status_info.message.lower()

    @pytest.mark.asyncio
    async def test_emulate_ref_only_failure_still_raises(self):
        """emulate-ref-only keeps its existing fatal-failure contract."""
        job = _job()
        repo = InMemoryJobRepository()
        await repo.create(job)

        storage = Mock()
        storage.exists = AsyncMock(return_value=False)
        storage.store = AsyncMock(side_effect=ResultStorageError("disk full"))

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(b'{"voronoi": {}}', "application/json")
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        coordinator = _coordinator(
            storage=storage, http_client=http_client, providers=providers
        )
        process_config = ProcessConfig(
            id="process",
            **{
                "transmission-mode-policy": "emulate-ref-only",
                "result-storage": "ldproxy",
            },
        )

        with pytest.raises(ResultStorageError):
            await coordinator.coordinate(job, process_config, repo)

        # emulate-ref-only never marks a downgrade: value was never an option.
        persisted = await repo.get(JOB_ID)
        assert persisted.status_info.transmissionModeApplied is None

    @pytest.mark.asyncio
    async def test_no_downgrade_marker_on_successful_store(self):
        job = _job(outputs_spec={"voronoi": {"transmissionMode": "reference"}})
        repo = InMemoryJobRepository()
        await repo.create(job)

        storage = Mock()
        storage.exists = AsyncMock(return_value=False)
        storage.store = AsyncMock(
            return_value=[
                StoredReference(
                    collection_id=f"{JOB_ID}-voronoi",
                    collection_url="https://geo/collections/x",
                    items_url="https://geo/collections/x/items",
                )
            ]
        )

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(
                b'{"voronoi": {"type": "FeatureCollection", "features": []}}',
                "application/json",
            )
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        coordinator = _coordinator(
            storage=storage, http_client=http_client, providers=providers
        )
        process_config = ProcessConfig(
            id="process",
            **{"transmission-mode-policy": "emulate-ref", "result-storage": "ldproxy"},
        )

        await coordinator.coordinate(job, process_config, repo)

        persisted = await repo.get(JOB_ID)
        assert persisted.status_info.transmissionModeApplied is None
        assert persisted.stored_outputs["voronoi"]["items_url"] == (
            "https://geo/collections/x/items"
        )


# ---------------------------------------------------------------------------
# 3. GET /jobs/{id}/results — always a document once anything is stored
# ---------------------------------------------------------------------------


def _job_manager(providers, http_client, repo) -> JobManager:
    class _NoOpValidator:
        def validate(self, process_id_with_prefix: str) -> bool:
            return True

        def extract(self, process_id_with_prefix: str):
            return process_id_with_prefix.split(":", 1)

        def create(self, provider_prefix: str, process_id: str) -> str:
            return f"{provider_prefix}:{process_id}"

    return JobManager(
        providers=providers,
        http_client=http_client,
        process_id_validator=_NoOpValidator(),
        job_repo=repo,
        config=JobManagerConfig(),
    )


class TestGetResultsDocument:
    @pytest.mark.asyncio
    async def test_stored_output_becomes_href_and_remote_output_stays_inline(self):
        job = _job(
            stored_outputs={
                "voronoi": {
                    "collection_id": f"{JOB_ID}-voronoi",
                    "collection_url": "https://geo/collections/x",
                    "items_url": "https://geo/collections/x/items",
                }
            }
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(
                b'{"voronoi": {"href": "should-be-overwritten"}, "count": 42}',
                "application/json",
            )
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        manager = _job_manager(providers, http_client, repo)
        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        assert result["content_type"] == "application/json"
        import json

        body = json.loads(result["body_bytes"])
        assert body["voronoi"]["href"] == "https://geo/collections/x/items"
        assert body["voronoi"]["rel"] == "item"
        assert body["count"] == 42  # non-stored output passed through inline

    @pytest.mark.asyncio
    async def test_remote_fetch_failure_still_returns_stored_outputs(self):
        job = _job(
            stored_outputs={
                "voronoi": {
                    "collection_id": f"{JOB_ID}-voronoi",
                    "collection_url": "https://geo/collections/x",
                    "items_url": "https://geo/collections/x/items",
                }
            }
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(side_effect=RuntimeError("network down"))
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        manager = _job_manager(providers, http_client, repo)
        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        import json

        body = json.loads(result["body_bytes"])
        assert body["voronoi"]["href"] == "https://geo/collections/x/items"

    @pytest.mark.asyncio
    async def test_emulate_ref_only_never_leaks_inline_values(self):
        """emulate-ref-only must return only href references, never fetching
        or merging the remote document (see ProcessConfig.transmission_mode_policy:
        the value channel is never open under this policy)."""
        job = _job(
            stored_outputs={
                "voronoi": {
                    "collection_id": f"{JOB_ID}-voronoi",
                    "collection_url": "https://geo/collections/x",
                    "items_url": "https://geo/collections/x/items",
                }
            }
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            side_effect=AssertionError(
                "remote must not be fetched under emulate-ref-only"
            )
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )
        providers.get_process_config = Mock(
            return_value=ProcessConfig(
                id="process",
                **{
                    "transmission-mode-policy": "emulate-ref-only",
                    "result-storage": "ldproxy",
                },
            )
        )

        manager = _job_manager(providers, http_client, repo)
        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        import json

        body = json.loads(result["body_bytes"])
        assert body == {
            "voronoi": {
                "href": "https://geo/collections/x/items",
                "rel": "item",
                "type": "application/geo+json",
            }
        }

    @pytest.mark.asyncio
    async def test_job_without_stored_outputs_keeps_raw_proxy_behaviour(self):
        """Jobs that never stored anything are unaffected by V-10."""
        job = _job(stored_outputs=None)
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(b"raw-bytes-passthrough", "application/flatgeobuf")
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        manager = _job_manager(providers, http_client, repo)
        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        assert result["content_type"] == "application/flatgeobuf"
        assert result["body_bytes"] == b"raw-bytes-passthrough"
