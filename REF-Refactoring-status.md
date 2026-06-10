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
- [ ] Add `/jobs` (list) and `/jobs/{id}` (detail) endpoints.
- [ ] Implement inputs separation & `/jobs/{id}/inputs` (no inputs in statusInfo).
- [ ] SQLModel JobRepository + Alembic migrations (jobs + status history).
- [ ] Status history persistence (append snapshots) & optional events.
- [ ] Integrate `ResultStoragePort` for optional persistence on success.
- [ ] Tests: JobManager polling (including timeout), immediate results synthesis, retry verification, link normalization, /jobs results endpoint.
- [ ] Optional: adaptive polling/backoff per provider.
- [ ] Optional: auth rules (JWT) restricting job visibility.

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

This proposal adresses `result-storage` and `transmission-mode-policy`

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
