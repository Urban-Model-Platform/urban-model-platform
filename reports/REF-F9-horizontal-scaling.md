_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature IX: Horizontal scaling — multi-instance poll coordination 🔲

## Problem statement

UMP is designed to be horizontally scaled (≥2 instances behind a load balancer or in a Kubernetes deployment). The current polling architecture has two failure modes that make multi-instance deployment unsafe:

**Failure mode 1 — Poll orphaning (silent, high severity)**

`_poll_tasks` and `_active_poll_jobs` are plain Python in-memory sets scoped to one process. When Instance A creates job X and starts a poll loop, then Instance A restarts (rolling deploy, OOM kill, scale-down), no other instance picks up the orphaned poll. The database row sits at `status=running` with a populated `remote_status_url` indefinitely. The job never reaches a terminal state.

**Failure mode 2 — Duplicate polling (load amplification + data races)**

`_active_poll_jobs` only deduplicates within one process. Two instances can both start polling the same job (e.g., during a rolling deploy where old and new instances overlap). Both will:
- Fetch the remote status endpoint simultaneously (2× load on the model server)
- Both call `repo.update()` — the current implementation is a blind overwrite (`setattr` on all fields). `Job.version` exists in the DB schema and domain model but is not enforced in `UPDATE` conditions, so the last writer silently wins, potentially discarding an intermediate status snapshot.
- Both may call `StatusHistoryObserver.on_status_changed` for the same transition, writing duplicate rows to `job_status_history`.

## What works correctly at ≥2 instances today

| Concern | Safe? | Reason |
|---|---|---|
| Job creation (`POST /execution`) | ✅ | UUID generation is instance-local; DB insert is atomic |
| Job reads (`GET /jobs`, `GET /jobs/{id}`) | ✅ | All instances read from the shared DB |
| Auth / JWKS cache | ✅ | Per-instance cache; fetches are idempotent |
| Process cache | ✅ | Cache miss → extra remote fetch; no correctness issue |
| Provider config file watcher | ✅ | Each instance watches independently |
| `status_history` writes | ⚠️ | Duplicate polling → duplicate history rows for the same transition |
| `repo.update()` under concurrency | ⚠️ | Last-writer-wins; `version` field not enforced |

## Solution: PostgreSQL-native coordination (no new infrastructure)

Both problems are solved using facilities already in the database.

---

### Fix 1 — Poll recovery on startup

When the FastAPI lifespan starts, scan for non-terminal jobs with a `remote_status_url` and re-schedule their poll loops. This ensures any job that was being polled by a now-dead instance is immediately adopted by the starting instance.

```python
# in asgi.py lifespan, after wiring adapters:
async def _recover_orphaned_polls(job_repo: JobRepositoryPort, schedule_poll) -> None:
    """Re-schedule poll loops for running jobs left over from a crashed instance."""
    terminal = {str(StatusCode.successful), str(StatusCode.failed), str(StatusCode.dismissed)}
    all_jobs = await job_repo.list()
    for job in all_jobs:
        if job.status not in terminal and job.remote_status_url:
            schedule_poll(job.id)
```

This is safe to run on every startup: `_schedule_poll` already guards against duplicate loops within the same instance via `_active_poll_jobs`; the DB advisory lock (Fix 2) guards against duplicate loops across instances.

---

### Fix 2 — PostgreSQL advisory lock for poll-loop exclusivity

Wrap `_poll_loop` in a non-blocking PostgreSQL advisory lock so that only one instance polls any given job at a time. The lock is released automatically when the loop exits (either normally or via exception).

Advisory locks are session-scoped and disappear with the DB connection, so a crashed instance automatically releases its locks — no cleanup required.

The lock key is derived from the job UUID: `hashtext(job_id)` maps it to the 32-bit integer required by `pg_try_advisory_xact_lock`. For the in-memory repository (tests and development) the lock is a no-op.

```python
# New port in src/ump/core/interfaces/poll_lock.py
from abc import ABC, abstractmethod

class PollLockPort(ABC):
    @abstractmethod
    async def try_acquire(self, job_id: str) -> bool:
        """Attempt to acquire exclusive poll ownership. Returns False if already held."""
        ...

    @abstractmethod
    async def release(self, job_id: str) -> None: ...
```

Two adapters:

```python
# src/ump/adapters/poll_lock_noop.py — for in-memory/test usage
class NoOpPollLock(PollLockPort):
    async def try_acquire(self, job_id: str) -> bool: return True
    async def release(self, job_id: str) -> None: pass

# src/ump/adapters/poll_lock_pg.py — for postgres usage
class PgAdvisoryPollLock(PollLockPort):
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def try_acquire(self, job_id: str) -> bool:
        key = _job_id_to_lock_key(job_id)   # hashtext equivalent
        async with self._session_factory() as session:
            row = await session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            return row.scalar()

    async def release(self, job_id: str) -> None:
        key = _job_id_to_lock_key(job_id)
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
```

`_poll_loop` in `JobManager` becomes:

```python
async def _poll_loop(self, job_id: str) -> None:
    if self._poll_lock and not await self._poll_lock.try_acquire(job_id):
        logger.debug(f"[job:poll] lock held by another instance, skipping job_id={job_id}")
        return
    try:
        while not self._shutdown:
            should_stop, reason = await self._should_stop_polling(job_id)
            if should_stop:
                logger.debug(f"[job:poll] stopping: {reason} job_id={job_id}")
                return
            ...
    finally:
        if self._poll_lock:
            await self._poll_lock.release(job_id)
```

---

### Fix 3 — Optimistic locking in `repo.update()`

`Job.version` already exists as a DB column and domain field. Enforce it in the SQL adapter:

```python
async def update(self, job: Job) -> Job:
    job.touch()
    async with self._session_factory() as session:
        result = await session.execute(
            update(JobRecord)
            .where(JobRecord.id == job.id, JobRecord.version == job.version)
            .values(**_job_to_values(job), version=job.version + 1)
            .returning(JobRecord)
        )
        row = result.one_or_none()
        if row is None:
            raise OptimisticLockError(f"Job {job.id} was modified by another instance")
        await session.commit()
        return row.to_domain()
```

Callers (mainly `_process_status_update`) should handle `OptimisticLockError` by re-reading the job from the DB and retrying the update. Because poll loops sleep between iterations this race is rare in practice, but the version guard ensures no update is silently lost.

---

## Implementation plan

| # | Task | Effort | Dependency |
|---|---|---|---|
| 1 | `PollLockPort` interface + `NoOpPollLock` adapter | XS | — |
| 2 | `PgAdvisoryPollLock` adapter (uses existing session factory) | S | 1 |
| 3 | Inject `_poll_lock: PollLockPort` into `JobManager`; use in `_poll_loop` | S | 1 |
| 4 | `_recover_orphaned_polls` helper; call from asgi.py lifespan after DB ready | S | — |
| 5 | Optimistic locking in `SQLModelJobRepository.update()` + `OptimisticLockError` exception | S | — |
| 6 | Retry on `OptimisticLockError` in `_process_status_update` | XS | 5 |
| 7 | Unit tests: lock not acquired → poll skipped; startup recovery schedules orphaned jobs | M | 1–4 |
| 8 | Integration test: two `JobManager` instances share a repo; only one polls | M | 1–3 |

Steps 1–4 resolve the critical orphaning and duplicate-polling problems. Steps 5–6 close the last-writer-wins race. Steps 7–8 give regression coverage.

## Files to create / modify

| File | Change |
|---|---|
| `src/ump/core/interfaces/poll_lock.py` | CREATE — `PollLockPort` ABC |
| `src/ump/core/exceptions.py` | ADD — `OptimisticLockError` |
| `src/ump/adapters/poll_lock_noop.py` | CREATE — `NoOpPollLock` (default / tests) |
| `src/ump/adapters/poll_lock_pg.py` | CREATE — `PgAdvisoryPollLock` |
| `src/ump/core/managers/job_manager.py` | MODIFY — accept `poll_lock`, guard `_poll_loop` |
| `src/ump/adapters/job_repository_sql.py` | MODIFY — optimistic locking in `update()` |
| `src/ump/asgi.py` | MODIFY — instantiate lock, call recovery helper in lifespan |
| `tests/test_poll_coordination.py` | CREATE |

## Non-goals

- **Redis / external lock store** — not required; PostgreSQL advisory locks are sufficient and avoid adding a new infrastructure dependency.
- **Single-owner job routing** (always route a job's requests to the same instance) — rejected; it ties scaling to sticky sessions and breaks when instances restart.
- **Distributed consensus (Raft, ZooKeeper)** — massively over-engineered for this use case.
