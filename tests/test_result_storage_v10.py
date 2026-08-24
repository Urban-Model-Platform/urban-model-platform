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
from ump.core.exceptions import OGCExceptionResponse, OGCProcessException
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
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
            progress=0,
        ),
    )
    defaults.update(overrides)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# 1. _apply_stored_references — the links + stored_outputs bugfix
# ---------------------------------------------------------------------------


class TestApplyStoredReferences:
    def test_stored_outputs_populated_but_status_info_links_untouched(self):
        """Result reference hrefs must stay OUT of statusInfo.links — they are
        only exposed via GET /jobs/{id}/results (see stored_outputs below).
        job.links (the ineffective internal field) is also untouched."""
        job = _job()
        assert job.status_info is not None
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
        # No new links of any kind: statusInfo carries lifecycle links only.
        assert (updated.status_info.links or []) == (job.status_info.links or [])
        assert updated.stored_outputs is not None
        assert updated.stored_outputs["voronoi"]["items_url"] == references[0].items_url

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

        assert updated.stored_outputs is not None
        assert set(updated.stored_outputs.keys()) == {"a", "b"}
        assert (
            updated.stored_outputs["a"]["items_url"]
            == "https://geo/collections/x-a/items"
        )
        assert updated.stored_outputs["b"]["collection_id"] == f"{JOB_ID}-b"

    def test_idempotent_on_repeated_application(self):
        """Re-running (e.g. an observer retry) must not duplicate stored_outputs."""
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

        assert twice.stored_outputs == once.stored_outputs

    def test_preserves_existing_links_and_does_not_add_item_links(self):
        """A pre-existing self/results link must survive the merge, and no
        item link is ever added — statusInfo.links stays untouched."""
        job = _job()
        assert job.status_info is not None
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

        assert updated.status_info is not None and updated.status_info.links is not None
        rels = [link.rel for link in updated.status_info.links]
        assert rels == ["self"]


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
    async def test_emulate_ref_storage_failure_raises_no_value_fallback(self):
        """Option A: a required store (client explicitly asked for reference)
        that fails must NEVER fall back to the inline value — it raises so the
        client sees an error rather than a potentially huge payload."""
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
        process_config = ProcessConfig.model_validate(
            {
                "id": "process",
                "transmission-mode-policy": "emulate-ref",
                "result-storage": "ldproxy",
            }
        )

        with pytest.raises(ResultStorageError):
            await coordinator.coordinate(job, process_config, repo)

        # No value-downgrade marker is written under Option A.
        persisted = await repo.get(JOB_ID)
        assert persisted is not None and persisted.status_info is not None
        assert persisted.status_info.transmissionModeApplied != "value"

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
        process_config = ProcessConfig.model_validate(
            {
                "id": "process",
                "transmission-mode-policy": "emulate-ref-only",
                "result-storage": "ldproxy",
            }
        )

        with pytest.raises(ResultStorageError):
            await coordinator.coordinate(job, process_config, repo)

        # emulate-ref-only never marks a downgrade: value was never an option.
        persisted = await repo.get(JOB_ID)
        assert persisted is not None and persisted.status_info is not None
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
        process_config = ProcessConfig.model_validate(
            {
                "id": "process",
                "transmission-mode-policy": "emulate-ref",
                "result-storage": "ldproxy",
            }
        )

        await coordinator.coordinate(job, process_config, repo)

        persisted = await repo.get(JOB_ID)
        assert persisted is not None and persisted.status_info is not None
        assert persisted.stored_outputs is not None
        assert persisted.status_info.transmissionModeApplied is None
        assert persisted.stored_outputs["voronoi"]["items_url"] == (
            "https://geo/collections/x/items"
        )


class TestMixedTransmissionModes:
    """emulate-ref with a mix of reference and value outputs in one request.

    Regression guard for the voronoi bug: a value-requested output whose media
    type is not storable (e.g. application/json) must NOT enter the store batch
    and therefore must NOT downgrade the whole job. Only the reference-requested
    geospatial output is stored; the value output is served inline by GET
    /results (tested separately in TestGetResultsDocument).
    """

    @pytest.mark.asyncio
    async def test_value_output_does_not_break_reference_store(self):
        job = _job(
            outputs_spec={
                "voronoi_diagram": {"transmissionMode": "reference"},
                "classification_breaks_wb": {"transmissionMode": "value"},
                "classification_breaks_ma": {"transmissionMode": "value"},
            },
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        # Capture what actually gets handed to the store. Only the reference
        # output must appear here — never the application/json value outputs.
        stored_payloads: list[ResultPayload] = []

        async def _store(job_id, payloads):
            stored_payloads.extend(payloads)
            return [
                StoredReference(
                    collection_id=f"{job_id}-voronoi_diagram",
                    collection_url="https://geo/collections/v",
                    items_url="https://geo/collections/v/items",
                )
            ]

        storage = Mock()
        storage.exists = AsyncMock(return_value=False)
        storage.store = AsyncMock(side_effect=_store)

        # The remote document response carries all three outputs — one
        # geospatial FeatureCollection and two non-geospatial JSON tables.
        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(
                (
                    b'{"voronoi_diagram": {"type": "FeatureCollection", '
                    b'"features": []}, '
                    b'"classification_breaks_wb": {"value": [1, 2, 3], '
                    b'"mediaType": "application/json"}, '
                    b'"classification_breaks_ma": {"value": [4, 5, 6], '
                    b'"mediaType": "application/json"}}'
                ),
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
        process_config = ProcessConfig.model_validate(
            {
                "id": "process",
                "transmission-mode-policy": "emulate-ref",
                "result-storage": "ldproxy",
            }
        )

        await coordinator.coordinate(job, process_config, repo)

        # Exactly one output stored — the reference-requested geospatial one.
        assert [p.output_id for p in stored_payloads] == ["voronoi_diagram"]

        persisted = await repo.get(JOB_ID)
        # No downgrade: the value outputs never entered the batch, so the
        # reference output stored cleanly.
        assert persisted is not None and persisted.status_info is not None
        assert persisted.stored_outputs is not None
        assert persisted.status_info.transmissionModeApplied is None
        assert persisted.stored_outputs["voronoi_diagram"]["items_url"] == (
            "https://geo/collections/v/items"
        )


def _job_manager(providers, http_client, repo) -> JobManager:
    class _NoOpValidator(ProcessIdValidatorPort):
        def validate(self, process_id_with_prefix: str) -> bool:
            return True

        def extract(self, process_id_with_prefix: str) -> tuple[str, str]:
            prefix, _, rest = process_id_with_prefix.partition(":")
            return prefix, rest

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
            return_value=ProcessConfig.model_validate(
                {
                    "id": "process",
                    "transmission-mode-policy": "emulate-ref-only",
                    "result-storage": "ldproxy",
                }
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


# ---------------------------------------------------------------------------
# 4. Post-success results-fetch race (404 / transient) is retried
# ---------------------------------------------------------------------------


class TestResultsFetchRetry:
    """A remote can report ``successful`` a beat before ``/results`` is
    queryable. The storage fetch must retry transient failures rather than
    immediately downgrade a reference output to an inline value."""

    def _providers(self):
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )
        return providers

    def _ogc_error(self, status: int) -> OGCProcessException:
        return OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Upstream HTTP Error",
                status=status,
                detail=f"The remote service returned HTTP {status}.",
                instance=None,
            )
        )

    @pytest.mark.asyncio
    async def test_transient_404_is_retried_then_succeeds(self, monkeypatch):
        # No real waiting between attempts.
        monkeypatch.setattr(
            "ump.core.services.result_storage_coordinator.asyncio.sleep",
            AsyncMock(),
        )
        http_client = Mock()
        http_client.get_content = AsyncMock(
            side_effect=[
                self._ogc_error(404),
                self._ogc_error(404),
                (
                    b'{"voronoi": {"type": "FeatureCollection", "features": []}}',
                    "application/json",
                ),
            ]
        )
        coordinator = _coordinator(http_client=http_client, providers=self._providers())
        job = _job(outputs_spec={"voronoi": {"transmissionMode": "reference"}})

        body, content_type = await coordinator._fetch_results_with_retry(
            "https://remote.example.com/jobs/r/results", None, job.id
        )

        assert content_type == "application/json"
        assert b"FeatureCollection" in body
        assert http_client.get_content.await_count == 3

    @pytest.mark.asyncio
    async def test_non_transient_error_is_not_retried(self, monkeypatch):
        sleep = AsyncMock()
        monkeypatch.setattr(
            "ump.core.services.result_storage_coordinator.asyncio.sleep", sleep
        )
        http_client = Mock()
        http_client.get_content = AsyncMock(side_effect=self._ogc_error(400))
        coordinator = _coordinator(http_client=http_client, providers=self._providers())

        with pytest.raises(ResultStorageError):
            await coordinator._fetch_results_with_retry(
                "https://remote.example.com/jobs/r/results", None, "job-x"
            )

        assert http_client.get_content.await_count == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhausted_retries_raise_result_storage_error(self, monkeypatch):
        monkeypatch.setattr(
            "ump.core.services.result_storage_coordinator.asyncio.sleep",
            AsyncMock(),
        )
        http_client = Mock()
        http_client.get_content = AsyncMock(side_effect=self._ogc_error(404))
        coordinator = _coordinator(http_client=http_client, providers=self._providers())

        with pytest.raises(ResultStorageError):
            await coordinator._fetch_results_with_retry(
                "https://remote.example.com/jobs/r/results", None, "job-x"
            )

        assert http_client.get_content.await_count == 8


# ---------------------------------------------------------------------------
# 5. Option A: a failed required store errors on GET /results (no value)
# ---------------------------------------------------------------------------


class TestRequiredStoreFailureErrorsOnResults:
    @pytest.mark.asyncio
    async def test_storage_failed_marker_yields_error_not_value(self):
        """A successful job carrying the storage-failure marker must surface a
        502 error rather than proxying the (potentially huge) inline value."""
        from ump.core.exceptions import OGCProcessException

        job = _job(stored_outputs=None)
        job.diagnostic = f"{Job.RESULT_STORAGE_FAILED_MARKER}: disk full"
        repo = InMemoryJobRepository()
        await repo.create(job)

        # If the proxy path were taken, this would return a value — it must not.
        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(b"HUGE-VALUE-PAYLOAD", "application/json")
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        manager = _job_manager(providers, http_client, repo)

        with pytest.raises(OGCProcessException) as excinfo:
            await manager.get_results(JOB_ID)

        assert excinfo.value.response.status == 502
        # The remote value channel must never be touched.
        http_client.get_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_successful_job_without_marker_still_proxies(self):
        """A successful job without the marker keeps proxying as before."""
        job = _job(stored_outputs=None)
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(b"raw-value", "application/json")
        )
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )

        manager = _job_manager(providers, http_client, repo)
        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        assert result["body_bytes"] == b"raw-value"


# ---------------------------------------------------------------------------
# 6. Option A: required store still in progress -> finalizing hint, not proxy
# ---------------------------------------------------------------------------


# 6. V-11 superseded the "Results Finalizing" 503 hint: a job can no longer be
# `successful` while its required stored reference is still being confirmed
# live -- see ResultStorageObserver._finalize_publication and
# JobManager._process_status_update in job_manager.py. The two scenarios that
# used to assert a 503 here have been removed as unreachable; see
# tests/test_observers.py::TestResultStorageObserver for the V-11 gated
# success/failure coverage. The remaining tests below cover cases that stay
# reachable regardless of V-11 (value-requested proxy, stored_outputs
# precedence).
# ---------------------------------------------------------------------------


class TestRequiredStorePendingFinalizingHint:
    """Remaining non-pending-hint coverage for the results proxy path."""

    def _providers(self, policy: str):
        providers = Mock()
        providers.get_provider = Mock(
            return_value=Mock(url="https://remote.example.com")
        )
        providers.get_process_config = Mock(
            return_value=ProcessConfig.model_validate(
                {
                    "id": "process",
                    "transmission-mode-policy": policy,
                    "result-storage": "ldproxy",
                }
            )
        )
        return providers

    @pytest.mark.asyncio
    async def test_emulate_ref_value_requested_still_proxies(self):
        """emulate-ref where the client asked for VALUE is not a required
        store, so it must keep proxying (no finalizing hint)."""
        job = _job(
            stored_outputs=None,
            outputs_spec={"voronoi": {"transmissionMode": "value"}},
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            return_value=(b"raw-value", "application/json")
        )
        manager = _job_manager(self._providers("emulate-ref"), http_client, repo)

        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
        assert result["body_bytes"] == b"raw-value"

    @pytest.mark.asyncio
    async def test_stored_outputs_present_takes_precedence_over_pending(self):
        """Once stored_outputs is populated, the document path wins — the
        pending hint must not fire."""
        job = _job(
            stored_outputs={
                "voronoi": {
                    "collection_id": f"{JOB_ID}-voronoi",
                    "collection_url": "https://geo/collections/x",
                    "items_url": "https://geo/collections/x/items",
                }
            },
        )
        repo = InMemoryJobRepository()
        await repo.create(job)

        http_client = Mock()
        http_client.get_content = AsyncMock(
            side_effect=AssertionError(
                "emulate-ref-only must not fetch remote for a stored job"
            )
        )
        manager = _job_manager(self._providers("emulate-ref-only"), http_client, repo)

        result = await manager.get_results(JOB_ID)

        assert result["status"] == 200
