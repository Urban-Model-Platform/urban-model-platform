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
| `JobExecutionPipeline` — all 9 steps implemented, active via `create_and_forward_ii` | ✅ | `src/ump/core/managers/steps/execution_steps.py` |

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



## Outstanding issues / TODOs

### small changes
- Improve logging usage across modules (inject `logger` where useful).
- Links in fetched processes are now optionally rewritten to local API links. This is controlled by the setting `UMP_REWRITE_REMOTE_LINKS`.
- A small utility `src/ump/core/utils/link_rewriter.py` performs the rewriting and is used by the manager.
- Fetched processes are passed through an explicit handler pipeline in `ProcessManager` (ID enforcement, link rewriting, and future handlers). This makes transformation/validation of remote process metadata explicit and extensible.

### feature extension
The following missing features must be implemented:

#### ✅ Feature 0: Landing page (completed)
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

#### ✅ Feature I: API versioning (implemented)

- Strategy: route-based versioning using path prefixes of the form `/v{major}.{minor}/` (for example `/v1.0/`). The landing page at `/` lists the available versions and links to each version's OpenAPI document (e.g. `/v1.0/openapi.json`) and docs (e.g. `/v1.0/docs`).
- Implementation notes:
  - Supported versions are configured via `app_settings.UMP_SUPPORTED_API_VERSIONS` (default: `["1.0"]`).
  - The web adapter (`src/ump/adapters/web/fastapi.py`) creates per-version FastAPI sub-apps and mounts them under `/v{version}` so endpoints like `/v1.0/processes` are available.
  - `src/ump/adapters/site_info_static_adapter.py` now advertises per-version routes on the landing page.
  - The landing template shows supported versions and links to their OpenAPI/docs.

This approach keeps the landing page at `/` (as required by the OGC draft) and makes breaking changes explicit by assigning them to a new version prefix.

#### ✅ Feature II: /processes/{process_id} (implemented)

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


#### ✅ Feature III: Execution proxy, Jobs, and Persistence

The goal of Feature III is to enable UMP to act as an OGC API Processes execution proxy: forwarding execution requests to remote model servers, maintaining a local federated job registry with full status lifecycle, and persisting jobs durably in PostgreSQL.

**Feature III is functionally complete for the core use case.** The remaining items are refinements and extensions.

##### Quick status

| Area | Status |
|---|---|
| Job model, ports, in-memory repo | ✅ |
| JobManager: forwarding, status derivation, polling, retry, timeout | ✅ |
| ExecuteRequest normalization | ✅ |
| /jobs, /jobs/{id}, /jobs/{id}/results routes | ✅ |
| POST /processes/{id}/execution route | ✅ |
| SQLModel JobRepository + Alembic migration | ✅ |
| Observer pattern (history, polling scheduler, results verification) | ✅ |
| /jobs/{id}/inputs endpoint | 🔲 |
| Status history reads (DB writes exist; no read endpoint yet) | 🔲 |
| Expanded test coverage | 🔲 |
| ResultStoragePort placeholder injection | 🔲 |
| Large-object input separation | 🔲 |

##### ✅ What is implemented

**Domain models**

- `Job` (`src/ump/core/models/job.py`): `id` (local UUID), `process_id`, `provider`, `remote_job_id`, `remote_status_url`, timestamps, `status`, `status_info` snapshot, inline `inputs`, `inputs_url`, `links`, `diagnostic`, `version`. Helper methods: `apply_status_info()`, `touch()`, `is_in_terminal_state()`. ID separation rationale documented in code (local UUID / remote id / public route id are kept distinct).
- `JobStatusInfo` / `StatusCode`: mirrors OGC `statusInfo.yaml` schema.
- `ExecuteRequest` (`src/ump/core/models/execute_request.py`): `from_raw()` factory normalizes inline/ref inputs, outputs, `response` mode, `transmissionMode`, and subscriber callbacks. `as_provider_payload()` converts to the wire format sent to the remote.

**Ports**

- `JobRepositoryPort` (`src/ump/core/interfaces/job_repository.py`): `create`, `get`, `update`, `list`, `mark_failed`, `append_status`, `append_event`.
- `JobStateObserver` (`src/ump/core/interfaces/observers.py`): `on_job_created`, `on_status_changed`, `on_job_completed`.

**Adapters**

- `InMemoryJobRepository` — async-safe, thread-safe, with optional JSON dump to `UMP_JOB_DUMP_DIR`. Used by default and in all tests.
- `SQLModelJobRepository` — PostgreSQL-backed via asyncpg. Selected when `UMP_JOB_STORE=postgres`. Uses two-model ORM pattern (see persistence notes below).

**JobManager** (`src/ump/core/managers/job_manager.py`)

Orchestrates the full async execution lifecycle via `create_and_forward`:
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
- `ResultsVerificationObserver` — attempts to fetch remote results for immediate-success jobs; downgrades to `failed` if unavailable.

**Routes** (on parent app and each versioned sub-app):
- `POST /processes/{id}/execution` → `JobManager.create_and_forward`
- `GET /jobs` → list all jobs (from repo)
- `GET /jobs/{id}` → current `statusInfo` snapshot
- `GET /jobs/{id}/results` → remote results proxy (404 if not successful)

**Link normalization**: always inject local `self` link with stable UUID; add `results` link on success; filter out any remote self/results links that contain foreign job identifiers.

**Lifecycle sequence (happy path)**:
1. `POST /processes/{id}/execution` with raw JSON body.
2. Web adapter parses body; `ExecuteRequest.from_raw` normalizes.
3. `ProcessManager.execute_process` delegates to `JobManager.create_and_forward`.
4. Job created locally (status=accepted); forwarded to provider.
5. StatusInfo derived; job updated (running/successful/failed).
6. Polling scheduled if non-terminal.
7. Returns HTTP 201 + `Location` header + current statusInfo body.

**ID strategy** (documented in code):
- Local UUID: internal canonical key; always used for public routes.
- Remote job id: stored for correlation/polling; never exposed externally.
- Public route id = local UUID (no leakage of provider semantics).

##### ✅ Persistence layer (SQLModel + Alembic)

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

##### 🔲 Remaining work

1. **`/jobs/{id}/inputs` endpoint** — inputs are stored on the `Job` record but never exposed via a dedicated route. Implement and segregate inputs from `statusInfo` (OGC compliance).
2. **Status history reads** — `job_status_history` table receives writes via `StatusHistoryObserver`, but no endpoint exposes the history. Add `GET /jobs/{id}/history` or include history in the job detail response.
3. **Test coverage** — expand: polling loop (including TTW timeout path), immediate results synthesis, retry exhaustion, link normalization invariants, `/jobs/{id}/results` edge cases.
4. **`ResultStoragePort` placeholder** — inject a no-op placeholder into `JobManager` at the composition root so the slot exists for Feature V adapters.
5. **Large-object input separation** — inputs above `inline_inputs_size_limit` should be stored externally (object storage) with a URL reference; see large input strategies below.
6. **Ambiguous bare process IDs** — current behavior picks the first matching provider; consider a deterministic policy (error on duplicates, or require fully-qualified IDs).

##### Design notes: large input data strategies

When processing large payloads (e.g. 4×30 MB = 120 MB geospatial data):

**Option 1 — Chunked Transfer Encoding (automatic, current baseline)**
aiohttp chunks large bodies automatically. Any standard HTTP server (RFC 7230) reassembles them. Works transparently — no code changes needed. Downside: full JSON still loaded into Python memory (~360–600 MB for 120 MB raw).

**Option 2 — URL/Href referencing (OGC-native)**
Send `{ "inputs": { "geospatial_data": { "href": "http://s3.../file.json" } } }`. Remote server fetches the reference on-demand. Eliminates local memory spike. Requires server-side support (`href` pattern) and stable external storage.

**Recommended future step**: add an input pre-processor that detects payloads above a configurable threshold (e.g. >100 MB) and automatically stores them externally, replacing inline data with `href` references before forwarding. -> REJECTED: not in-line with OGC API Processes (`transmissionMode: reference | value` determines when href-ing)

##### Design notes: job history / CQRS decision

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

#### ✅ Feature IV: JWT-based Auth (user → UMP)

##### Scope

User-to-UMP authentication via JWT (OIDC standard). Distinct from Feature VII (UMP → remote servers). Supports any OIDC-compliant IdP (Keycloak, Auth0, Okta, Azure AD, …) with no IdP-specific adapter — differences in claim location are handled by configuration.

##### Authentication flow

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

#### ✅ Feature VII: Remote server authentication (UMP → provider)

This is a distinct concern from Feature IV. Feature IV secures *inbound* requests (clients authenticating against UMP). Feature VII secures *outbound* requests (UMP authenticating against remote OGC API Processes servers).

##### Current state

`ProviderConfig.authentication: AuthConfig` already exists in the core model (`src/ump/core/models/providers_config.py`) with four variants:

| Type | Fields | Use case |
|---|---|---|
| `NoAuth` | — | Public servers (default) |
| `BasicAuth` | `user`, `password: SecretStr` | HTTP Basic Auth |
| `ApiKey` | `key_name`, `key_value: SecretStr` | API key header (any header name) |
| `BearerToken` | `token: SecretStr` | Static bearer token |

The data model is complete. What is missing is the port + adapter that converts `AuthConfig` to HTTP headers, and the wiring that applies them at every outbound call site.

##### Hexagonal architecture split

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

##### Call sites that need auth headers

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

##### Injection

`RemoteAuthAdapter` is stateless — a single shared instance injected at the composition root:

```python
# main.py
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
remote_auth = RemoteAuthAdapter()
# pass to process_manager_factory and job_manager_factory
```

`ProcessManager.__init__` and `JobManager.__init__` gain `remote_auth: RemoteAuthPort`; `ForwardToProviderStep.__init__` gains it too (injected via `_build_execution_pipeline()`).

##### Future extension: OAuth2 client credentials

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

##### Files to create / modify

| File | Action | Notes |
|---|---|---|
| `RemoteAuthPort` + `ProviderCredentials` | ✅ | `src/ump/core/interfaces/remote_auth.py` |
| `RemoteAuthAdapter` (BasicAuth, BearerToken, ApiKey, NoAuth) | ✅ | `src/ump/adapters/remote_auth_adapter.py` |
| `src/ump/core/managers/process_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in `_fetch_process` |
| `src/ump/core/managers/steps/execution_steps.py` | MODIFY | `ForwardToProviderStep` accepts + uses `RemoteAuthPort` |
| `src/ump/core/managers/job_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in polling and results proxy |
| `src/ump/main.py` | MODIFY | Instantiate `RemoteAuthAdapter`, inject into factories |

#### 🔲 Feature V: Add support for result storage
- add result storage business logic
- create an adapter for geoserver result storage (wfs, wms)
- create an adapter for ldproxy result storage (ogc api features)

#### ✅ Feature VI: Job Execution Pipeline (implemented)

**Goal**: replace the monolithic `create_and_forward` method (~200 lines) with a composable `JobExecutionPipeline` of discrete, independently testable `PipelineStep` objects. Each step receives and mutates a shared `JobExecutionContext`; any step can abort by setting `context.should_halt = True`.

**Status**: pipeline is implemented and active. `ProcessManager.execute_process` calls `create_and_forward_ii` (pipeline entrypoint). The old `create_and_forward` remains as dead code and can be deleted in a cleanup pass.

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

**Deferred extension steps** (not yet implemented):
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
4. 🔲 Delete `create_and_forward` (now dead code) and rename `_ii` → `create_and_forward`.

**Minimal DDD scaffolding** (optional, for future evolution toward CQRS):
- `src/ump/core/commands.py` — `CreateJobCommand`, `ForwardExecutionCommand`, etc.
- `src/ump/core/events.py` — `JobCreated`, `JobForwarded`, `JobStatusUpdated`, `JobFailed`.
- `src/ump/core/aggregates/job_aggregate.py` — pure `handle_command`/`apply_event` with no IO.
- `append_event(event, expected_version)` on `JobRepositoryPort` (already exists as no-op).
These are optional and deferred unless complexity grows sufficiently to justify them.


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


## Next non-immediate steps

- Add unit tests for `ProcessManager`, `ProcessCache`, and `ProviderConfigFileAdapter` (happy path + failure fallback).
- Add unit tests for the cache and manager (Task 10).

## How to run

### Install dependencies
```bash
poetry install
```

### Start the API server
```bash
ump                         # uses .env or environment variables
```

With PostgreSQL persistence:
```bash
UMP_JOB_STORE=postgres \
UMP_DATABASE_URL=postgresql+asyncpg://ump:ump@localhost:5432/ump \
ump
```

### Run database migrations
```bash
# Uses UMP_DATABASE_* env vars (or UMP_DATABASE_URL):
ump-migrate                 # upgrade head
ump-migrate downgrade -1    # any alembic subcommand passes through
```

### Start the mock OGC server (for local testing without a real model server)
```bash
PYTHONPATH=scripts .venv/bin/uvicorn scripts.mock_ogc_server:app --port 5001 --reload
```
Then set `providers.yaml` to point at `http://localhost:5001` with process ids `echo`, `hello-world`, `slow`, `failing-job`.

### Run tests
```bash
PYTHONPATH=src .venv/bin/pytest tests/ -q
```

### Docker Compose (dev environment)
```bash
docker compose -f docker-compose-dev.yaml up mock-ogc-server ump-db
ump-migrate
ump
```

## Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

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