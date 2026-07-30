_Last_updated: 2026-07-30: 
⚠️ Splitted into multiple files for better maintainability and readability
⚠️ this file islegacy and left for refernce purposes only 

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# How to run

## Install dependencies
```bash
poetry install
```

## Start the API server
```bash
ump                         # uses .env or environment variables
```

With PostgreSQL persistence:
```bash
UMP_JOB_STORE=postgres \
UMP_DATABASE_URL=postgresql+asyncpg://ump:ump@localhost:5432/ump \
ump
```

## Run database migrations
```bash
# Uses UMP_DATABASE_* env vars (or UMP_DATABASE_URL):
ump-migrate                 # upgrade head
ump-migrate downgrade -1    # any alembic subcommand passes through
```

## Start the mock OGC server (for local testing without a real model server)
```bash
PYTHONPATH=scripts .venv/bin/uvicorn scripts.mock_ogc_server:app --port 5001 --reload
```
Then set `providers.yaml` to point at `http://localhost:5001` with process ids `echo`, `hello-world`, `slow`, `failing-job`.

## Run tests
```bash
PYTHONPATH=src .venv/bin/pytest tests/ -q
```

## Docker Compose (dev environment)
```bash
docker compose -f docker-compose-dev.yaml up mock-ogc-server ump-db
ump-migrate
ump
```

# Refactoring status and next steps

This document captures where the refactor to a hexagonal architecture left off and what the next steps are. Use this as a personal reference and to inform you as the coding assistant.

## High-level goal

Refactor the Urban Model Platform (UMP) codebase to follow hexagonal architecture:
- Core (business logic) depends only on ports (interfaces).
- Adapters implement ports and are injected into the core.
- Keep web adapter, persistence, and other infra concerns outside of core.

## Current state — complete picture

_Last updated: 2026-07-04_

### ✅ Infrastructure / cross-cutting

| Component | Status | Location |
|---|---|---|
| Provider config (file-watcher, atomic updates, thread-safe store) | ✅ | `src/ump/adapters/provider_config_file_adapter.py` |
| `ProvidersPort` interface | ✅ | `src/ump/core/interfaces/providers.py` |
| Process ID validation (`ColonProcessId`) | ✅ | `src/ump/adapters/colon_process_id_validator.py` |
| `ProcessIdValidatorPort` interface | ✅ | `src/ump/core/interfaces/process_id_validator.py` |
| HTTP client (`AioHttpClientAdapter`) | ✅ | `src/ump/adapters/aiohttp_client_adapter.py` |
| `HttpClientPort` interface | ✅ | `src/ump/core/interfaces/http_client.py` |
| Retry adapter (Tenacity) | ✅ | `src/ump/adapters/retry_tenacity.py` |
| Logging port + adapter | ✅ | `src/ump/adapters/logging_adapter.py` |
| Settings (`UmpSettings` via pydantic-settings) | ✅ | `src/ump/core/settings.py` |
| `ump` + `ump-migrate` CLI commands (Poetry scripts) | ✅ | `pyproject.toml`, `src/ump/cli.py` |

### ✅ Web adapter & wiring

| Component | Status | Location |
|---|---|---|
| FastAPI web adapter with lifespan DI | ✅ | `src/ump/adapters/web/fastapi.py` |
| Landing page (Jinja2 + JSON fallback) | ✅ | `src/ump/adapters/web/templates/`, `static/` |
| Route-based API versioning (`/v1.0/`) | ✅ | `src/ump/adapters/web/fastapi.py` |
| Composition root (single root in `asgi.py`, CLI entry in `main.py`) | ✅ | `src/ump/asgi.py`, `src/ump/main.py` |
| Site info adapter (landing page routes) | ✅ | `src/ump/adapters/site_info_static_adapter.py` |
| `AuthPort` + `AuthContext` (JWT auth interface) | ✅ | `src/ump/core/interfaces/auth.py` |
| `JwtAuthAdapter` (OIDC, JWKS cache, role extraction) | ✅ | `src/ump/adapters/jwt_auth_adapter.py` |
| Per-route auth dependency + `_check_process_access` | ✅ | `src/ump/adapters/web/fastapi.py` |
| `Job.user_id` + user-aware job visibility filtering | ✅ | `src/ump/core/models/job.py`, repositories |
| DB migration: `user_id` column on `jobs` table | ✅ | `migrations/versions/0002_add_user_id_to_jobs.py` |
| `UMP_PUBLIC_PROCESSES` gate on process routes | ✅ | `src/ump/adapters/web/fastapi.py` |
| Request ID in all error responses (body + header) | ✅ | `src/ump/adapters/web/fastapi.py` |
| `AuthorizationService` (access control moved to core) | ✅ | `src/ump/core/services/authorization.py` |
| Startup wiring assertions (fail-fast on misconfigured factories) | ✅ | `src/ump/adapters/web/fastapi.py` lifespan |
| `HttpClientPort.get_content()` + results proxy (binary-safe) | ✅ | `src/ump/core/interfaces/http_client.py`, `src/ump/adapters/aiohttp_client_adapter.py` |

### ✅ Process management

| Component | Status | Location |
|---|---|---|
| `ProcessManager` (concurrent fetching, per-provider cache) | ✅ | `src/ump/core/managers/process_manager.py` |
| `ProcessCache` / `ProcessListCache` (TTL-based in-memory) | ✅ | `src/ump/core/managers/process_cache.py` |
| Process handler pipeline (ID enforcement, link rewriting, metadata leniency) | ✅ | `src/ump/core/managers/process_manager.py` |
| `GET /processes` + `GET /processes/{id}` routes | ✅ | `src/ump/adapters/web/fastapi.py` |

### ✅ Job management (Feature III — core complete)

| Component | Status | Location |
|---|---|---|
| `Job` domain model | ✅ | `src/ump/core/models/job.py` |
| `JobRepositoryPort` interface | ✅ | `src/ump/core/interfaces/job_repository.py` |
| `InMemoryJobRepository` (TDD / default) | ✅ | `src/ump/adapters/job_repository_inmemory.py` |
| `SQLModelJobRepository` (PostgreSQL via asyncpg) | ✅ | `src/ump/adapters/sqlmodel_job_repository.py` |
| Alembic migrations (`jobs` + `job_status_history` tables) | ✅ | `migrations/versions/0001_create_jobs_tables.py` |
| `UMP_JOB_STORE=memory\|postgres` adapter selection | ✅ | `src/ump/main.py` |
| `JobManager` (orchestration: create → forward → derive → persist → poll) | ✅ | `src/ump/core/managers/job_manager.py` |
| `ExecuteRequest` normalization model | ✅ | `src/ump/core/models/execute_request.py` |
| Remote status polling with TTW timeout | ✅ | `src/ump/core/managers/job_manager.py` |
| Immediate results fallback (no-statusInfo provider) | ✅ | `src/ump/core/managers/job_manager.py` |
| Link normalization (local self/results links) | ✅ | `src/ump/core/managers/job_manager.py` |
| Observer pattern (status history, polling scheduler, results verification) | ✅ | `src/ump/core/managers/observers.py` |
| Status derivation strategies (orchestrator + strategy pattern) | ✅ | `src/ump/core/managers/status_derivation_orchestrator.py` |
| `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/results` routes | ✅ | `src/ump/adapters/web/fastapi.py` |
| `POST /processes/{id}/execution` route | ✅ | `src/ump/adapters/web/fastapi.py` |
| `JobExecutionPipeline` — all 9 steps implemented, active via `run_execution_pipeline` | ✅ | `src/ump/core/managers/steps/execution_steps.py` |

### ✅ Developer tooling

| Component | Status | Location |
|---|---|---|
| Mock OGC API Processes server (echo, hello-world, slow, failing-job) | ✅ | `scripts/mock_ogc_server.py` |
| `providers.yaml` (correct list-based format, includes mock entry) | ✅ | `providers.yaml` |
| `providers.yaml.example` (documents all auth types + mock server) | ✅ | `providers.yaml.example` |
| `docker-compose-dev.yaml` `mock-ogc-server` service | ✅ | `docker-compose-dev.yaml` |

---

## Key files

### Core interfaces
- `src/ump/core/interfaces/providers.py` — `ProvidersPort`
- `src/ump/core/interfaces/process_id_validator.py` — `ProcessIdValidatorPort`
- `src/ump/core/interfaces/http_client.py` — `HttpClientPort`
- `src/ump/core/interfaces/job_repository.py` — `JobRepositoryPort`
- `src/ump/core/interfaces/observers.py` — `JobStateObserver`
- `src/ump/core/interfaces/retry.py` — `RetryPort`

### Core models
- `src/ump/core/models/job.py` — `Job`, `JobStatusInfo`, `StatusCode`, `JobList`
- `src/ump/core/models/execute_request.py` — `ExecuteRequest`, `ResponseMode`, `TransmissionMode`
- `src/ump/core/models/process.py` — `Process`, `ProcessSummary`, `ProcessList`
- `src/ump/core/models/providers_config.py` — `ProviderConfig`, `ProcessConfig`, `ProvidersConfig`
- `src/ump/core/config.py` — `JobManagerConfig`
- `src/ump/core/settings.py` — `UmpSettings` (env vars)

### Adapters
- `src/ump/adapters/provider_config_file_adapter.py`
- `src/ump/adapters/colon_process_id_validator.py`
- `src/ump/adapters/aiohttp_client_adapter.py`
- `src/ump/adapters/job_repository_inmemory.py`
- `src/ump/adapters/sqlmodel_job_repository.py` — ORM models + `SQLModelJobRepository`
- `src/ump/adapters/retry_tenacity.py`
- `src/ump/adapters/logging_adapter.py`
- `src/ump/adapters/site_info_static_adapter.py`
- `src/ump/adapters/web/fastapi.py` — all routes, lifespan, middleware

### Managers
- `src/ump/core/managers/process_manager.py`
- `src/ump/core/managers/job_manager.py`
- `src/ump/core/managers/status_derivation_orchestrator.py`
- `src/ump/core/managers/observers.py`
- `src/ump/core/managers/process_cache.py`

### Infrastructure
- `src/ump/main.py` — composition root + Uvicorn entrypoint
- `src/ump/cli.py` — `ump` and `ump-migrate` Poetry scripts
- `migrations/env.py` — Alembic config (reads `UMP_DATABASE_*` env vars)
- `migrations/versions/0001_create_jobs_tables.py`
- `scripts/mock_ogc_server.py` — standalone development mock server



## Feature Implementation Guide

### small changes
- Improve logging usage across modules (inject `logger` where useful).
- Links in fetched processes are now optionally rewritten to local API links. This is controlled by the setting `UMP_REWRITE_REMOTE_LINKS`.
- A small utility `src/ump/core/utils/link_rewriter.py` performs the rewriting and is used by the manager.
- Fetched processes are passed through an explicit handler pipeline in `ProcessManager` (ID enforcement, link rewriting, and future handlers). This makes transformation/validation of remote process metadata explicit and extensible.

### ✅ Feature 0: Landing page (completed)
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

### ✅ Feature I: API versioning (implemented)

- Strategy: route-based versioning using path prefixes of the form `/v{major}.{minor}/` (for example `/v1.0/`). The landing page at `/` lists the available versions and links to each version's OpenAPI document (e.g. `/v1.0/openapi.json`) and docs (e.g. `/v1.0/docs`).
- Implementation notes:
  - Supported versions are configured via `app_settings.UMP_SUPPORTED_API_VERSIONS` (default: `["1.0"]`).
  - The web adapter (`src/ump/adapters/web/fastapi.py`) creates per-version FastAPI sub-apps and mounts them under `/v{version}` so endpoints like `/v1.0/processes` are available.
  - `src/ump/adapters/site_info_static_adapter.py` now advertises per-version routes on the landing page.
  - The landing template shows supported versions and links to their OpenAPI/docs.

This approach keeps the landing page at `/` (as required by the OGC draft) and makes breaking changes explicit by assigning them to a new version prefix.

### ✅ Feature II: /processes/{process_id} (implemented)

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


### ✅ Feature III: Execution proxy, Jobs, and Persistence

The goal of Feature III is to enable UMP to act as an OGC API Processes execution proxy: forwarding execution requests to remote model servers, maintaining a local federated job registry with full status lifecycle, and persisting jobs durably in PostgreSQL.

**Feature III is functionally complete for the core use case.** The remaining items are refinements and extensions.

#### Quick status

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

#### ✅ What is implemented

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

#### ✅ Persistence layer (SQLModel + Alembic)

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

#### 🔲 Remaining work

1. `/jobs/{id}/inputs` — inputs are stored but never exposed via a dedicated route.
2. **Status history endpoint** — `job_status_history` table receives writes via `StatusHistoryObserver`, but no endpoint exposes the history. Add `GET /jobs/{id}/history` or include history in the job detail response.
3. **Test coverage gap** — retry-exhaustion path (forward retries exhausted → `failed` diagnostic) is not yet exercised. All other planned paths are covered: polling timeout ✅, immediate results ✅, link normalization ✅, results endpoint ✅, polling stop conditions ✅, auth/JWT ✅, job visibility ✅.
4. **Process-description-aware input validation (Feature X)** — `ExecuteRequest` validates structure only. A future `ValidateInputsStep` (opt-in via `UMP_VALIDATE_EXEC_REQUESTS=true`) should validate each input against its `ProcessInput.scheme: Schema` from the cached process description. Deferred because: (a) process description may not be cached yet; (b) `oneOf`/`anyOf` requires a JSON Schema evaluator; (c) operators may need lax mode for non-spec-compliant servers.

#### Design notes: job history / CQRS decision

Chosen approach: CRUD `jobs` table + append-only `job_status_history` table (hybrid). This gives fast reads, simple writes, a full audit trail, and replay capability for most needs, without the complexity of full CQRS or event sourcing. Migration path to CQRS is available if/when advanced projections or heavy scaling become necessary.

##### OGC schema reference

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

### ✅ Feature IV: JWT-based Auth (user → UMP)

#### Scope

User-to-UMP authentication via JWT (OIDC standard). Distinct from Feature VII (UMP → remote servers). Supports any OIDC-compliant IdP (Keycloak, Auth0, Okta, Azure AD, …) with no IdP-specific adapter — differences in claim location are handled by configuration.

#### Authentication flow

```
Client              UMP                     IdP (Keycloak / any OIDC)
  │                   │                              │
  │─ GET /token ───────────────────────────────────►│
  │◄─ access_token (JWT, RS256-signed) ─────────────│
  │                   │                              │
  │─ POST /processes/{id}/execution                  │
  │  Authorization: Bearer <token> ──────────────►  │
  │                   │                              │
  │          [offline validation]                    │
  │          1. fetch JWKS if not cached             │
  │             (once, TTL-based refresh)            │
  │          2. verify signature (RS256/ES256)       │
  │          3. check exp, nbf, iss, aud             │
  │          4. extract sub → user_id                │
  │          5. extract roles from configured claims │
  │                   │                              │
  │          [authorization check]                   │
  │          does user have role for this process?   │
  │                   │                              │
  │◄─ 201 / 401 / 403 │                              │
```

No call to the IdP on every request. JWKS keys are cached and refreshed on TTL expiry or when an unknown `kid` is seen (key rotation defence).

##### What UMP validates in every token

| Check | Claim | Detail |
|---|---|---|
| Signature | header + `kid` → JWKS | Proves IdP origin |
| Expiry | `exp` | Reject stale tokens |
| Issuer | `iss` | Must equal `UMP_JWT_ISSUER` |
| Audience | `aud` | Must contain `UMP_JWT_AUDIENCE` |
| Not-before | `nbf` | Edge case; reject future-dated tokens |
| Clock skew | — | ±30 s tolerance on `exp`/`nbf` |

**UMP does NOT handle**: token refresh (client's job), token revocation before expiry, session management — JWTs are stateless.

##### JWKS caching and key rotation

```
On startup / cache miss:
  fetch {UMP_JWKS_URL}  ─►  parse key set  ─►  store in cache (TTL = UMP_JWKS_CACHE_TTL_SECONDS)

Per request:
  decode JWT header → kid
  if kid in cache  → verify signature
  else             → re-fetch JWKS (key rotation)
                     if still not found → 401
```

##### Authorization rules (role-based)

Roles are extracted from the token and compared against a two-level rule:

- **Provider role** `{provider_name}` — grants access to execute any process from that provider
- **Process role** `{provider_name}:{process_bare_id}` — grants access to one specific process

Examples with canonical UMP process IDs:
- Role `fair2adapt` → can execute all `fair2adapt:*` processes
- Role `fair2adapt:pluvial-flood-risk-citywide` → can execute only that process

Additionally:
- Processes with `anonymous-access: true` in `providers.yaml` are executable without a token
- `GET /processes` and `GET /processes/{id}` are optionally public via `UMP_PUBLIC_PROCESSES=true`
- All other routes (jobs, execution) require a valid token when `UMP_AUTH_ENABLED=true`

When `UMP_AUTH_ENABLED=false` (development/testing):
- All tokens are silently ignored (including malformed ones)
- Every request is treated as having full access

Anonymous-access jobs store `user_id = null` — no user tracking required.

##### Hexagonal architecture split

**Core** (`src/ump/core/interfaces/auth.py`):
```python
@dataclass
class AuthContext:
    user_id: Optional[str]    # None for anonymous requests
    roles: List[str]          # flat list merged from all configured claim paths
    is_authenticated: bool

class AuthPort(ABC):
    @abstractmethod
    async def verify(self, token: Optional[str]) -> AuthContext:
        """Validate token and return context.
        
        Returns AuthContext(is_authenticated=False) when auth is disabled or
        token is absent and the route allows anonymous access.
        Raises OGCProcessException(401) for invalid/expired tokens.
        """
```

**Adapter** (`src/ump/adapters/jwt_auth_adapter.py`):
- Fetches + caches JWKS from `UMP_JWKS_URL`
- Verifies RS256/ES256 signatures using `python-jose[cryptography]`
- Validates standard claims (`exp`, `nbf`, `iss`, `aud`)
- Walks configured `UMP_JWT_ROLES_CLAIMS` paths to extract and merge roles
- Implements `UMP_AUTH_ENABLED=false` bypass

No Keycloak-specific code. Operators configure claim paths for their IdP.

**FastAPI wiring** — dependency injection per route (not middleware):
```python
# fastapi.py
async def _require_auth(request: Request) -> AuthContext:
    token = _extract_bearer_token(request)  # None if header absent
    return await app.state.auth_port.verify(token)

@api_router.post("/processes/{process_id}/execution")
async def execute_process(request: Request, process_id: str,
                          auth: AuthContext = Depends(_require_auth)):
    _check_process_access(auth, process_id, app.state.process_port)
    ...

def _check_process_access(auth: AuthContext, process_id: str, process_port) -> None:
    """Raise 403 if the user lacks access to this process."""
    # resolve provider_name from process_id
    provider_name, _ = process_id.split(":", 1) if ":" in process_id else (None, process_id)
    # anonymous-access bypass (read from provider config)
    proc_config = process_port.get_process_config(provider_name, process_id)
    if proc_config and proc_config.anonymous_access:
        return
    if not auth.is_authenticated:
        raise OGCProcessException(401, ...)
    if provider_name not in auth.roles and process_id not in auth.roles:
        raise OGCProcessException(403, ...)
```

`Depends` is preferred over middleware: individual routes declare their auth requirement; public routes simply don't inject the dependency.

##### Configuration settings

| Setting | Description | Default |
|---|---|---|
| `UMP_AUTH_ENABLED` | Master switch; `false` disables all auth | `true` |
| `UMP_JWKS_URL` | JWKS endpoint (e.g. `https://keycloak:8080/.../certs`) | required |
| `UMP_JWT_ISSUER` | Expected `iss` claim | required |
| `UMP_JWT_AUDIENCE` | Expected `aud` claim (client ID) | required |
| `UMP_JWT_ROLES_CLAIMS` | Comma-separated dot-paths to role arrays in token | `realm_access.roles` |
| `UMP_JWKS_CACHE_TTL_SECONDS` | Public-key cache TTL | `3600` |
| `UMP_PUBLIC_PROCESSES` | Allow unauthenticated process discovery | `false` |

**Keycloak example** (both realm and client roles):
```
UMP_JWT_ROLES_CLAIMS=realm_access.roles,resource_access.ump-client.roles
```

**Azure AD example** (flat roles claim):
```
UMP_JWT_ROLES_CLAIMS=roles
```

##### Library

**`python-jose[cryptography]`** — standard FastAPI JWT library, supports RS256/ES256, parses all standard claims, no IdP dependency.

JWKS fetching uses `aiohttp` (already a dependency) with a simple in-memory async cache.

##### Files to create / modify

| File | Action | Notes |
|---|---|---|
| `src/ump/core/interfaces/auth.py` | CREATE | `AuthPort` + `AuthContext` |
| `src/ump/adapters/jwt_auth_adapter.py` | CREATE | Generic OIDC adapter, configurable claim paths, JWKS cache |
| `src/ump/core/settings.py` | MODIFY | Add `UMP_AUTH_ENABLED`, `UMP_JWKS_URL`, `UMP_JWT_ISSUER`, `UMP_JWT_AUDIENCE`, `UMP_JWT_ROLES_CLAIMS`, `UMP_JWKS_CACHE_TTL_SECONDS`, `UMP_PUBLIC_PROCESSES` |
| `src/ump/adapters/web/fastapi.py` | MODIFY | Add `_require_auth` dependency; inject `auth_port`; add `_check_process_access` helper |
| `src/ump/main.py` | MODIFY | Instantiate `JwtAuthAdapter`; wire into `create_app` |

### 🔲 Feature V: Result storage (ldproxy first)

> **Code-quality mandate for this feature.** This is an exceptionally
> substantial feature. Optimize the code for a human reader: names and control
> flow should mirror how a person reasons about the problem ("fetch the result,
> write a GeoPackage, register a collection, hand back a link"). Every module,
> public function, and non-obvious decision carries a docstring/comment
> explaining *why*, not just *what*. Prefer small, single-purpose, well-named
> functions over clever density. This is written down from the start, not
> retro-fitted.
> Try to be concise when describing obvious things and concepts and manage verbosity.
> Reduce using defensive null-checks and type guards that don`match actual invariants.
> Write minimal, idiomatic code.

**Principle (core vs adapter split):**
- **UMP core decides WHAT and WHEN** to store — driven by the process's
  `transmission-mode-policy` (Feature VIII) and the client's requested
  `transmissionMode`. The core never knows what a GeoPackage or an ldproxy
  entity file is.
- **The result-storage adapter decides HOW** — the ldproxy adapter turns a
  fetched GeoJSON result into a per-job GeoPackage + per-job provider entity
  file, registers a collection in the single shared service entity file, and
  returns a public reference URL.

#### ⚠️ Dependency: a minimal slice of Feature VIII is a prerequisite

The core can only decide *whether* to store a result if it knows two things at
job-completion time:

1. What the client asked for — `response` (raw/document) and per-output
   `transmissionMode` (value/reference). These are **not currently persisted**
   (Feature VIII Gap 1). We must first store `job.response_mode` and
   `job.outputs_spec` on the job (fields + Alembic migration).
2. The process's `transmission-mode-policy` (`pass-through` / `emulate-ref` /
   `emulate-ref-only` / `value-only`) — a new field on `ProcessConfig`.

**Sequencing:** implement the Feature VIII "capture + policy resolution" slice
(steps V-0a/V-0b below) before the storage adapter. Storage is meaningless
without the decision inputs.

#### Storage trigger: eager at job completion (recommended)

Two options were considered:

| Option | When | Pros | Cons |
|---|---|---|---|
| Lazy | first `GET /jobs/{id}/results` | store only if read | slow first GET; `results` link can't be a ref until first read |
| **Eager** | on terminal `successful` | ref link ready immediately in statusInfo; matches Feature VIII "intercept the data flow" | stores even if never read |

**Recommendation: eager**, gated by the decision inputs — we only store when the
resolved policy + client request actually require a `ref` link. A job that
asked for `value` never triggers storage. This keeps eager storage cheap.

Implementation: a `ResultStorageObserver` (new `JobStateObserver`) reacts to
`on_job_completed`; if the job's resolved policy demands a stored reference, it
invokes the coordinator. This keeps the trigger out of the poll loop hot path
and reuses the existing observer wiring.

#### Core additions

```
src/ump/core/interfaces/result_storage.py
  ResultPayload      — dataclass: output_id, body_bytes, media_type
  StoredReference    — dataclass: collection_url, items_url, collection_id
  ResultStoragePort  — ABC:
      async store(job_id, payloads: list[ResultPayload]) -> list[StoredReference]
      async delete(job_id) -> None
      async exists(job_id) -> bool
  UnsupportedResultError / ResultStorageError  — exceptions

src/ump/core/services/result_storage_coordinator.py
  Pure orchestration (no I/O of its own beyond the injected ports):
    1. decide if storage is required (policy + client request)
    2. fetch result bytes from remote via HttpClientPort.get_content()
    3. hand payloads to ResultStoragePort.store()
    4. inject the returned ref links into the job's `links` / results response
    5. per-policy failure handling (see below)
```

The coordinator is injected into `JobManager` (replacing the current untyped
`result_storage_port: Optional[Any]` placeholder with the real port).

#### Adapter: `LdproxyResultStorage`

```
src/ump/adapters/result_storage/ldproxy_adapter.py         — LdproxyResultStorage(ResultStoragePort)
src/ump/adapters/result_storage/gpkg_writer.py             — GeoJSON FeatureCollection -> GeoPackage table
src/ump/adapters/result_storage/ldproxy_entities.py        — build provider YAML + one collection block
src/ump/adapters/result_storage/service_registry.py        — read-modify-write the shared service entity (locked)
src/ump/adapters/result_storage/atomic_fs.py               — atomic write (temp file + os.replace)
src/ump/adapters/result_storage/entity_config_backend.py   — EntityConfigBackendPort ABC + factory
src/ump/adapters/result_storage/entity_config_fs.py        — FilesystemEntityConfigBackend (dev / Docker)
src/ump/adapters/result_storage/entity_config_k8s.py       — K8sConfigMapEntityConfigBackend (Kubernetes)
```

**Topology (CONFIRMED): one ldproxy instance, one service, many providers, many
collections.** ldproxy composes an API from three levels:

1. **one service** = the common API root (`ump-results`), served at a single
   base such as `https://geodata.MY_DOMAIN/ump-results`;
2. **one provider per GeoPackage** (i.e. one provider per job);
3. **each provider exposed as one or more collections** *inside that same
   service*.

So the shared service root fans out to many datasets, exactly like the ldproxy
example:

```
/ump-results/collections/{collection-a}/items      # provider A (job A's gpkg)
/ump-results/collections/{collection-b}/items      # provider B (job B's gpkg)
```

There is exactly **one** service entity file (`ump-results.yml`) that UMP owns
and mutates; each job contributes **one provider file + one gpkg**, and
registers **one collection per output** (each bound to that job's provider) into
the shared service file.

**Storage layout (authoritative — UMP owns these paths):**

```
{root}/resources/features/{job_uuid}.gpkg                     # one per job
{root}/entities/instances/providers/{job_uuid}.yml           # one per job
{root}/entities/instances/services/ump-results.yml           # SINGLE shared file, collections map edited
```

`{root}` = `UMP_RESULTSTORE_LDPROXY_ROOTPATH` (the mounted Azure File share; a
local dir in dev). Follows the user's stated layout
(`/data/resources/features/`, `/data/entities/instances/{providers|services}`).
UMP **bootstraps** `ump-results.yml` on startup if it is missing (global `api:`
building blocks + empty `collections:` map), then only ever mutates the
`collections:` map.

**Entity / id model:**
- `provider id = gpkg filename = job UUID`.
- `collection id = job UUID` (single output) or `{job_uuid}_{output_id}`
  (multiple outputs) — the UUID keeps it globally unique within the shared
  service *and* unguessable (the only access control we have today, see
  security note).
- Public ref link:
  `{UMP_RESULTSTORE_LDPROXY_BASE_URL}/collections/{collection_id}/items`
  (the base already ends in `/ump-results`).

**Provider YAML** generated per the attached documented example:
`providerType: FEATURE`, `providerSubType: SQL`, `connectionInfo.dialect: GPKG`,
`database: {job_uuid}.gpkg`, one `types.{output_id}` entry per output with an
`OBJECTID` integer primary key (the GeoPackage fid), a `Shape`
`PRIMARY_GEOMETRY`, and one typed property per GeoJSON attribute.

**Service YAML** (`ump-results.yml`): `serviceType: OGC_API`; static global
`api:` building blocks (`SCHEMA`, `QUERYABLES`, `FILTER`, `CRS`, `FLATGEOBUF` —
matches example, gives clients filtering/subsetting, the core motivation from
Feature VIII). Each stored collection adds a `collections.{collection_id}` entry
with a `FEATURES_CORE` building block bound to `featureProvider: {job_uuid}` and
`featureType: {output_id}`.

**CRS:** OGC GeoJSON is WGS84 / CRS84 by RFC 7946, so `nativeCrs.code: 4326`
unless the result declares otherwise. Documented assumption.

**Schema derivation** (`gpkg_writer` + `ldproxy_entities`):
- geometry type from the features (homogeneous → e.g. `MULTI_POLYGON`; mixed →
  `GEOMETRY`).
- property types mapped GeoJSON→ldproxy: int→`INTEGER`, float→`FLOAT`,
  bool→`BOOLEAN`, str→`STRING`, ISO date→`DATETIME`.

#### Defensive concerns (explicit)

1. **Write ordering & atomicity** — ldproxy watches the store and must never
   read a half-written file, or a service collection that references a
   not-yet-written provider/gpkg. Strict order, each individual file written via
   atomic `os.replace` from a temp file on the *same* filesystem:
   1. write `.gpkg`  2. write provider `.yml`  3. register collection in
   `ump-results.yml` (read-modify-write, see concern 9).
   On any failure, clean up temp files and raise `ResultStorageError`.
2. **Shared-service-file concurrency (the topology's main hazard)** — with a
   single `ump-results.yml`, two jobs completing at once do a read-modify-write
   of the *same* file. Across multiple UMP pods on one Azure File share an
   in-process `asyncio.Lock` is **not** enough — a lost update would drop a
   collection. The `service_registry` therefore serializes every edit behind a
   cross-process lock. Reuse the existing advisory-lock infrastructure
   (`PollLockPort` / `PgAdvisoryPollLock`) with a single fixed key for the
   `ump-results` registry; a `NoOpLock` covers single-instance/dev. Read →
   mutate collections map → atomic-replace the whole file, all under the lock.
3. **Idempotency** — keyed by job UUID; re-storing overwrites the gpkg/provider
   deterministically and upserts the collection entry. `exists(job_id)`
   short-circuits a redundant store.
4. **Non-geospatial outputs** — only GeoJSON `FeatureCollection` outputs can be
   stored. Others raise `UnsupportedResultError`. Per-policy handling:
   - `emulate-ref`: fall back to proxying the value inline (client allowed value).
   - `emulate-ref-only`: hard error (value is blocked) → 502 results-unavailable.
5. **Storage failure** — never silently mark the *job* failed (the computation
   succeeded). Under `emulate-ref` fall back to value; under `emulate-ref-only`
   return an explicit results error. Log with the request id.
6. **Memory** — first implementation loads the full FeatureCollection into
   memory (geopandas/pyogrio). Flagged as a known limit; streaming large results
   straight into the gpkg is a later optimization (ties into Feature VIII
   "stream before fully received").
7. **Config activation / reload** — CONFIRMED that ldproxy auto-reloads entity
   config from the store, so activation is a no-op `StoreWatchActivation` for
   now. Note: rewriting the shared `ump-results.yml` triggers a reload of the
   whole service; ldproxy handles this hot-reload gracefully — accepted for v1.
   Restarting the pod per job is explicitly rejected.
8. **ConfigMaps vs File share — revised understanding** — the previous note
   said "ConfigMaps only for static global config". That is now revised:
   - **GeoPackage files** are binary and can be multi-MB → always on the
     **Azure File share**, never in a ConfigMap.
   - **ldproxy entity YAMLs** (provider + service) are small text files →
     environment-dependent:
     - *Local / Docker*: written to the filesystem path under
       `UMP_RESULTSTORE_LDPROXY_ROOTPATH` (fast, no API dependency).
     - *Kubernetes*: created/patched as **Kubernetes ConfigMaps** via the
       k8s API (see concern 10). The ConfigMaps are mounted as files into
       the ldproxy pod via a projected volume; ldproxy sees them as ordinary
       files and auto-reloads.
   The backend is selected at startup via `UMP_RESULTSTORE_CONFIG_BACKEND`.
9. **Cleanup** — `delete(job_id)` removes the gpkg from the file share,
   deletes the provider entity (file or ConfigMap), and deregisters the
   collection from the service entity (file patch or ConfigMap patch),
   then relies on auto-reload. Wire into anonymous-job/expiry cleanup.
10. **Kubernetes ConfigMap backend — design and hazards** — when
    `UMP_RESULTSTORE_CONFIG_BACKEND=k8s`, entity writes go through
    `K8sConfigMapEntityConfigBackend`, which uses the official
    `kubernetes` Python client (in-cluster `ServiceAccount` credentials).

    *Per-job provider entity*: each job gets its own ConfigMap named
    `ump-ldproxy-provider-{job_uuid}` with a single data key
    `{job_uuid}.yml`. Create-or-replace is idempotent and has no
    concurrency conflict (one writer, one key).

    *Shared service entity* (`ump-results.yml`): stored in one ConfigMap
    named `ump-ldproxy-service`. Adding/removing a collection requires a
    read-modify-write of this ConfigMap. The k8s API enforces optimistic
    concurrency via `resourceVersion`: patch with the version read; if
    another UMP pod patched it first the API returns 409 Conflict → retry
    the read-modify-write loop (typically 1-2 retries under normal load).
    This replaces the file-based advisory lock needed for the filesystem
    backend — the k8s API server is the single serialisation point.
    Bootstrap (create if absent) on UMP startup.

    *RBAC*: the UMP `ServiceAccount` needs `get`, `create`, `patch`,
    `delete` on `configmaps` in the ldproxy namespace. Document as a
    required Helm values addition.

    *Volume mount — how ldproxy sees the ConfigMaps as files:* two viable
    approaches, both avoid a sidecar:

    - **Directory volume mount** (preferred): mount the ConfigMap as a plain
      `volume` / `volumeMount` in the ldproxy pod without `subPath` (i.e.
      mount the entire ConfigMap as a directory). The kubelet sync loop
      (default every 60 s, tunable via `--sync-frequency`) automatically
      propagates updates to mounted files and also picks up new keys added
      to an existing ConfigMap. A new provider ConfigMap created by UMP is
      therefore picked up without any pod restart.

    - **Pre-deployed static projected volume with pre-filled YAML literal**:
      deploy a skeleton ConfigMap for the service entity and one per
      pre-allocated provider slot via ArgoCD. Mount via projected volume.
      UMP only ever *patches* (updates) these pre-existing ConfigMaps — it
      never creates new ones. Kubelet propagates updates as above. Trade-off:
      requires pre-provisioning slots before jobs arrive, and caps
      simultaneous stored results to the number of pre-allocated slots. Only
      viable if the result set is bounded and known in advance.

    Recommended: **directory volume mount**. The kubelet propagation delay
    (up to ~60 s) between ConfigMap write and ldproxy seeing the file is
    acceptable given that result storage is a background post-completion step.
    Document as a Kubernetes deployment prerequisite.

#### Security (result access)

- Today: **collection id = job UUID = unguessable** is the only protection.
  Anyone with the link can read the collection. Acceptable interim per user.
- ldproxy is OIDC-secured at the *instance* level (same realm as UMP). That
  gates "is a valid user", not "is this the user who created the job".
- **Per-user result isolation is a future enhancement**: ldproxy PDP policies
  keyed on `ldproxy:collection:id` + a per-job permission claim. Requires UMP to
  provision a policy/permission per job. Out of scope for the first cut —
  documented as a known gap.

#### Configuration additions

`ProcessConfig` (`src/ump/core/models/providers_config.py`):
- extend `result_storage` Literal → `Literal["geoserver", "ldproxy", "remote"]`
- add `transmission_mode_policy` (Feature VIII) —
  `Literal["pass-through", "emulate-ref", "emulate-ref-only", "value-only"]`,
  default `pass-through`.

Settings (`src/ump/core/settings.py`):
- `UMP_RESULTSTORE_LDPROXY_BASE_URL` — public base for ref links, ending in
  `/ump-results` (reverse-proxied ldproxy).
- `UMP_RESULTSTORE_LDPROXY_ROOTPATH` — mounted store root (Azure File share /
  local dir). Required for both backends (gpkg always lives here).
- `UMP_RESULTSTORE_LDPROXY_NATIVE_CRS` — default `4326`.
- `UMP_RESULTSTORE_CONFIG_BACKEND` — `"filesystem"` (default) | `"k8s"`.
  Selects where entity YAML files are written.
- `UMP_RESULTSTORE_K8S_NAMESPACE` — Kubernetes namespace that holds the ldproxy
  ConfigMaps. Required when backend is `k8s`.
- `UMP_RESULTSTORE_K8S_SERVICE_CONFIGMAP` — name of the shared service
  ConfigMap, default `"ump-ldproxy-service"`.
- `UMP_RESULTSTORE_K8S_PROVIDER_CM_PREFIX` — name prefix for per-job provider
  ConfigMaps, default `"ump-ldproxy-provider-"`.

Startup validation (Feature VIII rules, enforced at config load):
- `emulate-ref` / `emulate-ref-only` without a configured store → **error**.
- `ldproxy` store without `UMP_RESULTSTORE_LDPROXY_BASE_URL` /
  `UMP_RESULTSTORE_LDPROXY_ROOTPATH` → **error**.

#### New dependency

GeoPackage writing needs GDAL bindings — **`geopandas` + `pyogrio`** (pragmatic,
well-maintained). Adds a non-trivial native dependency to the UMP image;
flagged as a deliberate decision. Alternative (raw `fiona`) noted but not
preferred.

Kubernetes entity config backend needs the **`kubernetes`** Python client
(`kubernetes` on PyPI). Added as an optional/conditional dependency; only
required when `UMP_RESULTSTORE_CONFIG_BACKEND=k8s`.

#### Implementation steps (sequenced, defensive)

| # | Step | Depends on |
|---|---|---|
| ~~V-0a~~ | ~~Persist `job.response_mode` + `job.outputs_spec` (+ Alembic migration)~~ | ✅ done |
| ~~V-0b~~ | ~~`ProcessConfig.transmission_mode_policy` + startup validation rules~~ | ✅ done |
| ~~V-1~~ | ~~`ResultStoragePort` + dataclasses + exceptions (core)~~ | ✅ done |
| ~~V-2~~ | ~~`ResultStorageCoordinator` (core, decide-fetch-store-linkinject)~~ | ✅ done |
| ~~V-3~~ | ~~`atomic_fs` + `gpkg_writer` (GeoJSON→gpkg) with unit tests over temp dir~~ | ✅ done |
| V-4 | `ldproxy_entities` (provider YAML + one collection block from a FeatureCollection) | V-3 |
| V-5a | `EntityConfigBackendPort` + `FilesystemEntityConfigBackend` (write entity YAMLs to disk) | V-4 |
| V-5b | `K8sConfigMapEntityConfigBackend` (write entity YAMLs as ConfigMaps, retry on 409) | V-4 |
| V-5c | `service_registry` (locked read-modify-write of shared service entity, works over both backends) | V-5a |
| V-6 | `LdproxyResultStorage` adapter wiring 3-5 together, atomic ordering | V-3, V-4, V-5c |
| V-7 | `ResultStorageObserver` (eager trigger on `on_job_completed`) | V-2 |
| V-8 | Compose in `asgi.py`; inject port + coordinator + observer + registry lock | V-2, V-6, V-7 |
| V-9 | `delete()` + cleanup wiring (anonymous/expiry, deregister collection) | V-6 |
| V-10 | Ref links in statusInfo `links` + `GET /results` returns 302/link | V-2 |

Steps V-3, V-4 and V-5a/c are pure/file-only and fully unit-testable against a
temp directory with no ldproxy or Kubernetes running — that is where most of
the defensive test coverage goes (schema derivation, atomic ordering, concurrent
registry edits under the lock, malformed/empty/mixed-geometry FeatureCollections,
409-retry loop). V-5b is tested against a mocked `kubernetes` client.

#### Decisions (confirmed by user 2026-07-24)

1. **Trigger:** eager on job completion. ✔
2. **Topology:** one ldproxy instance, one service (`ump-results`), many
   collections — base URL `https://geodata.MY_DOMAIN/ump-results`. UMP owns and
   mutates the single service file. ✔
3. **Reload:** ldproxy auto-reloads entity config from the store — rely on it
   (`StoreWatchActivation` no-op). ✔
4. **Dependency:** `geopandas` + `pyogrio` accepted. ✔

Remaining self-imposed check before coding: confirm the exact ldproxy field
names for the target ldproxy version against the two attached documented
examples as each entity file is generated.

### ✅ Feature VI: Job Execution Pipeline (implemented)

**Goal**: replace the monolithic `create_and_forward` method (~200 lines) with a composable `JobExecutionPipeline` of discrete, independently testable `PipelineStep` objects. Each step receives and mutates a shared `JobExecutionContext`; any step can abort by setting `context.should_halt = True`.

**Status**: pipeline is implemented and active. 
1. `ProcessManager.execute_process` calls `create_and_forward_ii` (pipeline entrypoint). The old `create_and_forward` remains as dead code and can be deleted in a cleanup pass. 
2. renamed `create_and_forward_ii` to create_and_forward and delete old `create_and_forward` dead code
3. renamed `create_and_forward` to `run_execution_pipeline`

**`ShapeClientResponseStep` currently implements the async row only** (201 + accepted statusInfo). The full OGC sync response table is deferred until sync execution is added (see deferred items below).

**Implemented steps** (`src/ump/core/managers/steps/execution_steps.py`):

| Step | Responsibility | Status |
|---|---|---|
| `ValidateAndResolveStep` | Validate process_id, resolve provider prefix; looks up verbatim remote ID from config | ✅ |
| `CreateLocalJobStep` | Create `Job` with UUID and inline inputs | ✅ |
| `PersistAcceptedStep` | Persist job + accepted snapshot; notify observers | ✅ |
| `ForwardToProviderStep` | POST to remote OGC endpoint with retry | ✅ |
| `HandleProviderResponseStep` | Detect upstream errors ≥ 400, propagate or absorb | ✅ |
| `DeriveStatusInfoStep` | Select derivation strategy via `StatusDerivationOrchestrator` | ✅ |
| `FinalizeJobStep` | Persist derived status, notify observers | ✅ |
| `ShapeClientResponseStep` | Async path: 201 + accepted statusInfo. Sync path deferred. | ✅ (async only) |
| `InitiatePollingStep` | Schedule background poll if non-terminal | ✅ |

**Deferred extension steps** (not yet implemented) 🔲:
- `ResolveOutputFormatsStep` — per-output `(media_type, is_binary)` from execute request + process description
- `ApplyTransmissionModePolicyStep` — rewrite `transmissionMode` per provider config
- `ApplyResponseModePolicyStep` — override `response` field sent to remote

**OGC execution response table** — `ShapeClientResponseStep` implements this:

| execution_mode | response_mode | transmissionMode | # outputs | HTTP code | Content-Type | Body |
|---|---|---|---|---|---|---|
| async | any | any | any | 201 | application/json | statusInfo |
| sync | raw | value | 1 | 200 | per output definition | raw output bytes |
| sync | raw | value | >1 | 200 | multipart/related | one part per output |
| sync | raw | reference | 1 | 204 | — | empty + Link headers |
| sync | raw | mixed | >1 | 200 | multipart/related | one part per output |
| sync | document | value | any | 200 | application/json | results document |

The decision belongs in core; the adapter only serialises the dict to HTTP (e.g., builds the multipart MIME body for `multipart/related` rows).

**Adapter/core boundary for sync**

The adapter currently passes `Prefer` as `headers["Prefer"]` but does NOT pass `exec_req.response` (raw vs document) or `exec_req.outputs` (per-output `transmissionMode`) as first-class parameters to the core's decision logic — they are buried inside `provider_payload` and forwarded to the remote without being used by UMP itself. To support `ShapeClientResponseStep`, the pipeline entrypoint needs:

```python
# What the adapter must extract and pass as first-class context (not just in provider_payload):
exec_req.response           # ResponseMode.raw | ResponseMode.document
exec_req.outputs            # Dict[output_id, OutputSpec] — contains transmissionMode per output
# Derived from Prefer header:
execution_mode              # "sync" | "async"
```

These become first-class fields on `JobExecutionContext` (see below); `ShapeClientResponseStep` reads them to select the correct row in the OGC table.

**`JobExecutionContext` fields** (updated — additions marked with ★):
```python
class JobExecutionContext(BaseModel):
    job: Optional[Job]               # set by CreateLocalJobStep
    process_id: str
    provider: Optional[ProviderConfig]  # set by ValidateAndResolveStep
    execute_payload: Dict[str, Any]  # normalized ExecuteRequest payload for the remote
    headers: Dict[str, str]          # forwarding headers (Prefer, etc.)
    # ★ first-class execution context (used by ShapeClientResponseStep, not forwarded)
    execution_mode: str = "async"    # "sync" | "async" — derived from Prefer header
    response_mode: str = "raw"       # "raw" | "document" — from ExecuteRequest.response
    output_specs: Dict[str, Any] = {}  # ExecuteRequest.outputs (transmissionMode per output)
    output_formats: Dict[str, Any] = {}  # resolved by ResolveOutputFormatsStep
    # pipeline control
    status_info: Optional[JobStatusInfo]  # set by DeriveStatusInfoStep
    should_halt: bool = False        # abort flag
    response: Optional[Dict] = None  # final response dict (set by ShapeClientResponseStep)
```

**Migration status**:
1. ✅ Implemented steps one at a time.
2. ✅ Wired into `_build_execution_pipeline()`.
3. ✅ Switched `ProcessManager.execute_process` to call `create_and_forward_ii`.
4. ✅ Delete `create_and_forward` (now dead code) and rename `_ii` → `create_and_forward`. -> renamed to `run_execution_pipeline`

**Minimal DDD scaffolding** (optional, for future evolution toward CQRS):
- `src/ump/core/commands.py` — `CreateJobCommand`, `ForwardExecutionCommand`, etc.
- `src/ump/core/events.py` — `JobCreated`, `JobForwarded`, `JobStatusUpdated`, `JobFailed`.
- `src/ump/core/aggregates/job_aggregate.py` — pure `handle_command`/`apply_event` with no IO.
- `append_event(event, expected_version)` on `JobRepositoryPort` (already exists as no-op).
These are optional and deferred unless complexity grows sufficiently to justify them.


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

Design trade-offs accepted in Step 1:
- Always async semantics (no sync shortcut yet) simplifies initial implementation; sync execute deferred.
- Polling interval is global; per-provider backoff not yet implemented.
- StatusInfo snapshots currently overwritten (history table planned to preserve transitions).
- Object storage integration postponed to keep test surface small.

### ✅ Feature VII: Remote server authentication (UMP → provider)

This is a distinct concern from Feature IV. Feature IV secures *inbound* requests (clients authenticating against UMP). Feature VII secures *outbound* requests (UMP authenticating against remote OGC API Processes servers).

#### Current state

`ProviderConfig.authentication: AuthConfig` already exists in the core model (`src/ump/core/models/providers_config.py`) with four variants:

| Type | Fields | Use case |
|---|---|---|
| `NoAuth` | — | Public servers (default) |
| `BasicAuth` | `user`, `password: SecretStr` | HTTP Basic Auth |
| `ApiKey` | `key_name`, `key_value: SecretStr` | API key header (any header name) |
| `BearerToken` | `token: SecretStr` | Static bearer token |

The data model is complete. What is missing is the port + adapter that converts `AuthConfig` to HTTP headers, and the wiring that applies them at every outbound call site.

#### Hexagonal architecture split

**Core** (no infrastructure knowledge):
- `ProviderCredentials` — a simple `dataclass` with `headers: Dict[str, str]`; result of credential resolution
- `RemoteAuthPort` — interface: `resolve(auth_config: AuthConfig) -> ProviderCredentials`

**Adapter** (owns encoding knowledge):
- `RemoteAuthAdapter` — implements `RemoteAuthPort`; converts each auth type to headers:
  - `BasicAuth` → `Authorization: Basic base64(user:pass)`
  - `BearerToken` → `Authorization: Bearer <token>`
  - `ApiKey` → `{key_name}: <key_value>` (uses the configured header name)
  - `NoAuth` → empty headers
  
  The adapter imports `base64`; the core does not. `HttpClientPort` stays unchanged — auth is expressed purely as additional `headers` entries.

#### Call sites that need auth headers

All four places where UMP makes outbound requests to a remote provider need the credentials merged into the request headers:

| Call site | File | Has `ProviderConfig`? |
|---|---|---|
| `ProcessManager._fetch_process` | `process_manager.py` | ✅ via `provider.authentication` |
| `ForwardToProviderStep.process` | `steps/execution_steps.py` | ✅ via `context.provider.authentication` |
| `JobManager._poll_and_update_status` | `job_manager.py` | ✅ via `job.provider` → `get_provider()` |
| `JobManager.get_results` | `job_manager.py` | ✅ via `job.provider` → `get_provider()` |

The pattern at each site is:
```python
auth_headers = self._remote_auth.resolve(provider.authentication).headers
merged_headers = {**auth_headers, **forward_headers}
await self._http.post(url, json=payload, headers=merged_headers)
```

#### Injection

`RemoteAuthAdapter` is stateless — a single shared instance injected at the composition root:

```python
# main.py
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
remote_auth = RemoteAuthAdapter()
# pass to process_manager_factory and job_manager_factory
```

`ProcessManager.__init__` and `JobManager.__init__` gain `remote_auth: RemoteAuthPort`; `ForwardToProviderStep.__init__` gains it too (injected via `_build_execution_pipeline()`).

#### Future extension: OAuth2 client credentials

When a provider requires dynamic token refresh (OAuth2 client credentials flow), add a new union member to `AuthConfig`:

```python
class OAuthClientCredentialsConfig(BaseModel):
    type: Literal["OAuthClientCredentials"]
    client_id: str
    client_secret: SecretStr
    token_url: HttpUrl
    scope: Optional[str] = None
```

The adapter's `resolve()` method fetches and caches a token using the `token_url`. The port signature (`resolve(AuthConfig) -> ProviderCredentials`) remains unchanged. No core changes needed.

#### Files to create / modify

| File | Action | Notes |
|---|---|---|
| `RemoteAuthPort` + `ProviderCredentials` | ✅ | `src/ump/core/interfaces/remote_auth.py` |
| `RemoteAuthAdapter` (BasicAuth, BearerToken, ApiKey, NoAuth) | ✅ | `src/ump/adapters/remote_auth_adapter.py` |
| `src/ump/core/managers/process_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in `_fetch_process` |
| `src/ump/core/managers/steps/execution_steps.py` | MODIFY | `ForwardToProviderStep` accepts + uses `RemoteAuthPort` |
| `src/ump/core/managers/job_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in polling and results proxy |
| `src/ump/main.py` | MODIFY | Instantiate `RemoteAuthAdapter`, inject into factories |


### Feature VIII: UMP as execution proxy: add or remove skills to/from remote Models

#### Context

Data is transferred from modelserver to UMP to client. The Problem: Large geodata and missing filtering. OGC API Processes v1.0.0 does not allow for subsetting or filtering of large geodata. The Results object is always passed as a block. Models can generate very large
geodata sets, which can cause bottlenecks.

An external result store that only comes into play **after** the data has been received by the UMP solves the
problem too late: for this to work, the data must already have flowed completely from `Model Server → UMP`.

The solution lies in expanding the UMP into an **execution proxy** that can actively
control the data flow-even before the data has completely passed through the UMP.

The UMP acts as a broker for the entire execution lifecycle according to OGC API Processes:

- `POST /processes/{id}/execution` - Receive and forward execution requests
- `GET /jobs` / `GET /jobs/{id}` - Federated job registry across all model servers
- `GET /jobs/{id}/results` - Intercept results and, if necessary, write them to an external store

**execution proxies central skills:**

- federated job registry
- central auth management
- process exclusion
- deterministic caching
- normalizing: UMP can add or remove capabilities from model servers — e.g. add
  `transmissionMode: reference` to the advertised process description, then
  fulfill it by constructing its own canonical execute request to the remote

**Two-layer proxy model (architectural clarification)**

UMP operates on two distinct levels simultaneously:

1. **Process description** — UMP is the *authoritative source* for the process
   descriptions it serves to clients.  It may rewrite the upstream process
   description to add or remove advertised capabilities.  Clients trust UMP's
   version; they never see the remote's raw description.

2. **Remote request construction** — When executing a job, UMP reads the
   client's *intent* (e.g. `transmissionMode: reference`) but does **not**
   forward the client's execute body to the remote unchanged once policies are
   active.  Instead, UMP constructs its own canonical request to the remote: one
   the remote can actually service (e.g. `transmissionMode: value`,
   `response: document`) that allows UMP to subsequently fulfil the client's
   original intent.  The client's **inputs** are forwarded unchanged; the
   execution-mode fields (`transmissionMode`, `response`) in the request UMP
   sends to the remote are determined by UMP's policy configuration, not copied
   from the client's body.

This proposal addresses `result-storage`, `transmission-mode-policy`, and `response-mode-policy`

#### Configuration: `transmission-mode-policy`

For each process, the `providers.yaml` file explicitly configures how the UMP handles the
`transmissionMode` parameter of the OGC standard. The parameter can take four possible values:

---

##### `pass-through`

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

##### `emulate-ref`

UMP adds `transmissionMode: reference` capability that the remote may not support
natively.
- **Process description**: UMP adds `transmissionMode: reference` to the
  advertised `outputTransmission` if it is not already present.  Clients see
  `reference` as a valid option regardless of remote capability.
- **Remote request** (when client requests `reference`): UMP constructs a
  canonical request with `transmissionMode: value` (and, if configured,
  `response: document`) — the remote receives a value request it can service.
  UMP stores the returned value in the result store, then returns a reference
  link to the client, fulfilling the client's original intent.
- **Remote request** (when client requests `value`): UMP forwards value-mode
  request to the remote.  The result store is not activated.

**Prerequisite:** A result store (`result-storage`) must be configured.
If missing, UMP rejects the config at startup.

---

##### `emulate-ref-only`

Like `emulate-ref`, but `value` is completely removed from the advertised
capabilities and from client options.
- **Process description**: `outputTransmission` is set to `["reference"]` only.
  Clients cannot request `value`.
- **Remote request**: UMP always constructs a canonical request with
  `transmissionMode: value` to the remote, stores the result, and returns a
  reference link.  A client that attempts to send `transmissionMode: value` is
  rejected by UMP before the request is forwarded.

**Prerequisite:** A result store (`result-storage`) must be configured.

---

##### `value-only`

UMP removes `reference` from the advertised capabilities and enforces value-only delivery.
- **Process description**: `outputTransmission` is set to `["value"]` only.
  Clients cannot request `reference`.
- **Remote request**: UMP constructs a canonical request with
  `transmissionMode: value`.  A client that attempts to send
  `transmissionMode: reference` is rejected by UMP.  The result store is not
  used.


#### Configuration: `result-storage`

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


#### Behaviour Overview

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


#### Validierungsregeln für die Konfiguration

When starting the UMP (or reloading `providers.yaml`), the configuration should be validated
as follows:

- `emulate-ref` without `result-storage` → **Error**
- `emulate-ref-only` without `result-storage` → **Error**
- `native` with `result-storage` → **Warning** (Store is ignored)
- `value-only` with `result-storage` → **Warning** (Store is ignored)
- Model can only be configured with `ref` or `result-storage` → **Error** (not supported)


#### Impact on the Process Description

The UMP is the **authoritative source** of the process description for all configured processes.
It may modify the process description provided by the model server:

| `transmission-mode` | Change to the process description |
|---|---|
| `native` | None - 1:1 forwarding |
| `emulate-ref` | `transmissionMode: ref` is added if not present |
| `emulate-ref-only` | `transmissionMode` is set to `[“ref”]` |
| `value-only` | `transmissionMode` is set to `[“value”]` |


This modification is intentional and must be transparent to UMP operators. Clients should act exclusively based on the Process Description and should not have to consult the model server’s Process description.

---

#### Configuration: `response-mode-policy`

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

#### OGC API Processes results response spec

The HTTP response shape for `GET /jobs/{id}/results` is fully determined by three fields from the original execute request: `response` (`"raw"` or `"document"`), per-output `transmissionMode` (`"value"` or `"reference"`), and the number of outputs requested.

**Full OGC table** (from OGC API - Processes, Part 1):

| Negotiated execute mode | `response` | `transmissionMode` | # outputs | HTTP code | `Content-Type` | Body |
|---|---|---|---|---|---|---|
| sync (server may create job anyway) | any | any | any | — | — | [job results re-fetchable as per async] |
| async | `raw` | `value` | 1 | 200 | as per output definition | raw output bytes |
| async | `raw` | `value` | >1 | 200 | `multipart/related` | one part per output |
| async | `raw` | `reference` | 1 | 204 | — | empty + `Link` headers |
| async | `raw` | mixed | >1 | 200 | `multipart/related` | one part per output |
| async | `document` | `value` | any | 200 | `application/json` | results document |
| async | `document` | `reference` | 1 | 200 | `application/json` | results document with links |

**Critical clarification — `response: "raw"` does NOT mean bytes:**

`"raw"` means the output is returned **without the OGC document envelope** — but the
content itself is whatever the output's `format.mediaType` declares.  A GeoJSON output
with `response: "raw"` returns JSON text.  A FlatGeobuf output returns binary bytes.
What the remote actually sends is determined by the per-output `format.mediaType`, not by
`response`.

`"document"` always returns a JSON wrapper — even binary outputs appear as base64-encoded
strings inside it.

**Consequence for UMP proxy (no local result storage):**

If UMP does not store results itself, it only needs to forward what the remote sends with
the `Content-Type` the remote declared.  No pre-prediction of content type is needed.
The correct proxy strategy is always:

```
body_bytes, content_type = await http.get_content(results_url)
return Response(content=body_bytes, media_type=content_type)
```

The remote's `Content-Type` response header carries the correct MIME type — UMP trusts it
and forwards it verbatim.  `response_mode` and per-output `format.mediaType` only matter
when UMP **stores or transforms** results (Feature VIII).

**`response: "raw"` with multiple outputs returns `multipart/related`** — each part is one output. UMP does not currently parse multipart responses; this case is deferred (see deferred items below).

#### Results document format (OGC reference)

A `response: "document"` result body is a JSON object with one key per output. Each value is one of:

- Scalar (inline): `"stringOutput": "Value2"` or `"doubleOutput": "3.14159"`
- Qualified value with optional `mediaType`: `{"value": "<gml:...>", "mediaType": "application/gml+xml"}`
- Inline base64 binary: `{"value": "VBORw0...", "encoding": "base64", "mediaType": "image/tiff"}`
- Reference link: `{"href": "https://...", "type": "application/geo+json"}`

UMP as a proxy does not need to parse this structure today — it proxies the JSON as-is. Only `response-mode-policy: force-document` (Feature VIII) requires UMP to unwrap specific values from the document envelope.

#### The problem

An OGC API Processes execute request body can declare, per output, which media type the client wants and how results should be transmitted:

```json
{
  "outputs": {
    "voronoi_diagram": {
      "format": {
        "mediaType": "application/flatgeobuf",
        "schema": "https://geojson.org/schema/FeatureCollection.json"
      },
      "transmissionMode": "value"
    },
    "classification_breaks": {
      "transmissionMode": "value"
    }
  },
  "response": "raw"
}
```

UMP, as a proxy, must be aware of the OGC table above in two places:

1. **Forwarding the execute request**: UMP forwards `outputs`, `response`, and all other fields to the remote server unchanged (as of the current implementation). No format resolution is needed at forward time — the remote handles OGC rules natively.

2. **Proxying the results** (`GET /jobs/{id}/results`): Since UMP does not store results,
   it only needs to forward what the remote sends with the `Content-Type` the remote
   declared.  No content-type prediction is required.  `response_mode` and
   `format.mediaType` are only needed when UMP **stores or transforms** results (Feature VIII).

#### What UMP needs to track per job

**For the basic proxy (no result storage — current state):**
Nothing beyond what is already stored. The remote's `Content-Type` response header is
the authoritative source; UMP forwards it verbatim via `get_content()`.

**For Feature VIII (result storage / policy enforcement):**
UMP must additionally capture the original execute request's `response` field and the raw
`outputs` map so it can apply `response-mode-policy` and `transmission-mode-policy`:

```
job.response_mode:  str                        — "raw" | "document" from execute body
job.outputs_spec:   Optional[Dict[str, Any]]   — verbatim execute body "outputs" map
```

And the fully resolved per-output format (for base64-decode decisions):

```
job.output_formats: dict[output_id, ResolvedOutputFormat]   ← Feature VIII only
  output_id:         str        — e.g. "voronoi_diagram"
  media_type:        str        — canonical IANA type, e.g. "application/geo+json"
  transmission_mode: str        — "value" | "reference"
  is_binary:         bool       — True for FlatGeobuf, GeoTIFF, PNG etc.
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

#### Three concrete gaps that are NOT yet addressed

##### Gap 1 — `response_mode` and `outputs_spec` not stored on the job ← **deferred to Feature VIII**

For the basic proxy path (no result storage), these are not needed — the remote's
`Content-Type` response header tells UMP everything required.

They become necessary when UMP applies `response-mode-policy` or stores results
(Feature VIII): only then does UMP need to know the original `response` and per-output
`format.mediaType` to decide whether to unwrap a document envelope or base64-decode a value.

##### Gap 2 — `HttpClientPort.get()` hard-fails on non-JSON content (current bug)

`AioHttpClientAdapter._fetch_json` calls `resp.json()` unconditionally. For any output
whose content is not JSON (FlatGeobuf, GeoTIFF, multipart, etc.), `resp.json()` raises
`ContentTypeError`, which UMP converts to a 502. This **currently breaks** the results
proxy for all non-JSON outputs.

The fix is simpler than initially designed. Since UMP is a pure proxy (no result storage),
it does not need to predict the content type in advance. Just always fetch raw bytes and
forward the remote's `Content-Type`:

**Fix:** Add `get_content(url) -> tuple[bytes, str]` to the port:

```python
# src/ump/core/interfaces/http_client.py
@abstractmethod
async def get_content(
    self,
    url: str,
    timeout: float | None = None,
    headers: Dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Fetch URL, returning (body_bytes, content_type).

    Never attempts JSON parsing. Always use this for the results proxy.
    The remote's Content-Type header is the authoritative source.
    """
```

`AioHttpClientAdapter.get_content` implementation:

```python
async def get_content(self, url, timeout=None, headers=None) -> tuple[bytes, str]:
    async with self._session.get(url, timeout=..., headers=headers or {}) as resp:
        resp.raise_for_status()
        return await resp.read(), resp.content_type or "application/octet-stream"
```

`JobManager.get_results()` always uses `get_content()`:

```python
body_bytes, ct = await self._http.get_content(results_url, headers=auth_headers)
return {"status": 200, "content_type": ct, "body_bytes": body_bytes}
```

The `GET /jobs/{id}/results` route in `fastapi.py`:

```python
if "body_bytes" in resp:
    return Response(
        content=resp["body_bytes"],
        media_type=resp.get("content_type", "application/octet-stream"),
    )
return JSONResponse(status_code=resp.get("status", 200), content=resp.get("body", {}))
```

No dispatch on `response_mode` or `is_binary` — the remote tells UMP what it sent.

**Deferred:** `response: "raw"` + multiple outputs → `multipart/related`. Bytes are
forwarded opaquely with `Content-Type: multipart/related`. Structured per-part handling
is deferred to Feature VIII.

##### Gap 3 — Binary media-type detection (scope reduced)

With the OGC spec clarified, `is_binary_media_type()` is no longer needed for the basic
proxy path: the `response_mode` field (`"raw"` vs `"document"`) alone determines whether
UMP receives bytes or JSON from the remote.  `is_binary` remains useful only for Feature
VIII (`response-mode-policy: force-document`), where UMP receives a JSON document but must
base64-decode a binary value before returning it to a client that requested `response: "raw"`.

For now, `src/ump/core/utils/media_types.py` is **deferred** to Feature VIII.  The proxy
path only needs `job.response_mode` and a count of value outputs.

---

#### ✅ Implemented (basic proxy)

| File | Change |
|---|---|
| `src/ump/core/interfaces/http_client.py` | ✅ `get_content(url, …) -> tuple[bytes, str]` |
| `src/ump/adapters/aiohttp_client_adapter.py` | ✅ `get_content` — `resp.read()` + `resp.content_type`, no JSON parsing |
| tests: fake `HttpClientPort` impls | ✅ `get_content` stub in all 4 fakes |
| `src/ump/core/managers/job_manager.py` | ✅ `get_results` uses `get_content`; returns `body_bytes` + `content_type` |
| `src/ump/adapters/web/fastapi.py` | ✅ results route returns `Response(content=bytes, media_type=ct)` |

#### 🔲 Deferred to Feature VIII (result storage / policy)

- `job.response_mode` + `job.outputs_spec` fields + DB migration
- `job.output_formats` (`ResolvedOutputFormat`) + `src/ump/core/utils/output_format_resolver.py`
- `is_binary_media_type()` helper (for base64-decode in force-document path)
- Structured `multipart/related` parsing

### Large input data: implementation strategies

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

### 🔲 Feature IX: Horizontal scaling — multi-instance poll coordination

#### Problem statement

UMP is designed to be horizontally scaled (≥2 instances behind a load balancer or in a Kubernetes deployment). The current polling architecture has two failure modes that make multi-instance deployment unsafe:

**Failure mode 1 — Poll orphaning (silent, high severity)**

`_poll_tasks` and `_active_poll_jobs` are plain Python in-memory sets scoped to one process. When Instance A creates job X and starts a poll loop, then Instance A restarts (rolling deploy, OOM kill, scale-down), no other instance picks up the orphaned poll. The database row sits at `status=running` with a populated `remote_status_url` indefinitely. The job never reaches a terminal state.

**Failure mode 2 — Duplicate polling (load amplification + data races)**

`_active_poll_jobs` only deduplicates within one process. Two instances can both start polling the same job (e.g., during a rolling deploy where old and new instances overlap). Both will:
- Fetch the remote status endpoint simultaneously (2× load on the model server)
- Both call `repo.update()` — the current implementation is a blind overwrite (`setattr` on all fields). `Job.version` exists in the DB schema and domain model but is not enforced in `UPDATE` conditions, so the last writer silently wins, potentially discarding an intermediate status snapshot.
- Both may call `StatusHistoryObserver.on_status_changed` for the same transition, writing duplicate rows to `job_status_history`.

#### What works correctly at ≥2 instances today

| Concern | Safe? | Reason |
|---|---|---|
| Job creation (`POST /execution`) | ✅ | UUID generation is instance-local; DB insert is atomic |
| Job reads (`GET /jobs`, `GET /jobs/{id}`) | ✅ | All instances read from the shared DB |
| Auth / JWKS cache | ✅ | Per-instance cache; fetches are idempotent |
| Process cache | ✅ | Cache miss → extra remote fetch; no correctness issue |
| Provider config file watcher | ✅ | Each instance watches independently |
| `status_history` writes | ⚠️ | Duplicate polling → duplicate history rows for the same transition |
| `repo.update()` under concurrency | ⚠️ | Last-writer-wins; `version` field not enforced |

#### Solution: PostgreSQL-native coordination (no new infrastructure)

Both problems are solved using facilities already in the database.

---

##### Fix 1 — Poll recovery on startup

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

##### Fix 2 — PostgreSQL advisory lock for poll-loop exclusivity

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

##### Fix 3 — Optimistic locking in `repo.update()`

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

#### Implementation plan

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

#### Files to create / modify

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

#### Non-goals

- **Redis / external lock store** — not required; PostgreSQL advisory locks are sufficient and avoid adding a new infrastructure dependency.
- **Single-owner job routing** (always route a job's requests to the same instance) — rejected; it ties scaling to sticky sessions and breaks when instances restart.
- **Distributed consensus (Raft, ZooKeeper)** — massively over-engineered for this use case.


# Ideas (not ordered, no exact location within the current implementation plan)
