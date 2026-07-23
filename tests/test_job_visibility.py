"""Tests for user-scoped job visibility filtering in the job repository.

Feature IV attaches an owning ``user_id`` to each job (None = anonymous/public)
and the repository's ``list()`` enforces the visibility rules that the web
adapter relies on:

- unauthenticated caller  → sees only public jobs (public_only=True)
- authenticated caller    → sees own jobs, optionally plus public ones
"""

import pytest

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.core.models.job import Job


async def _seed(repo: InMemoryJobRepository) -> None:
    await repo.create(Job(id="pub-1", user_id=None, process_id="infra:echo"))
    await repo.create(Job(id="alice-1", user_id="alice", process_id="infra:echo"))
    await repo.create(Job(id="alice-2", user_id="alice", process_id="infra:square"))
    await repo.create(Job(id="bob-1", user_id="bob", process_id="infra:echo"))


def _ids(jobs) -> set[str]:
    return {j.id for j in jobs}


@pytest.mark.asyncio
async def test_public_only_returns_only_anonymous_jobs():
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(public_only=True)
    assert _ids(jobs) == {"pub-1"}


@pytest.mark.asyncio
async def test_user_scope_includes_public_by_default():
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(user_id="alice")
    assert _ids(jobs) == {"alice-1", "alice-2", "pub-1"}


@pytest.mark.asyncio
async def test_user_scope_can_exclude_public():
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(user_id="alice", include_public=False)
    assert _ids(jobs) == {"alice-1", "alice-2"}


@pytest.mark.asyncio
async def test_user_never_sees_another_users_jobs():
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(user_id="alice")
    assert "bob-1" not in _ids(jobs)


@pytest.mark.asyncio
async def test_public_only_takes_precedence_over_user_id():
    # public_only is the unauthenticated path; it must ignore any user_id
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(user_id="alice", public_only=True)
    assert _ids(jobs) == {"pub-1"}


@pytest.mark.asyncio
async def test_filters_compose_with_process_id():
    repo = InMemoryJobRepository()
    await _seed(repo)
    jobs = await repo.list(user_id="alice", process_id="infra:square")
    assert _ids(jobs) == {"alice-2"}
