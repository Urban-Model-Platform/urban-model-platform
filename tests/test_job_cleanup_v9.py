"""V-9: JobCleanupService, PeriodicTaskRunner, and repository list_expired/delete.

Covers the generic cleanup scheduler introduced in V-9:
  - InMemoryJobRepository.list_expired / .delete
  - JobCleanupService.run_once (cutoff computation, best-effort storage delete)
  - PeriodicTaskRunner start/stop lifecycle and resilience to task failures

SQLModelJobRepository.list_expired/.delete are not covered here: that adapter
uses PostgreSQL-only JSONB columns and requires a real Postgres instance (no
sqlite-compatible fixture exists in this suite for it), consistent with the
rest of the test suite never exercising SQLModelJobRepository directly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.periodic_task_runner import PeriodicTaskRunner
from ump.core.interfaces.result_storage import ResultStorageError, ResultStoragePort
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.services.job_cleanup_service import JobCleanupService


def _job(
    job_id: str,
    *,
    user_id: Optional[str],
    status: StatusCode,
    finished: Optional[datetime],
) -> Job:
    job = Job(id=job_id, user_id=user_id)
    status_info = JobStatusInfo(jobID=job_id, status=status, finished=finished)
    job.apply_status_info(status_info)
    return job


NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(hours=10)
RECENT = NOW - timedelta(minutes=1)


class TestInMemoryListExpired:
    @pytest.mark.asyncio
    async def test_both_cutoffs_none_returns_empty(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("j1", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        assert await repo.list_expired() == []

    @pytest.mark.asyncio
    async def test_anonymous_only_cutoff(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("anon-old", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        await repo.create(
            _job(
                "anon-recent",
                user_id=None,
                status=StatusCode.successful,
                finished=RECENT,
            )
        )
        await repo.create(
            _job(
                "auth-old", user_id="alice", status=StatusCode.successful, finished=OLD
            )
        )

        expired = await repo.list_expired(anonymous_cutoff=NOW - timedelta(hours=1))
        assert [j.id for j in expired] == ["anon-old"]

    @pytest.mark.asyncio
    async def test_authenticated_only_cutoff(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("anon-old", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        await repo.create(
            _job(
                "auth-old", user_id="alice", status=StatusCode.successful, finished=OLD
            )
        )

        expired = await repo.list_expired(authenticated_cutoff=NOW - timedelta(hours=1))
        assert [j.id for j in expired] == ["auth-old"]

    @pytest.mark.asyncio
    async def test_both_cutoffs_set(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("anon-old", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        await repo.create(
            _job(
                "auth-old", user_id="alice", status=StatusCode.successful, finished=OLD
            )
        )

        expired = await repo.list_expired(
            anonymous_cutoff=NOW - timedelta(hours=1),
            authenticated_cutoff=NOW - timedelta(hours=1),
        )
        assert {j.id for j in expired} == {"anon-old", "auth-old"}

    @pytest.mark.asyncio
    async def test_non_terminal_jobs_excluded(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("running", user_id=None, status=StatusCode.running, finished=None)
        )
        expired = await repo.list_expired(anonymous_cutoff=NOW + timedelta(days=1))
        assert expired == []

    @pytest.mark.asyncio
    async def test_terminal_but_not_finished_yet_excluded(self):
        repo = InMemoryJobRepository()
        # Defensive case: terminal status but no finished timestamp recorded.
        job = Job(id="weird", user_id=None)
        job.apply_status_info(
            JobStatusInfo(jobID="weird", status=StatusCode.failed, finished=None)
        )
        await repo.create(job)
        expired = await repo.list_expired(anonymous_cutoff=NOW + timedelta(days=1))
        assert expired == []


class TestInMemoryDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_job(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("j1", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        await repo.delete("j1")
        assert await repo.get("j1") is None

    @pytest.mark.asyncio
    async def test_delete_is_idempotent(self):
        repo = InMemoryJobRepository()
        await repo.delete("does-not-exist")  # must not raise


class _FakeResultStorage(ResultStoragePort):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deleted: list[str] = []

    async def store(self, job_id, payloads):  # pragma: no cover - unused here
        raise NotImplementedError

    async def exists(self, job_id: str) -> bool:  # pragma: no cover - unused here
        return False

    async def delete(self, job_id: str) -> None:
        if self.fail:
            raise ResultStorageError("simulated storage failure")
        self.deleted.append(job_id)


class TestJobCleanupService:
    @pytest.mark.asyncio
    async def test_both_retentions_disabled_is_noop(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("j1", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        storage = _FakeResultStorage()
        service = JobCleanupService(
            job_repo=repo,
            result_storage=storage,
            anonymous_retention_minutes=None,
            authenticated_retention_minutes=None,
        )
        removed = await service.run_once()
        assert removed == 0
        assert await repo.get("j1") is not None

    @pytest.mark.asyncio
    async def test_deletes_expired_anonymous_job(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("anon-old", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        await repo.create(
            _job(
                "anon-recent",
                user_id=None,
                status=StatusCode.successful,
                finished=RECENT,
            )
        )
        storage = _FakeResultStorage()
        service = JobCleanupService(
            job_repo=repo,
            result_storage=storage,
            anonymous_retention_minutes=60,
            authenticated_retention_minutes=None,
        )
        removed = await service.run_once()
        assert removed == 1
        assert await repo.get("anon-old") is None
        assert await repo.get("anon-recent") is not None
        assert storage.deleted == ["anon-old"]

    @pytest.mark.asyncio
    async def test_storage_failure_does_not_block_repo_delete(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job("anon-old", user_id=None, status=StatusCode.successful, finished=OLD)
        )
        storage = _FakeResultStorage(fail=True)
        service = JobCleanupService(
            job_repo=repo,
            result_storage=storage,
            anonymous_retention_minutes=60,
            authenticated_retention_minutes=None,
        )
        removed = await service.run_once()
        assert removed == 1
        assert await repo.get("anon-old") is None  # deleted despite storage failure

    @pytest.mark.asyncio
    async def test_authenticated_job_kept_by_default(self):
        repo = InMemoryJobRepository()
        await repo.create(
            _job(
                "auth-old", user_id="alice", status=StatusCode.successful, finished=OLD
            )
        )
        storage = _FakeResultStorage()
        service = JobCleanupService(
            job_repo=repo,
            result_storage=storage,
            anonymous_retention_minutes=60,
            authenticated_retention_minutes=None,  # default: never delete
        )
        removed = await service.run_once()
        assert removed == 0
        assert await repo.get("auth-old") is not None


class TestPeriodicTaskRunner:
    @pytest.mark.asyncio
    async def test_calls_task_repeatedly(self):
        calls = 0

        async def task() -> None:
            nonlocal calls
            calls += 1

        runner = PeriodicTaskRunner(task=task, interval_seconds=0.01, name="test")
        runner.start()
        await asyncio.sleep(0.05)
        await runner.stop()
        assert calls >= 2

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        calls = 0

        async def task() -> None:
            nonlocal calls
            calls += 1

        runner = PeriodicTaskRunner(task=task, interval_seconds=0.05, name="test")
        runner.start()
        runner.start()  # second call must be a no-op, not spawn a second loop
        await asyncio.sleep(0.02)
        await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        async def task() -> None:
            pass

        runner = PeriodicTaskRunner(task=task, interval_seconds=1, name="test")
        await runner.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_exception_in_one_cycle_does_not_kill_loop(self):
        calls = 0

        async def task() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")

        runner = PeriodicTaskRunner(task=task, interval_seconds=0.01, name="test")
        runner.start()
        await asyncio.sleep(0.05)
        await runner.stop()
        assert calls >= 2  # loop survived the first cycle's exception
