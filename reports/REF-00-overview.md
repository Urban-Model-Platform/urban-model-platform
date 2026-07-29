_Last_updated: 2026-07-23

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

---

## Feature Implementation Guide

### Index

| File | Content |
|---|---|
| [REF-F0-landing-page.md](REF-F0-landing-page.md) | Feature 0: Landing page |
| [REF-F1-api-versioning.md](REF-F1-api-versioning.md) | Feature I: API versioning |
| [REF-F2-processes.md](REF-F2-processes.md) | Feature II: /processes/{process_id} |
| [REF-F3-jobs.md](REF-F3-jobs.md) | Feature III: Execution proxy, Jobs, and Persistence |
| [REF-F4-jwt-auth.md](REF-F4-jwt-auth.md) | Feature IV: JWT-based Auth (user → UMP) |
| [REF-F5-result-storage.md](REF-F5-result-storage.md) | Feature V: Result storage |
| [REF-F6-execution-pipeline.md](REF-F6-execution-pipeline.md) | Feature VI: Job Execution Pipeline |
| [REF-F7-remote-auth.md](REF-F7-remote-auth.md) | Feature VII: Remote server authentication (UMP → provider) |
| [REF-F8-execution-proxy.md](REF-F8-execution-proxy.md) | Feature VIII: UMP as execution proxy + Output format awareness + Large input data |
| [REF-F9-horizontal-scaling.md](REF-F9-horizontal-scaling.md) | Feature IX: Horizontal scaling — multi-instance poll coordination |
| [REF-IDEAS.md](REF-IDEAS.md) | Ideas (not yet scheduled) |

### Small changes (no feature number)
- Improve logging usage across modules (inject `logger` where useful).
- Links in fetched processes are now optionally rewritten to local API links. This is controlled by the setting `UMP_REWRITE_REMOTE_LINKS`.
- A small utility `src/ump/core/utils/link_rewriter.py` performs the rewriting and is used by the manager.
- Fetched processes are passed through an explicit handler pipeline in `ProcessManager` (ID enforcement, link rewriting, and future handlers). This makes transformation/validation of remote process metadata explicit and extensible.
