# Refactoring status and next steps

This document captures where the refactor to a hexagonal architecture left off and what the next steps are. Use this as a personal reference and to inform you as the coding assistant.

## High-level goal

Refactor the Urban Model Platform (UMP) codebase to follow hexagonal architecture:
- Core (business logic) depends only on ports (interfaces).
- Adapters implement ports and are injected into the core.
- Keep web adapter, persistence, and other infra concerns outside of core.

## Current state (what's implemented)

- Provider config
  - `src/ump/adapters/provider_config_file_adapter.py` implemented
    - Atomic updates, file-watcher for configmap updates, thread-safe ModelServers store.
  - `ProvidersPort` interface in `src/ump/core/interfaces/providers.py` exists and adapter implements it.

- Process ID validation
  - `ProcessIdValidatorPort` added (`src/ump/core/interfaces/process_id_validator.py`).
  - `RegexProcessIdValidator` / `ColonProcessId` adapter implemented under `src/ump/adapters/`.
  - `ProcessManager` delegates pattern validation and creation to validator adapter.

- HTTP client
  - `HttpClientPort` interface added with `__aenter__` / `__aexit__` for async context manager.
  - `AioHttpClientAdapter` implemented to map remote errors to domain exceptions.

- ProcessManager
  - `src/ump/core/managers/process_manager.py` refactored:
    - Depends on `ProvidersPort`, `HttpClientPort`, and `ProcessIdValidatorPort`.
    - Implements `fetch_processes_for_provider(provider_name)` helper.
    - Implements `get_all_processes()` which runs per-provider fetches concurrently with `asyncio.gather()`.
    - Adds an in-memory per-provider cache via `ProcessCache` helper.

- Cache
  - Small `ProcessCache` class added to `src/ump/core/managers/process_cache.py` for expiry-based in-memory caching.

- Logging
  - `LoggingPort` and `LoggingAdapter` created.
  - A `logger` instance exposed via `src/ump/core/settings.py`.

- Web adapter
  - `src/ump/adapters/web/fastapi.py` adapted to accept dependencies and create `ProcessManager` in the lifespan using an injected `http_client`.

- Main entrypoint
  - `src/ump/main.py` wires provider config adapter, process manager and FastAPI adapter and starts Uvicorn.

## Files changed (key ones)

- `src/ump/core/interfaces/providers.py` (ProvidersPort)
- `src/ump/adapters/provider_config_file_adapter.py` (Provider config file adapter)
- `src/ump/core/interfaces/process_id_validator.py` (Process ID validator port)
- `src/ump/adapters/regex_process_id_validator.py` (validator implementation)
- `src/ump/core/interfaces/http_client.py` (HttpClientPort)
- `src/ump/adapters/aiohttp_client_adapter.py` (HTTP adapter)
- `src/ump/core/managers/process_manager.py` (ProcessManager with async fetching and cache)
- `src/ump/core/managers/process_cache.py` (ProcessCache)
- `src/ump/adapters/web/fastapi.py` (web adapter wiring)
- `src/ump/core/settings.py` (logger exposure)
- `src/ump/adapters/logging_adapter.py` (logging adapter)
- `src/ump/main.py` (app entrypoint)

## Outstanding issues / TODOs

### small changes
- Improve logging usage across modules (inject `logger` where useful).
- Links in fetched processes are now optionally rewritten to local API links. This is controlled by the setting `UMP_REWRITE_REMOTE_LINKS`.
- A small utility `src/ump/core/utils/link_rewriter.py` performs the rewriting and is used by the manager.
- Fetched processes are passed through an explicit handler pipeline in `ProcessManager` (ID enforcement, link rewriting, and future handlers). This makes transformation/validation of remote process metadata explicit and extensible.

### feature extension
The following missing features must be implemented:

#### Feature 0: Landing page (completed)
A simple landing page (HTML) which informs visitors about:
- licence
- contact
- available api routes

Notes:
- Implemented as part of the web adapter using Jinja2 templates.
- Template and stylesheet are packaged with the web adapter under `src/ump/adapters/web/`:
  - `src/ump/adapters/web/templates/template.html`
  - `src/ump/adapters/web/static/style.css`
  These are mounted and served by the FastAPI adapter; the landing route also supports a JSON fallback (`?f=json` or Accept header).

#### Feature I: API versioning (implemented)

- Strategy: route-based versioning using path prefixes of the form `/v{major}.{minor}/` (for example `/v1.0/`). The landing page at `/` lists the available versions and links to each version's OpenAPI document (e.g. `/v1.0/openapi.json`) and docs (e.g. `/v1.0/docs`).
- Implementation notes:
  - Supported versions are configured via `app_settings.UMP_SUPPORTED_API_VERSIONS` (default: `["1.0"]`).
  - The web adapter (`src/ump/adapters/web/fastapi.py`) creates per-version FastAPI sub-apps and mounts them under `/v{version}` so endpoints like `/v1.0/processes` are available.
  - `src/ump/adapters/site_info_static_adapter.py` now advertises per-version routes on the landing page.
  - The landing template shows supported versions and links to their OpenAPI/docs.

This approach keeps the landing page at `/` (as required by the OGC draft) and makes breaking changes explicit by assigning them to a new version prefix.

#### Feature II: /processes/{process_id} (implemented - current state)

Status: implemented in code and wired into the web adapter. The following pieces have been completed:

- Route and web adapter
  - The FastAPI web adapter (`src/ump/adapters/web/fastapi.py`) exposes the route `/processes/{process_id}` on the parent app and on each mounted versioned sub-app (e.g. `/v1.0/processes/{process_id}`).
  - Routes declare `response_model=Process` (or `ProcessList` for list endpoints) and use `response_model_exclude_none=True` so returned JSON omits None/unset fields.

- Core manager
  - `ProcessManager.get_process(process_id: str)` was added to `src/ump/core/managers/process_manager.py` and implements the business logic to retrieve a process description.
  - Behavior:
    - If the incoming id contains a provider prefix (detected through `ProcessIdValidatorPort.extract`), the manager fetches the full process description directly from that provider's `/processes/{id}` endpoint, runs the manager's handler pipeline (ID enforcement, optional link rewriting), constructs a `Process` model and returns it.
    - If no prefix is present, the manager searches across configured providers (via `get_all_processes()` which in turn calls `fetch_processes_for_provider`) for a matching `ProcessSummary`. If found the manager attempts to fetch the detailed description; if that fetch fails the manager constructs a `Process` from the summary and returns it.
    - If not found the manager raises an `OGCProcessException` with a 404 response payload.

- Caching
  - A per-provider process-list cache (`ProcessListCache`) protects repeated list fetches.
  - A per-process cache (`ProcessCache`) caches full process descriptions by canonical id and also by the bare id (the part after the colon) to reduce repeated remote requests and accidental amplification.
  - Logging was added to record cache hits, misses and cache store events for both caches.

- Serialization & API contract
  - FastAPI `response_model` features are used for output serialization and OpenAPI generation; Pydantic models (`Process`, `ProcessSummary`, `ProcessList`) define the API contract and any None fields are excluded from responses.

Outstanding / next improvements for Feature II

- Ambiguous bare ids: current behavior picks the first matching provider when searching bare ids. Consider implementing a deterministic policy (for example: error on duplicate bare ids, prefer provider order, or require fully-qualified ids to disambiguate).
- Expose cache TTL as a configuration setting (e.g. `UMP_PROCESS_CACHE_EXPIRY_SECONDS`) so operators can tune caching behavior without code changes.

Notes:
- The implementation keeps the core free of framework code and uses the ports/adapters pattern: `ProcessManager` depends only on `ProvidersPort`, `HttpClientPort` and `ProcessIdValidatorPort` and is instantiated in the web adapter lifespan and stored on `app.state` for route handlers to use.
- Link rewriting (controlled by `UMP_REWRITE_REMOTE_LINKS`) still happens inside the manager as a handler in the processing pipeline; it will rewrite remote links into local API links when enabled.


#### Feature III: /execution endpoint, Jobs, polling, and local storage (Step 1 implemented)

Current status (Step 1 COMPLETE - async execution forwarding with local job lifecycle):

Implemented pieces (updated Nov 14 2025):
1. `Job` model (`src/ump/core/models/job.py`) including: `id` (UUID), `process_id`, `provider_name`, `remote_job_id`, `remote_status_url`, timestamps, `status_code`, `status_info` snapshot history, and helpers like `is_in_terminal_state()` plus documented ID separation rationale (local vs remote vs public id).
2. `JobRepositoryPort` (`src/ump/core/interfaces/job_repository.py`) and in-memory adapter `InMemoryJobRepository` for fast TDD; ready to swap with SQLModel adapter later.
3. `JobManager` (`src/ump/core/managers/job_manager.py`): orchestrates `create_and_forward` by:
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
9. Composition root refactor: `main.py` now wires all concrete adapters (providers, HTTP client, repository, process id validator, logging) and passes factories to `create_app`. Web adapter no longer instantiates infra objects.
10. Remote status polling: background tasks query `remote_status_url` until terminal state (success/failed/ dismissed etc.) then stop; tasks are tracked for cleanup.
11. Results endpoint `/jobs/{job_id}/results` added (remote-only proxy, no local persistence yet) returning provider results; 404 if job not successful.
12. Retry adapter (Tenacity-based `RetryPort`) integrated into remote results verification for immediate-success jobs to handle transient availability gaps.
13. Poll timeout via new setting `UMP_REMOTE_JOB_TTW`; jobs exceeding this total wait time are marked failed with a diagnostic message.
14. Immediate results fallback: when provider ignores `Prefer` and returns a body containing `outputs` but no `statusInfo`, we synthesize a successful terminal `statusInfo` snapshot and skip polling.
15. Link normalization: always inject a local `self` link with the stable local UUID; add `results` link only upon success; filter out remote self/results links containing foreign job identifiers.
16. Local vs remote job ID handling simplified: remote job id captured (if present) but never exposed externally; all links and user-facing IDs use the local UUID.

Lifecycle sequence (happy path):
1. HTTP POST hits `/processes/{process_id}/execution` with raw body.
2. Web adapter parses raw JSON; `ExecuteRequest.from_raw` normalizes inputs & outputs.
3. ProcessManager delegates to JobManager.
4. JobManager creates local job record (status=accepted) and persists.
5. Forwards execute to provider; captures body or follows `Location`.
6. Derives `StatusInfo` snapshot; updates job status (e.g. `running`, `finished`).
7. Starts polling task if remote job not terminal and remote status endpoint present.
8. Returns HTTP 201 with `Location: /jobs/{local_job_id}` and the current `statusInfo` snapshot body.

Failure / edge handling:
- Missing statusInfo & no Location: mark job `failed` with diagnostic message.
- Transport errors/timeouts: catch, mark failed, persist snapshot, still return 201 (client can inspect statusInfo for failure detail). Upstream HTTP codes mapped to OGC error responses when appropriate.
- Polling stops automatically on terminal state or shutdown.

ID strategy (documented in code):
- Local UUID: internal canonical job primary key; stable for public routes.
- Remote job id: only stored when provider returns one; never replaces local id.
- Public job route id: same as local UUID (no leakage of remote semantics).
This separation avoids coupling deletion/retry semantics to remote provider identifiers and supports multi-provider orchestration.

Normalization decisions:
- Inputs kept outside `statusInfo` (OGC compliance); dedicated endpoint & storage separation pending (see remaining tasks).
- Default jobControlOptions/outputTransmission/version injected for sparse upstream process definitions.
- Malformed metadata safely ignored (logged debug).

What is still pending for Feature III:
1. `/jobs` list & `/jobs/{id}` detail endpoints (read snapshots + metadata).
2. `/jobs/{id}/inputs` or presigned URL strategy to expose stored inputs (ensuring they remain segregated from `statusInfo`).
3. SQLModel-based repository + Alembic migrations (job table + status history table).
4. Inputs large-object separation (object storage integration, checksum & size metadata fields).
5. Result storage abstraction: introduce `ResultStoragePort` (placeholder injected) and adapters (e.g., GeoServer, ldproxy) for optional persistence after success.
6. Test coverage: finalize unit tests for JobManager helpers (including timeout & immediate results paths), remote polling, ExecuteRequest normalization, ProcessManager handler pipeline, `/jobs/{id}/results`, and future /jobs endpoints.
7. Optional minimal DDD scaffolding (commands/events/aggregate) – deferred unless complexity grows; current CRUD + snapshot history sufficient.
8. Enhanced status history (append-only table) and event log optional.
9. Authorization layer (JWT) to restrict job visibility (ties into Feature IV).

Removed or superseded tasks (were proposals, now done): Add Job model, JobRepositoryPort, in-memory repo, JobManager, execute delegation, normalization factory, polling loop, leniency handlers, composition root decoupling.

Design trade-offs accepted in Step 1:
- Always async semantics (no sync shortcut yet) simplifies initial implementation; sync execute deferred.
- Polling interval is global; per-provider backoff not yet implemented.
- StatusInfo snapshots currently overwritten (history table planned to preserve transitions).
- Object storage integration postponed to keep test surface small.

Next incremental enhancements (suggested order): implement /jobs endpoints → inputs separation & endpoint → SQLModel repo & migrations → status history/events → auth gating of job resources.


Large input data: implementation strategies

When processing large payloads (e.g., 4×30MB = 120MB geospatial data), there are two primary approaches; the key constraint is **the receiving server must support the chosen approach**:

**Option 1: Chunked Transfer Encoding (automatic)**
- How it works: aiohttp automatically chunks large request bodies; no client-side code changes needed.
  ```python
  # aiohttp handles chunking transparently for large payloads
  async with session.post(url, json=large_dict) as resp:
      ...
  ```
- Receiving side requirement: ANY standard HTTP server automatically reassembles chunks (RFC 7230). OGC API Processes servers support this natively with no modifications.
- Pros: transparent, works with existing servers, no schema changes.
- Cons: no progress visibility from client; doesn't reduce memory footprint of the UMP pod during ingestion (still parses full body into Python dict).

**Option 2: URL/Href Referencing (OGC-native)**
- How it works: instead of embedding large data inline, reference it by URL.
  ```json
  {
    "inputs": {
      "geospatial_data": { "href": "http://s3.../buildings.json" }
    }
  }
  ```
- Receiving side requirement: the OGC API Processes server MUST implement the `href` reference pattern (most modern servers do, including pygeoapi). Server fetches the referenced data on-demand.
- Pros: minimal payload size, server-driven retrieval, can validate checksums, supports range requests.
- Cons: requires remote server capability; adds latency for reference resolution; requires stable external storage.

**Memory impact mitigation:**
- Option 1 still loads full JSON into Python memory (360–600MB for 120MB raw). Mitigate by:
  - Using a streaming JSON parser (e.g., `ijson`) instead of `request.json` to avoid full deserialization.
  - Storing large inputs temporarily and passing a URL reference instead.
- Option 2 avoids local memory spike entirely by outsourcing data hosting.

**Recommendation for Step 2:**
- Keep chunked transfer (Option 1) as the baseline; aiohttp handles it automatically.
- Investigate adding an input pre-processor that detects payloads above a threshold (e.g., >100MB) and automatically:
  - Stores large input objects in a temporary location (local, S3, or GCS).
  - Replaces inline data with `href` references before forwarding to the provider.
  - Cleans up temporary storage after the job completes or expires.
- This hybrid approach avoids memory pressure while remaining transparent to callers.


Event sourcing, CQRS, and job history: design decision

- For now, we will not implement full CQRS or event sourcing. Instead, we will:
  - Implement a CRUD JobRepository with an append-only `job_statuses` (history) table (A: hybrid approach).
  - Optionally add an `append_event(job_event)` primitive to the JobRepositoryPort (B: event log for future migration/testing).
  - This gives us: fast reads, simple writes, a full audit trail, and replayability for most needs, with minimal complexity.
  - If/when we need advanced projections, replay, or scaling, we can migrate to CQRS + Event Sourcing later.

Rationale:
- CRUD + history table is simple, testable, and covers most audit/replay needs.
- CQRS + event sourcing is powerful but adds significant complexity and infra cost; only migrate if you need advanced projections, strict event audit, or heavy read/write scaling.

Immediate actionable checklist (updated):
Current focus has shifted with new capabilities; checklist re-aligned:
- [x] Add `/jobs` (list) and `/jobs/{id}` (detail) endpoints (routes exist, backed by InMemoryJobRepository).
- [x] **SQLModel JobRepository + Alembic migrations + design smell fix** (complete).
- [ ] Implement inputs separation & `/jobs/{id}/inputs` (no inputs in statusInfo).
- [ ] Status history persistence (append snapshots) & optional events.
- [ ] Integrate `ResultStoragePort` for optional persistence on success.
- [ ] Tests: JobManager polling (including timeout), immediate results synthesis, retry verification, link normalization, /jobs results endpoint.
- [ ] Job execution pipeline: implement `PipelineStep` subclasses and migrate `create_and_forward` → `create_and_forward_ii` (see pipeline plan above).
- [ ] Optional: adaptive polling/backoff per provider.
- [ ] Optional: auth rules (JWT) restricting job visibility.

---

## SQLModel JobRepository — detailed implementation plan

### Architectural decisions

#### 1. Two-model ORM mapping (hexagonal-correct)

Isolating persistence details from the core requires a second model class in the adapter layer. The core `Job` (`BaseModel`) must stay free of all ORM annotations; the adapter owns a separate `JobRecord(SQLModel, table=True)` that holds all persistence metadata. The adapter maps between the two explicitly.

```
src/ump/core/models/job.py          ← domain model (pure Pydantic, no ORM)
src/ump/adapters/sqlmodel_job_repository.py
    ├── JobRecord(SQLModel, table=True)              ← ORM model (adapter only)
    ├── JobStatusHistoryRecord(SQLModel, table=True) ← history ORM model (adapter only)
    └── SQLModelJobRepository(JobRepositoryPort)     ← adapter implementing port
          ├── JobRecord.from_domain(job) -> JobRecord
          └── JobRecord.to_domain()     -> Job
```

The core never imports `sqlmodel`, `sqlalchemy`, or any database driver. All ORM-specific concerns (column types, primary key strategy, JSONB vs TEXT, FK cascade) stay inside the adapter file.

#### 2. Alembic as migration tool

SQLModel does not ship a migration tool. It wraps SQLAlchemy 2.x, so Alembic is the natural and correct choice. Autogenerate works by inspecting `SQLModel.metadata`, which is only populated once the SQLModel table classes are imported. `migrations/env.py` must import the ORM models before `run_migrations_online()` / `run_migrations_offline()` is called.

Key caveat — JSONB: SQLModel infers `TEXT` for `dict` type annotations by default. Native JSONB requires an explicit `sa_column`:

```python
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

class JobRecord(SQLModel, table=True):
    status_info: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    links: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    inputs: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
```

Without `sa_column=Column(JSONB)`, the column would be stored as a JSON-encoded TEXT, losing native JSONB operators and index support.

#### 3. Migration strategy

All old migrations have been deleted — the `migrations/versions/` directory is now empty. We start fresh from a clean Alembic history.

Action: create a single initial migration `create_jobs_tables` with `down_revision = None` (new root). This is the first and only migration in the chain and creates the two correctly structured tables.

#### 4. Design smell fix: `job_repository` on `ProcessManager`

Current state — `ProcessManager.__init__` accepts `job_repository` and the web adapter retrieves it via:
```python
repo = getattr(app.state.process_port, "job_repository", None)
```
`ProcessManager` is a process-fetching concern; it has no business holding a persistence repo. The repo belongs to `JobManager`.

Fix (same step):
1. Remove `job_repository: JobRepositoryPort | None = None` from `ProcessManager.__init__`.
2. Add `job_repo: JobRepositoryPort` parameter to `create_app(...)`.
3. In the lifespan: `app.state.job_repo = job_repo` (direct, first-class state).
4. All routes replace `getattr(app.state.process_port, "job_repository", None)` with `app.state.job_repo`.
5. `main.py`: pass `job_repo` to `create_app` directly; remove it from `process_manager_factory`.

#### 5. Adapter selection env flag

To keep tests running without a database, `main.py` selects the adapter based on `UMP_JOB_STORE`:

```
UMP_JOB_STORE=memory   → InMemoryJobRepository (default, used in all tests)
UMP_JOB_STORE=postgres → SQLModelJobRepository (production)
```

SQLModelJobRepository requires `UMP_DATABASE_URL` (PostgreSQL DSN).

### DB schema (Alt A — hybrid)

```sql
-- jobs: current snapshot (fast reads, SQL-filterable on scalar fields)
CREATE TABLE jobs (
    id              UUID PRIMARY KEY,
    process_id      TEXT,
    provider        TEXT,
    remote_job_id   TEXT,
    remote_status_url TEXT,
    status          TEXT,                      -- denormalized for WHERE filters
    created         TIMESTAMPTZ NOT NULL,
    updated         TIMESTAMPTZ,
    status_info     JSONB,                     -- full current JobStatusInfo snapshot
    inputs          JSONB,                     -- inline inputs (small payloads only)
    inputs_url      TEXT,
    inputs_storage  TEXT,
    inputs_size     INTEGER,
    inputs_checksum TEXT,
    links           JSONB,                     -- list[Link]
    diagnostic      TEXT,
    version         INTEGER NOT NULL DEFAULT 0
);

-- job_status_history: append-only audit log of all status transitions
CREATE TABLE job_status_history (
    id          BIGSERIAL PRIMARY KEY,
    job_id      UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    snapshot    JSONB NOT NULL,                -- full JobStatusInfo snapshot
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_jobs_status    ON jobs (status);
CREATE INDEX idx_jobs_provider  ON jobs (provider);
CREATE INDEX idx_jobs_process   ON jobs (process_id);
CREATE INDEX idx_jsh_job_id     ON job_status_history (job_id, seq);
```

### Files to create / modify

| File | Action | Notes |
|---|---|---|
| `src/ump/adapters/sqlmodel_job_repository.py` | CREATE | `JobRecord`, `JobStatusHistoryRecord`, `SQLModelJobRepository` |
| `migrations/versions/XXXX_create_jobs_tables.py` | CREATE | `down_revision=None` (new root), correct schema for both tables |
| `migrations/env.py` | MODIFY | Import SQLModel table classes; use `SQLModel.metadata` |
| `src/ump/adapters/web/fastapi.py` | MODIFY | Add `job_repo` param; fix all repo access; set `app.state.job_repo` |
| `src/ump/core/managers/process_manager.py` | MODIFY | Remove `job_repository` param |
| `src/ump/main.py` | MODIFY | Pass `job_repo` to `create_app`; add `UMP_JOB_STORE` selection |
| `src/ump/core/settings.py` | MODIFY | Add `UMP_JOB_STORE`, `UMP_DATABASE_URL` settings |

### Mapping strategy for `SQLModelJobRepository`

The `from_domain` / `to_domain` bridge is the only place where the two models touch. It must be complete and round-trip safe (i.e., `to_domain(from_domain(job)) == job`).

```python
@classmethod
def from_domain(cls, job: Job) -> "JobRecord":
    return cls(
        id=job.id,
        process_id=job.process_id,
        provider=job.provider,
        status=job.status,
        status_info=job.status_info.model_dump(mode="json") if job.status_info else None,
        inputs=job.inputs,
        links=[lnk.model_dump(mode="json") for lnk in job.links],
        # ... all other fields
    )

def to_domain(self) -> Job:
    return Job(
        id=self.id,
        status_info=JobStatusInfo(**self.status_info) if self.status_info else None,
        links=[Link(**lnk) for lnk in (self.links or [])],
        # ... all other fields
    )
```

### Session management

`SQLModelJobRepository` requires an async SQLAlchemy session. Two strategies:

**Option A — session per operation (simpler, safest for now):**
Each method opens its own `async_sessionmaker(engine)` context. No session leaks across requests.

**Option B — session injected at construction (more testable):**
The factory in `main.py` creates an `async_sessionmaker` and injects it; the repo uses it for all operations. Better for transactional grouping of multi-step operations.

Recommendation: Option B (session factory injected), deferred to async session from `sqlalchemy.ext.asyncio`. The `InMemoryJobRepository` needs no changes.

---

### Job execution pipeline — incremental refactoring plan

#### Motivation

`create_and_forward` in `JobManager` is a monolithic orchestration method (~200 lines) that handles validation, local job creation, HTTP forwarding, status derivation, finalization, and polling scheduling in one sequential flow. As complexity grows (output format resolution, `transmission-mode-policy`, `response-mode-policy`, result storage), each new concern requires inserting conditional logic into an already dense method.

The `JobExecutionPipeline` pattern decomposes this into a sequence of discrete, independently testable `PipelineStep` objects. Each step receives and mutates a shared `JobExecutionContext`; any step can abort the pipeline by setting `context.should_halt = True`.

#### Current state (scaffolding only — steps not yet implemented)

The following classes live in `src/ump/core/managers/job_manager.py`:

```python
class PipelineStep(ABC):           # abstract base — one async process(context) method
class ExecutionResult(dataclass):  # output of a completed pipeline run
class JobExecutionContext(BaseModel):  # mutable shared state across steps
class JobExecutionPipeline:        # runs steps in sequence; stops on should_halt
```

`create_and_forward_ii` is the new entrypoint that delegates to the pipeline. It currently runs an empty step list and is therefore a no-op. **`create_and_forward` remains the active path in production.**

#### Planned pipeline steps

Each becomes a concrete `PipelineStep` subclass, ideally in its own file under `src/ump/core/managers/steps/`:

| Step class | Responsibility | Source logic to extract |
|---|---|---|
| `ValidateAndResolveStep` | Validate `process_id`, resolve provider prefix and raw id | `_resolve_provider` |
| `CreateLocalJobStep` | Create `Job` domain object with local UUID and inline inputs | `_init_job` |
| `PersistAcceptedStep` | Persist job + initial `accepted` statusInfo snapshot; notify observers | `_persist_accepted` + `_notify_job_created` |
| `ForwardToProviderStep` | POST to remote OGC endpoint with retry/backoff | `_safe_forward` |
| `HandleProviderResponseStep` | Detect upstream error responses (≥ 400) and propagate or absorb | `_handle_upstream_error_response` |
| `DeriveStatusInfoStep` | Select derivation strategy (direct body / Location follow / immediate results / failed) | `_derive_status_info` + `StatusDerivationOrchestrator` |
| `FinalizeJobStep` | Persist derived status, update job record, notify observers | `_finalize_job` |
| `InitiatePollingStep` | Schedule background poll loop if job is non-terminal and has `remote_status_url` | `_schedule_poll` call |

Future steps can be inserted at any position without touching the others:

- `ResolveOutputFormatsStep` — capture per-output `(media_type, is_binary)` from execute request + process description (output format awareness proposal)
- `ApplyTransmissionModePolicyStep` — rewrite `transmissionMode` in payload before forwarding
- `ApplyResponseModePolicyStep` — override `response` field sent to remote

#### Migration strategy

1. Implement steps one at a time, each fully unit-tested against a minimal `JobExecutionContext` fixture.
2. Wire implemented steps into `_build_execution_pipeline()`.
3. Run `create_and_forward_ii` in parallel (shadow mode) alongside `create_and_forward` in tests — compare outputs.
4. When all steps are implemented and all tests pass against `_ii`, switch the active call in `ProcessManager.execute_process` from `create_and_forward` to `create_and_forward_ii`.
5. Delete `create_and_forward` and rename `_ii`.

#### `JobExecutionContext` field summary

```python
class JobExecutionContext(BaseModel):
    job: Optional[Job]                  # set by CreateLocalJobStep
    process_id: str                     # set by caller
    provider: Optional[ProviderConfig]  # set by ValidateAndResolveStep
    execute_payload: Dict[str, Any]     # the normalized ExecuteRequest payload
    headers: Dict[str, str]             # forwarding headers (Prefer, etc.)
    status_info: Optional[JobStatusInfo]# set by DeriveStatusInfoStep
    should_halt: bool                   # set by any step to abort the pipeline
    response: Optional[Dict[str, Any]]  # set when a step produces a direct HTTP response
```

`to_result()` converts the context into an `ExecutionResult`, which has a `to_response()` method returning the `{status, headers, body}` dict that the web adapter expects.

Minimal DDD guidance (commands, events, aggregates) - lightweight, incremental

To make the Job lifecycle easier to test, evolve, and (later) migrate to CQRS/Event Sourcing, introduce a small, optional DDD scaffolding that remains lightweight for Step 1:

- Concepts to add (minimal):
  - Commands: immutable intent objects used by the `JobManager` to express actions, e.g. `CreateJobCommand`, `ForwardExecutionCommand`, `FetchRemoteStatusCommand`.
  - Domain Events: immutable facts emitted when something meaningful happens, e.g. `JobCreated`, `JobForwarded`, `JobStatusUpdated`, `JobFailed`.
  - Aggregate: `JobAggregate` encapsulates in-memory domain logic and invariant checks. It receives Commands (or Events) and returns Events; it does not perform IO.
  - Event append primitive: `JobRepository.append_event(event, expected_version=None)` for persisting events to an in-memory list or events table.

- How these pieces fit together (runtime flow):
  1. API / ProcessManager creates a `CreateJobCommand` and passes it to `JobManager`.
 2. `JobManager` constructs or loads a `JobAggregate` and invokes `handle_command(cmd)` to get a list of DomainEvents.
 3. `JobManager` persists events via `JobRepository.append_event(...)` and updates the snapshot (`JobRepository.update(...)`).
 4. `JobManager` forwards the execution to the provider using `HttpClientPort`, maps provider responses to events (e.g., `JobForwarded`, `JobStatusUpdated`, `JobFailed`), persists them, and updates job snapshot.
 5. Optionally dispatch events on an in-memory bus for side-effects (projections, webhooks).

- Benefits (practical):
  - Testability: unit tests can exercise `JobAggregate` pure logic without IO.
  - Evolution: you capture discrete facts for replay/projection in the future without flipping the architecture.
  - Minimal cost: dataclasses + a few helper methods; keep Phase 1 in-memory to avoid infra overhead.

- Minimal files to add (small footprint):
  - `src/ump/core/commands.py` - small dataclasses for the command shapes.
  - `src/ump/core/events.py` - dataclasses for domain events.
  - `src/ump/core/aggregates/job_aggregate.py` - `JobAggregate` with pure `handle_command` and `apply_event` methods.
  - update `src/ump/core/interfaces/job_repository.py` to include `append_event(event, expected_version: int | None = None)`.
  - `src/ump/adapters/job_repository_inmemory.py` - implement `append_event` alongside CRUD methods.

- Tests to add (TDD):
  - `tests/unit/test_job_aggregate.py` - aggregate specs (command -> events -> state transitions).
  - `tests/unit/test_job_manager_events.py` - JobManager integration with in-memory repo and fake HttpClient.

Keep these optional: if you prefer to delay, we can add only the event-append signature on the port so tests can emit events later. Otherwise I can scaffold the lightweight DDD pieces now.
Notes:
- Keep adapters conservative: they provide raw response shape and map transport/IO errors to domain exceptions. Business logic (statusInfo merging, job lifecycle) belongs in core.
- For Step 1 use an in-memory job store; prepare SQLModel schema and migration plan for Step 2 (hybrid approach with JSONB + history table recommended).

What is NOT implemented yet for Step 1 (short):

- Local job creation/storage: there is no job model or any in-memory/persistent store yet. For Step 1 we should add a lightweight in-memory job store (replaceable by DB in Step 2).
- Execute flow: the manager must be extended to always create a local job, populate a statusInfo snapshot, follow provider `Location` headers when necessary to fetch job status, and return HTTP 201 with Location header pointing to the local job plus the statusInfo body.
- Validation: request body validation against the OGC `execute` schema is not enforced yet.

Brief notes about tests already added (TDD):

- Lightweight FastAPI integration tests: `tests/test_fastapi_execute_async.py` - TDD-style tests that cover the expected behaviors around async execute handling (forwarding valid statusInfo, following `Location`, handling missing statusInfo, provider errors/timeouts, always creating a local job, resolving relative Location headers). These tests currently express the desired behavior and will drive implementation.
- Adapter tests: `tests/test_aiohttp_adapter.py` - unit-level tests for `AioHttpClientAdapter` (JSON parsing, non-JSON -> 502, timeouts -> 504, POST text fallback).
- Full-stack E2E test: `tests/test_fastapi_execute_e2e.py` - uses the real `AioHttpClientAdapter` together with `aioresponses` to mock provider responses and verify the full call path from FastAPI -> ProcessManager -> Adapter -> provider.
- ProcessManager unit tests: `tests/test_process_manager.py` - earlier unit tests using a fake HTTP client exist to exercise manager logic in isolation.

Recommended next actions (for the next coding assistant):

1. Add a minimal `Job` model and an in-memory job store in core (e.g., `src/ump/core/models/job.py` and `src/ump/core/managers/job_store.py`). Keep the store replaceable by a DB-backed adapter later.
2. Extend `ProcessManager.execute_process` to implement the async execute flow:
  - Create a local job immediately (uuid, timestamps, provider ref, inputs).
  - Call `http_client.post(...)` to forward execution.
  - If the provider response includes a JSON body conforming to statusInfo, use it to populate the local job status snapshot.
  - Else if the provider response has a `Location` header, resolve relative locations against the provider base URL and `http_client.get(location)` to fetch the statusInfo; use it if valid.
  - Else mark the job as failed and include diagnostic details.
  - Persist the job in the in-memory store and return HTTP 201 with `Location: /jobs/{local_id}` and the job's statusInfo body.
3. Implement lightweight validation of incoming execute request bodies (Pydantic or jsonschema) and return 400 on invalid input (no job created).
4. Run the newly added TDD tests (lightweight FastAPI tests and adapter/E2E tests) and iterate until they pass. Use `aioresponses` for adapter/E2E mocks.
5. Keep the adapter conservative: it should supply raw `status/headers/body` and map transport errors to `OGCProcessException`; business rules (statusInfo merging, job lifecycle) belong to `ProcessManager`.

Pointers for the assistant taking over the task:

- FastAPI route: `src/ump/adapters/web/fastapi.py` - where `execute_process` is wired.
- Core manager: `src/ump/core/managers/process_manager.py` - extend `execute_process` to implement job creation, Location-following and statusInfo population.
- HTTP adapter: `src/ump/adapters/aiohttp_client_adapter.py` - the adapter contract (returns dict) that `ProcessManager` relies on.
- Tests to run: `tests/test_fastapi_execute_async.py`, `tests/test_fastapi_execute_e2e.py`, `tests/test_aiohttp_adapter.py`, `tests/test_process_manager.py`.

Quick prioritized checklist (for the next session):
Superseded (already implemented); new quick priorities:
- [ ] `/jobs` list & detail endpoints
- [ ] Inputs separation & endpoint
- [ ] SQLModel repository & migrations
- [ ] Result storage adapter integration
- [ ] Expanded test coverage (retry, timeout, immediate results)

```yaml
#JobControlOptions.yaml
type: string
enum:
  - sync-execute
  - async-execute
  - dismiss
```

```yaml
#statusInfo.yaml
type: object
required:
   - jobID
   - status
   - type
properties:
   processID:
      type: string
   type:
      type: string
      enum:
        - process
   jobID:
      type: string
   status:
      $ref: "statusCode.yaml"
   message:
      type: string
   created:
      type: string
      format: date-time
   started:
      type: string
      format: date-time
   finished:
      type: string
      format: date-time
   updated:
      type: string
      format: date-time
   progress:
      type: integer
      minimum: 0
      maximum: 100
   links:
      type: array
      items:
         $ref: "link.yaml"
```

```yaml
# JobList
type: object
required:
  - jobs
  - links
properties:
  jobs:
    type: array
    items:
      $ref: "statusInfo.yaml"
  links:
    type: array
    items:
      $ref: "link.yaml"
```

- deferred: remote process execution: sync
- deferred: remote transmission direct, local by ref
- deferred: stream content to clients (for use cases where some resources take longer than others)

#### Feature IV: JWT-based Auth
- implement jwt based authentication
- evaluate jwt for realm roles and client roles to grant or restrict access to resources (all routes)
- add an option to grant public access to /processes route

#### Feature V: Add support for result storage
- add result storage business logic
- create an adapter for geoserver result storage (wfs, wms)
- create an adapter for ldproxy result storage (ogc api features)

Notes to assistant:
When user asks for implementation details for"ensembles":
- Ask user for reference code to gain insights what ensembles are and which mechanisms must be reimplemented
- do not reuse the user provided code, instead look for a better solution and inform the user

## Next non-immediate steps

- Add unit tests for `ProcessManager`, `ProcessCache`, and `ProviderConfigFileAdapter` (happy path + failure fallback).
- Add unit tests for the cache and manager (Task 10).

## How to run the app for local testing

1. Install dependencies (ensure `uvicorn`, `fastapi`, `aiohttp`, and `watchdog` are installed).
2. Start the app:

```bash
python -m src.ump.main
```

(Or use `uvicorn src.ump.adapters.web.fastapi:create_app --reload` after wiring DI in a runner.)

## Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.

---

_Last updated: 2026-05-29

# Ideas (not ordered, no exact location within the current implementation plan)

## adding a mocked OGC API Processes remote server
- for easier testing and users to try out without additional infrastructure mocking an OGC API Processes server would be helpful instead of relying on a (PyGeoApi) modelserver

## UMP - execution proxy: add additional skills to remote Models

### Kontext

Data is transferred from modelserver to UMP to client. The Problem: Large geodata and missing filtering. OGC API Processes v1.0.0 does not allow for subsetting or filtering of large geodata. The Results object is always passed as a block. Models can generate very large
geodata sets, which can cause bottlenecks.

An external result store that only comes into play **after** the data has been received by the UMP solves the
problem too late: for this to work, the data must already have flowed completely from `Model Server → UMP`.

The solution lies in expanding the UMP into an **execution proxy** that can actively
control the data flow-even before the data has completely passed through the UMP.


### UMP as Execution Proxy

The UMP acts as a broker for the entire execution lifecycle according to OGC API Processes:

- `POST /processes/{id}/execution` - Receive and forward execution requests
- `GET /jobs` / `GET /jobs/{id}` - Federated job registry across all model servers
- `GET /jobs/{id}/results` - Intercept results and, if necessary, write them to an external store

**Zentrale Fähigkeiten eines execution proxies:**

- federated job registry
- central auth management
- process exclusion
- deterministic caching
- normalizing: UMP can add or deprive model servers of skills, e.g. add `transmissionMode: reference` capability

This proposal addresses `result-storage`, `transmission-mode-policy`, and `response-mode-policy`

### Configuration: `transmission-mode-policy`

For each process, the `providers.yaml` file explicitly configures how the UMP handles the
`transmissionMode` parameter of the OGC standard. The parameter can take four possible values:
---

#### `pass-through`

The UMP acts as a transparent proxy. The `transmissionMode` from the client request
is forwarded to the model server unchanged. The UMP’s result store is not
used - even if one is configured.

The process description that the UMP communicates externally reflects exactly the native
capabilities of the model server.

**Suitable for:**
- Model servers that natively support `ref` (the model server’s link is accessible to clients)
- Model servers that only support `value` when no UMP-side store is desired
- Scenarios in which the UMP should not interfere with the data path

---

#### `emulate-ref`

The UMP adds the `transmissionMode: reference` capability to the process, even if the
model server does not natively support it.

**Behavior:**
- The UMP authoritatively adds `transmissionMode: reference` to the externally visible
  process description.
- If the client requests `ref`: The UMP internally sends `value` to the model server,
  receives the data (ideally as a stream), writes it to the configured
  result store, and returns a link to the client.
- If the client requests `value`: The UMP passes the data directly-the
  result store is not used.

**Prerequisite:** A result store (`result-storage`) must be configured.
If the configuration is missing, `emulate-ref` results in a configuration error.

**Suitable for:**
- Model servers that only support `value`, but whose results are to be
  persisted in the UMP store if the client requests it

---

#### `emulate-ref-only`

Like `emulate-ref`, but `value` is completely blocked as the transmission mode for the client.
The UMP authoritatively removes `value` from the process description.
All results are routed through the result store without exception.

**Behavior:**
- The UMP advertises only `transmissionMode: ref`.
- Every Execution Request is internally forwarded to the model server with `value`,
  the result is written to the store, and a link is returned.
- A client request with `value` is rejected by the UMP with an error
  (the store is not optional).

**Prerequisite:** A Result Store (`result-storage`) must be configured.

**Suitable for:**
- Scenarios in which all results are to be stored centrally in the UMP Store
  (e.g., for auditing, caching, or access reasons)
- Model servers whose native storage is temporarily unavailable or inaccessible to clients


---

#### `value-only`

The UMP completely blocks `transmissionMode: ref`- even if the model server natively
supports it. The process description is cleaned up accordingly.

A client request with `ref` is rejected by the UMP with an error.
The result store is not used.

**Suitable for:**
- Scenarios in which uniform `value` semantics must be enforced
- Model servers whose native `ref` links are not accessible to all clients and
  where no UMP store is to be operated


### Configuration: `result-storage`

`result-storage` defines the **storage destination** for results managed by the UMP. It is
a parameter separate from `transmission-mode`:

- `transmission-mode` → defines the **behavior policy**
- `result-storage` → defines the **storage destination**

| Wert | Bedeutung |
|---|---|
| `remote` | not necessary anymore, has no meaning |
| `geoserver` | UMP saves Results within GeoServer-Instanz |
| `ldproxy` | UMP saves Results within ldproxy-Instanz |

`result-storage` is only relevant if `transmission-mode-policy: emulate-ref` or
`emulate-ref-only` is configured and the client requests `transmission-mode: reference`. In all other cases, it is ignored.


### Behaviour Overview

| Model skills | `transmission-mode` | Client wants `ref` | Client wants `value` | Store active? |
|---|---|---|---|---|
| `ref` + `value` | `native` | Proxy through | Proxy through | No |
| `value` only | `native` | Proxy through (model decides) | Proxy through | No |
| `ref` only | `native` | Proxy through | Error from model | No |
| `value` only | `emulate-ref` | UMP stores, provides link | Directly through | Yes (only for `ref`) |
| `ref` + `value` | `emulate-ref` | UMP stores, provides link | Directly through | Yes (only for `ref`) |
| `value` only | `emulate-ref-only` | UMP saves, provides link | Error from UMP | Always |
| `ref` + `value` | `emulate-ref-only` | UMP saves, provides link | Error from UMP | Always |
| `ref` + `value` | `value-only` | Error from UMP | Directly through | No |
| `ref` only | `value-only` | UMP error | Model error | No |

**Unsupported combination:** The model can only be `ref`; `result-storage` is configured.
The UMP would have to follow the model's native link, download the data, and resave it.
This combination is treated as a configuration error.


### Validierungsregeln für die Konfiguration

When starting the UMP (or reloading `providers.yaml`), the configuration should be validated
as follows:

- `emulate-ref` without `result-storage` → **Error**
- `emulate-ref-only` without `result-storage` → **Error**
- `native` with `result-storage` → **Warning** (Store is ignored)
- `value-only` with `result-storage` → **Warning** (Store is ignored)
- Model can only be configured with `ref` or `result-storage` → **Error** (not supported)


### Impact on the Process Description

The UMP is the **authoritative source** of the process description for all configured processes.
It may modify the process description provided by the model server:

| `transmission-mode` | Change to the process description |
|---|---|
| `native` | None - 1:1 forwarding |
| `emulate-ref` | `transmissionMode: ref` is added if not present |
| `emulate-ref-only` | `transmissionMode` is set to `[“ref”]` |
| `value-only` | `transmissionMode` is set to `[“value”]` |

This modification is intentional and must be transparent to UMP operators. Clients
should act exclusively based on the Process Description and should not have to consult the
model server’s Process description.
---

### Configuration: `response-mode-policy`

The OGC API Processes standard defines a `response` field in the execute request body with two values:

- `"document"` — the server wraps results in a structured JSON document (conforming to the OGC result schema). UMP can parse this, follow links, and store results.
- `"raw"` — the server returns the unstructured raw output (e.g., binary data, plain GeoJSON) without any JSON envelope.

This matters because UMP as an execution proxy often needs to **inspect** the remote response body (e.g., to extract `statusInfo`, store results, or rewrite links). A `raw` response from the remote may be opaque to UMP's business logic.

The `response-mode-policy` is therefore an operator-level decision that controls **what `response` value UMP actually sends to the remote OGC API Processes server**, independently of what the client requested.

It is a parameter separate from `transmission-mode-policy`:

- `transmission-mode-policy` → controls how result *links vs. values* are handled
- `response-mode-policy` → controls the *encoding format* UMP requests from the remote server

#### Policy values

---

##### `pass-through` (default)

UMP forwards the `response` value from the client execute request to the remote server unchanged. The remote response is proxied as-is.

**Suitable for:**
- Remote servers whose `raw` response is directly consumable by clients (no UMP-side result storage needed).
- Scenarios where the operator does not want UMP to interfere with response encoding.

**Risk:** If UMP needs to parse the remote response (e.g., for result storage or link rewriting), a `raw` upstream response may be unreadable. This policy is therefore incompatible with `transmission-mode-policy: emulate-ref` / `emulate-ref-only`.

---

##### `force-document`

UMP always sends `response: "document"` to the remote server, regardless of what the client requested.

- If the client requested `document`: UMP proxies the document response directly.
- If the client requested `raw`: UMP extracts the raw result value from the document envelope before returning it to the client, preserving the client's expected response shape.

**Suitable for:**
- Any configuration where result storage is active (UMP must parse the structured response to store results and generate links).
- Remote servers that produce structured output that benefits from document-level validation and link injection.

**Required when:** `transmission-mode-policy` is `emulate-ref` or `emulate-ref-only`, because UMP must receive a parseable response to write to the result store.

---

##### `force-raw`

UMP always sends `response: "raw"` to the remote server, regardless of what the client requested.

Results are proxied as raw bytes. Result storage and link rewriting are bypassed (UMP cannot inspect raw binary responses).

**Suitable for:**
- Processes that produce binary or non-JSON output and where no UMP-side storage is needed.
- Performance-sensitive scenarios where avoiding JSON serialization overhead is important.

**Incompatible with:** `transmission-mode-policy: emulate-ref` / `emulate-ref-only` (no result store without a parseable response).

---

#### Validation rules

When starting the UMP (or reloading `providers.yaml`), the following rules apply:

- `response-mode-policy: pass-through` with `transmission-mode-policy: emulate-ref` or `emulate-ref-only` → **Warning** (client may send `raw`, breaking result storage; consider `force-document`)
- `response-mode-policy: force-raw` with `transmission-mode-policy: emulate-ref` or `emulate-ref-only` → **Error** (result store requires a parseable `document` response)
- `response-mode-policy: force-raw` with `result-storage` configured → **Warning** (result store will never be reachable for raw responses)

#### Behaviour overview

| Client `response` | `response-mode-policy` | What UMP sends to remote | What UMP returns to client | Store active? |
|---|---|---|---|---|
| `document` | `pass-through` | `document` | `document` (proxy) | Depends on `transmission-mode-policy` |
| `raw` | `pass-through` | `raw` | `raw` (proxy) | No |
| `document` | `force-document` | `document` | `document` (proxy) | Depends on `transmission-mode-policy` |
| `raw` | `force-document` | `document` | raw content extracted from document | Depends on `transmission-mode-policy` |
| `document` | `force-raw` | `raw` | `raw` (proxy, client expected document) | No |
| `raw` | `force-raw` | `raw` | `raw` (proxy) | No |

#### Impact on process description

The `response-mode-policy` does **not** modify the externally advertised process description (OGC does not expose `response` as a process-level capability). It is purely an internal forwarding decision. UMP operators configure it in `providers.yaml` at the process level.

Example `providers.yaml` fragment:

```yaml
modelserver:
  name: example
  url: "http://modelserver:5000"
  processes:
    hello-world:
      transmission-mode-policy: emulate-ref
      response-mode-policy: force-document   # required when emulate-ref is set
      result-storage: geoserver
    wind-model:
      transmission-mode-policy: pass-through
      response-mode-policy: pass-through     # no store involved, proxy as-is
```

---

### Output format awareness in UMP

#### The problem

An OGC API Processes execute request body can declare, per output, which media type the client wants and how results should be transmitted:

```json
{
  "outputs": {
    "voronoi_diagram": {
      "format": {
        "mediaType": "application/geo+json",
        "schema": "https://geojson.org/schema/FeatureCollection.json"
      },
      "transmissionMode": "reference"
    },
    "classification_breaks": {
      "transmissionMode": "value"
    }
  }
}
```

The OGC standard also mandates specific HTTP response shapes depending on the combination of execute mode, `response`, transmission mode, and number of outputs (summarised below):

| response | transmission mode | # outputs | HTTP code | Content-Type | Body |
|---|---|---|---|---|---|
| `raw` | `value` | 1 | 200 | as per output definition | raw output bytes |
| `raw` | `value` | >1 | 200 | `multipart/related` | one part per output |
| `raw` | `reference` | 1 | 204 | — | empty + `Link` headers |
| `raw` | mixed | >1 | 200 | `multipart/related` | one part per output |
| `document` | `value` | any | 200 | `application/json` | results document |
| `document` | `reference` | 1 | — | — | — |

UMP, as a proxy, must be aware of these rules in two places:

1. **Forwarding the execute request**: UMP forwards the `outputs` dict (including per-output `format` and `transmissionMode`) to the remote server unchanged (or adjusted by `transmission-mode-policy`). No additional format resolution is needed at forward time because the remote server handles the OGC rules natively.

2. **Proxying the results** (`GET /jobs/{id}/results`): When UMP fetches results from the remote and returns them to the client, it must know:
   - Whether the expected response body is binary (e.g., FlatGeobuf, GeoTIFF) or JSON-native (GeoJSON, plain JSON), to decide whether to parse or stream it.
   - What `Content-Type` to advertise to the client.
   - When `response-mode-policy: force-document` causes UMP to request `document` from the remote but the client originally requested `raw` with a single output, UMP must unwrap the document envelope and return only the raw output value.

#### What UMP needs to track per job

When a job is created (`POST /processes/{id}/execution`), UMP should capture the **resolved per-output format** alongside the job record, so it is available when proxying results later:

```
job.output_formats: dict[output_id, ResolvedOutputFormat]
  output_id:         str        — e.g. "voronoi_diagram"
  media_type:        str        — canonical IANA type, e.g. "application/geo+json"
  transmission_mode: str        — "value" | "reference"
  is_binary:         bool       — True for FlatGeobuf, GeoTIFF, PNG etc.; False for JSON-native types
```

`is_binary` is the critical flag: it tells UMP whether a remote `document` response will contain a base64-encoded string value (binary) or a JSON object/array (JSON-native), which controls how UMP can safely unwrap or pass through the result.

#### Output format resolution

UMP resolves the per-output format at execute time by reading the process description it already fetched and cached (via `ProcessManager`):

- For each `output_id` in the execute request's `outputs` map:
  - If the client supplied `format.mediaType`, use it (after validating it is advertised in the process description output schema).
  - Otherwise pick the highest-priority default from the output schema's `oneOf` branches, using a priority list (e.g., `application/geo+json` > `application/json` > binary types).
- If the client omitted `outputs` entirely, resolve all described outputs with their defaults (OGC req. 27).

This logic closely mirrors the `OutputSchemaResolver` in the `fastprocesses` package the user referenced. UMP should implement an equivalent `OutputFormatResolver` in `src/ump/core/utils/output_format_resolver.py` (port-free pure function — no I/O). The key difference from the `fastprocesses` version is that UMP does **not** serialize results itself; it only needs the resolved `(media_type, is_binary, transmission_mode)` triple per output.

#### Results proxy behavior

When serving `GET /jobs/{id}/results`, UMP fetches the result from the remote server. The behavior depends on `response-mode-policy` and the stored `output_formats`:

| `response-mode-policy` | Client's original `response` | Remote response shape | UMP action |
|---|---|---|---|
| `pass-through` | `raw` | raw bytes | Stream directly; use stored `media_type` for `Content-Type` |
| `pass-through` | `document` | JSON document | Proxy JSON document as-is |
| `force-document` | `raw`, 1 output | JSON document with value envelope | Unwrap the `value` field; return raw bytes with stored `media_type`. If `is_binary`, base64-decode first. |
| `force-document` | `document` | JSON document | Proxy JSON document as-is |
| `force-raw` | any | raw bytes | Stream directly; use stored `media_type` |

#### What is reusable from `fastprocesses`

| Component | Relevance to UMP |
|---|---|
| `OutputSchemaResolver.resolve()` | Directly applicable. UMP needs the same schema-walking logic to derive `(media_type, is_binary)` per output. Adapt rather than import directly (keep UMP's core free of fastprocesses dependency). |
| `ResolvedOutputFormat` dataclass | The `output_id`, `media_type`, `is_binary`, `transmission_mode` fields are all needed. `schema_branch` is only needed during resolution, not at proxy time. |
| `_media_type_from_schema`, `_find_branch`, `_default_media_type`, `_is_binary` | Internal helpers — replicate the logic in `src/ump/core/utils/output_format_resolver.py`. |
| `serialize_result` / `BaseProcessResult` | **Not** applicable to UMP. UMP is a proxy; it never instantiates or calls process logic. Result serialization is handled by streaming the remote response body. |
| `_build_document_response` | Partially applicable: the unwrapping direction (`document → raw`) is the inverse of what fastprocesses does (`result → document`). UMP needs the inverse: extract `value` from a document envelope and return raw bytes. |

#### Files to add / modify

- `src/ump/core/utils/output_format_resolver.py` — pure `resolve_output_formats(process: Process, requested_outputs: dict) -> dict[str, ResolvedOutputFormat]` function.
- `src/ump/core/models/job.py` — add `output_formats: dict[str, ResolvedOutputFormat]` field (serializable to JSON for DB storage).
- `src/ump/core/managers/job_manager.py` — call `resolve_output_formats` at job creation time and store on the job record.
- `src/ump/adapters/web/fastapi.py` — update `GET /jobs/{id}/results` route to read `job.output_formats` and apply the correct proxy/unwrap logic.