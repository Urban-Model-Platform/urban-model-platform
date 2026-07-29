_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature III: Execution proxy, Jobs, and Persistence ✅ (core complete)

The goal of Feature III is to enable UMP to act as an OGC API Processes execution proxy: forwarding execution requests to remote model servers, maintaining a local federated job registry with full status lifecycle, and persisting jobs durably in PostgreSQL.

**Feature III is functionally complete for the core use case.** The remaining items are refinements and extensions.

## Quick status

| Area | Status |
|---|---|
| Job model, ports, in-memory repo | ✅ |
| JobManager: forwarding, status derivation, polling, retry, timeout | ✅ |
| ExecuteRequest structural validation (raw body forwarded unchanged) | ✅ |
| /jobs, /jobs/{id}, /jobs/{id}/results routes | ✅ |
| POST /processes/{id}/execution route | ✅ |
| SQLModel JobRepository + Alembic migration | ✅ |
| Observer pattern (history, polling scheduler, results verification) | ✅ |
| Poll fan-out bug fixed (notify only on real status transitions) | ✅ |
| Results proxy: content-type-aware, binary-safe (`get_content`) | ✅ |
| /jobs/{id}/inputs endpoint | 🔲 |
| Status history reads (DB writes exist; no read endpoint yet) | 🔲 |
| Expanded test coverage | ⚠️ retry-exhaustion path missing |
| ResultStoragePort placeholder injection | ✅ |
| Large-object input separation | ✅ rejected (OGC transmissionMode governs href-ing) |

## ✅ What is implemented

**Domain models**

- `Job` (`src/ump/core/models/job.py`): `id` (local UUID), `process_id`, `provider`, `remote_job_id`, `remote_status_url`, timestamps, `status`, `status_info` snapshot, inline `inputs`, `inputs_url`, `links`, `diagnostic`, `version`. Helper methods: `apply_status_info()`, `touch()`, `is_in_terminal_state()`. ID separation rationale documented in code (local UUID / remote id / public route id are kept distinct).
- `JobStatusInfo` / `StatusCode`: mirrors OGC `statusInfo.yaml` schema.
- `ExecuteRequest` (`src/ump/core/models/execute_request.py`): `from_raw()` performs structural validation only (href is a URL, each input has `value` or `href`). It does **not** mutate its argument; the original raw body is forwarded to the remote server unchanged. Dead code removed: `as_provider_payload()`, `normalized_inputs()`, `NormalizedInput`.

**Ports**

- `JobRepositoryPort` (`src/ump/core/interfaces/job_repository.py`): `create`, `get`, `update`, `list`, `mark_failed`, `append_status`, `append_event`.
- `JobStateObserver` (`src/ump/core/interfaces/observers.py`): `on_job_created`, `on_status_changed`, `on_job_completed`.

**Adapters**

- `InMemoryJobRepository` — async-safe, thread-safe, with optional JSON dump to `UMP_JOB_DUMP_DIR`. Used by default and in all tests.
- `SQLModelJobRepository` — PostgreSQL-backed via asyncpg. Selected when `UMP_JOB_STORE=postgres`. Uses two-model ORM pattern (see persistence notes below).

**JobManager** (`src/ump/core/managers/job_manager.py`)

Orchestrates the full async execution lifecycle via `run_execution_pipeline`:
1. Resolve provider from `process_id` (with prefix extraction).
2. Create local job immediately with `accepted` statusInfo snapshot; persist.
3. Forward execute request to remote provider with retry/backoff (`TenacityRetryAdapter`).
4. Derive initial `StatusInfo` from provider response via `StatusDerivationOrchestrator` (strategy pattern: direct body / Location header follow-up / immediate results fallback / failed).
5. Normalise remote job ID to local UUID; enrich missing timestamps and progress.
6. Finalize: persist derived status, notify observers.
7. Schedule background polling loop if job is non-terminal and has `remote_status_url`. Polling stops on terminal state, TTW timeout (`UMP_REMOTE_JOB_TTW`), or graceful shutdown.

Error handling: transport errors, upstream 4xx/5xx, missing statusInfo, and TTW timeout are all normalized into `failed` snapshots with diagnostic messages. Returns HTTP 201 with `Location: /jobs/{local_id}` in all cases.

**Observer pattern** (`src/ump/core/managers/observers.py`):
- `StatusHistoryObserver` — calls `repo.append_status()` on every status transition (writes to `job_status_history` table in postgres).
- `PollingSchedulerObserver` — calls `_schedule_poll()` when a non-terminal job needs polling.
- `ResultsVerificationObserver` — attempts to fetch remote results for immediate-success jobs; logs a warning on failure (best-effort, does not downgrade job status).

**Routes** (on parent app and each versioned sub-app):
- `POST /processes/{id}/execution` → `JobManager.run_execution_pipeline`
- `GET /jobs` → list all jobs (from repo)
- `GET /jobs/{id}` → current `statusInfo` snapshot
- `GET /jobs/{id}/results` → remote results proxy; forwards the remote's `Content-Type` verbatim via `get_content()`; handles JSON, binary, and multipart responses correctly.

**Link normalization**: always inject local `self` link with stable UUID; add `results` link on success; filter out any remote self/results links that contain foreign job identifiers.

**Lifecycle sequence (happy path)**:
1. `POST /processes/{id}/execution` with raw JSON body.
2. Web adapter parses body; `ExecuteRequest.from_raw` normalizes.
3. `ProcessManager.execute_process` delegates to `JobManager.run_execution_pipeline`.
4. Job created locally (status=accepted); forwarded to provider.
5. StatusInfo derived; job updated (running/successful/failed).
6. Polling scheduled if non-terminal.
7. Returns HTTP 201 + `Location` header + current statusInfo body.

**ID strategy** (documented in code):
- Local UUID: internal canonical key; always used for public routes.
- Remote job id: stored for correlation/polling; never exposed externally.
- Public route id = local UUID (no leakage of provider semantics).

## ✅ Persistence layer (SQLModel + Alembic)

**Two-model ORM pattern** (hexagonal-correct): the core `Job` (`BaseModel`) stays pure Pydantic with no ORM annotations. The adapter owns separate ORM classes:

```
src/ump/core/models/job.py                 ← domain model (pure Pydantic)
src/ump/adapters/sqlmodel_job_repository.py
  ├── JobRecord(SQLModel, table=True)              ← ORM model
  ├── JobStatusHistoryRecord(SQLModel, table=True) ← history ORM model
  └── SQLModelJobRepository(JobRepositoryPort)
        ├── JobRecord.from_domain(job) -> JobRecord
        └── JobRecord.to_domain()     -> Job
```

The core never imports `sqlmodel` or `sqlalchemy`. The `from_domain`/`to_domain` bridge is the only contact point.

**JSONB columns**: `status_info`, `inputs`, and `links` use `sa_column=Column(JSONB)` — without this SQLModel defaults to TEXT, losing native JSONB operators.

**DB schema**:
```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    process_id TEXT, provider TEXT, remote_job_id TEXT, remote_status_url TEXT,
    status TEXT,              -- denormalized for WHERE filters
    created TIMESTAMPTZ NOT NULL, updated TIMESTAMPTZ,
    status_info JSONB,        -- current OGC statusInfo snapshot
    inputs JSONB,             -- inline inputs (small payloads only)
    inputs_url TEXT, inputs_storage TEXT, inputs_size INT, inputs_checksum TEXT,
    links JSONB,              -- list[Link]
    diagnostic TEXT, version INT NOT NULL DEFAULT 0
);
CREATE TABLE job_status_history (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq INT NOT NULL, snapshot JSONB NOT NULL, recorded_at TIMESTAMPTZ NOT NULL
);
-- Indexes: status, provider, process_id, job_id+seq
```

**Alembic**: `env.py` reads `UMP_DATABASE_URL` or constructs from individual `UMP_DATABASE_*` vars. Strips `asyncpg` prefix for sync Alembic engine. Run migrations with:
```bash
ump-migrate                  # uses env vars
ump-migrate downgrade -1     # any alembic subcommand
```

**Adapter selection** via `UMP_JOB_STORE`:
- `memory` (default) — `InMemoryJobRepository`, no DB required, used in all tests.
- `postgres` — `SQLModelJobRepository`, requires `UMP_DATABASE_URL`.

**Session management**: async session factory injected at construction in `main.py`; each repository method opens its own session context (no session leaks).

## 🔲 Remaining work

1. `/jobs/{id}/inputs` — inputs are stored but never exposed via a dedicated route.
2. **Status history endpoint** — `job_status_history` table receives writes via `StatusHistoryObserver`, but no endpoint exposes the history. Add `GET /jobs/{id}/history` or include history in the job detail response.
3. **Test coverage gap** — retry-exhaustion path (forward retries exhausted → `failed` diagnostic) is not yet exercised. All other planned paths are covered: polling timeout ✅, immediate results ✅, link normalization ✅, results endpoint ✅, polling stop conditions ✅, auth/JWT ✅, job visibility ✅.
4. **Process-description-aware input validation (Feature X)** — `ExecuteRequest` validates structure only. A future `ValidateInputsStep` (opt-in via `UMP_VALIDATE_EXEC_REQUESTS=true`) should validate each input against its `ProcessInput.scheme: Schema` from the cached process description. Deferred because: (a) process description may not be cached yet; (b) `oneOf`/`anyOf` requires a JSON Schema evaluator; (c) operators may need lax mode for non-spec-compliant servers.

## Design notes: job history / CQRS decision

Chosen approach: CRUD `jobs` table + append-only `job_status_history` table (hybrid). This gives fast reads, simple writes, a full audit trail, and replay capability for most needs, without the complexity of full CQRS or event sourcing. Migration path to CQRS is available if/when advanced projections or heavy scaling become necessary.

### OGC schema reference

```yaml
# statusInfo.yaml (OGC API Processes)
type: object
required: [jobID, status, type]
properties:
  processID: {type: string}
  type: {type: string, enum: [process]}
  jobID: {type: string}
  status: {$ref: "statusCode.yaml"}
  message: {type: string}
  created: {type: string, format: date-time}
  started: {type: string, format: date-time}
  finished: {type: string, format: date-time}
  updated: {type: string, format: date-time}
  progress: {type: integer, minimum: 0, maximum: 100}
  links: {type: array, items: {$ref: "link.yaml"}}
```

```yaml
# JobList.yaml (OGC API Processes)
type: object
required: [jobs, links]
properties:
  jobs: {type: array, items: {$ref: "statusInfo.yaml"}}
  links: {type: array, items: {$ref: "link.yaml"}}
```

```yaml
# JobControlOptions.yaml (OGC API Processes)
type: string
enum: [sync-execute, async-execute, dismiss]
```

Deferred execution modes (all belong in Feature VI pipeline implementation):
- **Sync execute** (`Prefer: respond-sync` or absent `Prefer`) — see Feature VI for design
- Transmission direct / local-by-ref
- Streaming results to clients

## Extended implementation notes

2. `JobRepositoryPort` (`src/ump/core/interfaces/job_repository.py`) and in-memory adapter `InMemoryJobRepository` for fast TDD; ready to swap with SQLModel adapter later.
3. `JobManager` (`src/ump/core/managers/job_manager.py`): orchestrates `run_execution_pipeline` by:
   - Creating local job immediately.
   - Forwarding execute request downstream.
   - Capturing statusInfo directly from provider body OR following a `Location` header to GET remote status when needed.
   - Normalizing failures (transport/error, missing statusInfo) into terminal `failed` snapshots.
   - Scheduling background polling tasks for remote jobs until a terminal state is reached (interval from `UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL`).
   - Graceful shutdown via `shutdown()` awaited in FastAPI lifespan.
4. `ExecuteRequest` model (`src/ump/core/models/execute_request.py`) with rich normalization (`from_raw`) moving coercion out of web adapter. Transmission modes, inline/ref inputs, outputs, response preference, subscriber callbacks all normalized centrally.
5. Process execution delegation: `ProcessManager.execute_process` now delegates entirely to `JobManager.create_and_forward` (adapter route calls ProcessManager, which no longer contains forwarding logic itself).
6. Link & metadata leniency: handler pipeline in `ProcessManager` now includes `_handle_fill_defaults` (injects `version`, `jobControlOptions`, `outputTransmission`, minimal self link) and `_handle_sanitize_metadata` (drops malformed metadata dicts). This ensures partially non-spec processes are still exposed.
7. Per-process fetch strategy is now the default: the previous `UMP_PER_PROCESS_FETCH` toggle was removed; we always fetch each configured process individually for richer metadata.
8. Logger decoupling: core no longer directly imports `LoggingAdapter`; `main.py` acts as composition root and injects logging via `set_logger` before building factories handed to the FastAPI adapter.
9. Composition root refactor: `asgi.py` now wires all concrete adapters (providers, HTTP client, repository, process id validator, logging) and passes factories to `create_app`. Web adapter no longer instantiates infra objects. `main.py` became the entrypoint for development
10. Remote status polling: background tasks query `remote_status_url` until terminal state (success/failed/ dismissed etc.) then stop; tasks are tracked for cleanup.
11. Results endpoint `/jobs/{job_id}/results` added (remote-only proxy, no local persistence yet) returning provider results; 404 if job not successful.
12. Retry adapter (Tenacity-based `RetryPort`) integrated into remote results verification for immediate-success jobs to handle transient availability gaps.
13. Poll timeout via new setting `UMP_REMOTE_JOB_TTW`; jobs exceeding this total wait time are marked failed with a diagnostic message.
14. Immediate results fallback: when provider ignores `Prefer` and returns a body containing `outputs` but no `statusInfo`, we synthesize a successful terminal `statusInfo` snapshot and skip polling.
15. Link normalization: always inject a local `self` link with the stable local UUID; add `results` link only upon success; filter out remote self/results links containing foreign job identifiers.
16. Local vs remote job ID handling simplified: remote job id captured (if present) but never exposed externally; all links and user-facing IDs use the local UUID.

## Lifecycle sequence (happy path)

1. HTTP POST hits `/processes/{process_id}/execution` with raw body.
2. Web adapter parses raw JSON; `ExecuteRequest.from_raw` normalizes inputs & outputs.
3. ProcessManager delegates to JobManager.
4. JobManager creates local job record (status=accepted) and persists.
5. Forwards execute to provider; captures body or follows `Location`.
6. Derives `StatusInfo` snapshot; updates job status (e.g. `running`, `finished`).
7. Starts polling task if remote job not terminal and remote status endpoint present.
8. Returns HTTP 201 with `Location: /jobs/{local_job_id}` and the current `statusInfo` snapshot body.

## Failure / edge handling

- Missing statusInfo & no Location: mark job `failed` with diagnostic message.
- Transport errors/timeouts: catch, mark failed, persist snapshot, still return 201 (client can inspect statusInfo for failure detail). Upstream HTTP codes mapped to OGC error responses when appropriate.
- Polling stops automatically on terminal state or shutdown.

## ID strategy

- Local UUID: internal canonical job primary key; stable for public routes.
- Remote job id: only stored when provider returns one; never replaces local id.
- Public job route id: same as local UUID (no leakage of remote semantics).
This separation avoids coupling deletion/retry semantics to remote provider identifiers and supports multi-provider orchestration.

## Normalization decisions

- Inputs kept outside `statusInfo` (OGC compliance); dedicated endpoint & storage separation pending (see remaining tasks).
- Default jobControlOptions/outputTransmission/version injected for sparse upstream process definitions.
- Malformed metadata safely ignored (logged debug).

## Design trade-offs accepted in Step 1

- Always async semantics (no sync shortcut yet) simplifies initial implementation; sync execute deferred.
- Polling interval is global; per-provider backoff not yet implemented.
- StatusInfo snapshots currently overwritten (history table planned to preserve transitions).
- Object storage integration postponed to keep test surface small.
