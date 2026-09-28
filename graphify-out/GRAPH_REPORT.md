# Graph Report - .  (2026-09-28)

## Corpus Check
- 72 files · ~48,818 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 795 nodes · 1581 edges · 67 communities (32 shown, 35 thin omitted)
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 522 edges (avg confidence: 0.54)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]

## God Nodes (most connected - your core abstractions)
1. `JobManager` - 64 edges
2. `JobStatusInfo` - 51 edges
3. `Link` - 47 edges
4. `Job` - 43 edges
5. `ProcessManager` - 40 edges
6. `OGCExceptionResponse` - 39 edges
7. `JobExecutionContext` - 32 edges
8. `JobRepositoryPort` - 31 edges
9. `PipelineStep` - 30 edges
10. `TransientOGCError` - 30 edges

## Surprising Connections (you probably didn't know these)
- `Federated Job Registry` --implemented_by--> `JobRepositoryPort`  [INFERRED]
  reports/REF-F3-jobs.md → src/ump/core/interfaces/job_repository.py
- `Ambiguous Bare Process IDs` --depends_on--> `ProcessIdValidatorPort`  [INFERRED]
  reports/REF-F2-processes.md → src/ump/core/interfaces/process_id_validator.py
- `Job Link Normalization` --uses--> `Link`  [INFERRED]
  reports/REF-F3-jobs.md → src/ump/core/models/link.py
- `UMP_SUPPORTED_API_VERSIONS Setting` --implemented_by--> `UmpSettings`  [INFERRED]
  reports/REF-F1-api-versioning.md → src/ump/core/settings.py
- `UMP_AUTH_ENABLED Master Switch` --configured_in--> `UmpSettings`  [INFERRED]
  reports/REF-F4-jwt-auth.md → src/ump/core/settings.py

## Hyperedges (group relationships)
- **Nine implemented JobExecutionPipeline steps** — steps_execution_steps_validateandresolvestep, steps_execution_steps_createlocaljobstep, steps_execution_steps_persistacceptedstep, steps_execution_steps_forwardtoproviderstep, steps_execution_steps_handleproviderresponsestep, steps_execution_steps_derivestatusinfostep, steps_execution_steps_finalizejobstep, steps_execution_steps_shapeclientresponsestep, steps_execution_steps_initiatepollingstep [INFERRED]
- **Job state observers** — interfaces_observers_jobstateobserver, managers_observers_statushistoryobserver, managers_observers_pollingschedulerobserver, managers_observers_resultsverificationobserver [INFERRED]
- **Feature IX multi-instance poll coordination fixes** — doc_concept_poll_recovery_on_startup, doc_concept_poll_lock, doc_concept_optimistic_locking [INFERRED]
- **Provider AuthConfig variants** — models_providers_config_noauthconfig, models_providers_config_basicauthconfig, models_providers_config_apikeyauthconfig, models_providers_config_bearertokenauthconfig [INFERRED]

## Communities (67 total, 35 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.0
Nodes (30): JobManager, process(), Check if status info needs enrichment (status changed or fields missing)., Fetch remote results for a terminal successful job.          We never persist re, Single-attempt results fetch; wraps transient OGC errors for the retry adapter., Ensure a relative results link is present in statusInfo.links when successful., Guarantee a local self link (remove remote self/results with foreign job id)., Orchestrates job lifecycle: creation, forwarding, status derivation, polling, re (+22 more)

### Community 1 - "Community 1"
Cohesion: 0.0
Nodes (54): BaseModel, UMP as Authoritative Process Description, Provider Config Validation Rules, Minimal DDD / CQRS Scaffolding (optional), Deferred Pipeline Steps (output formats, transmission/response policy), OGC Execution Proxy, GeoServer Result Storage Adapter (WFS/WMS), GET /processes/{process_id} (+46 more)

### Community 2 - "Community 2"
Cohesion: 0.0
Nodes (48): JobManagerConfig, Configuration for JobManager behavior.      Consolidates global job execution se, OGCProcessException, OptimisticLockError, Raised when a DB update fails because another instance modified the row.      Ca, Base exception for OGC Process API errors., Composable Job Execution Pipeline, Exception (+40 more)

### Community 3 - "Community 3"
Cohesion: 0.0
Nodes (21): Ambiguous Bare Process IDs, Remote Link Rewriting (UMP_REWRITE_REMOTE_LINKS), Per-provider and Per-process Caching, Process Handler Pipeline (ID enforcement, defaults, sanitize), REF-F2 /processes/{process_id}, ProcessCache, ProcessListCache, Remove cache entries whose keys are no longer in valid_keys. (+13 more)

### Community 4 - "Community 4"
Cohesion: 0.0
Nodes (27): _ConfigFileHandler, ProviderConfigFileAdapter, Atomar und thread-safe die Provider-Konfiguration aktualisieren, bei Fehlern Rol, RemoteAuthAdapter — converts provider AuthConfig to HTTP headers.  This adapter, Stateless adapter — safe to share as a singleton.      Supported auth types:, RemoteAuthAdapter, Anonymous Access Processes, Outbound Remote Provider Authentication (+19 more)

### Community 5 - "Community 5"
Cohesion: 0.0
Nodes (36): ColonProcessId, StaticSiteInfoAdapter, Composition Root (asgi.py / main.py), Explicit Dependency Injection in main.py, Hexagonal Architecture (ports & adapters), Landing Page JSON Fallback (?f=json), JWKS Caching and Key Rotation, Landing Page (+28 more)

### Community 6 - "Community 6"
Cohesion: 0.0
Nodes (21): InMemoryJobRepository, In-memory implementation of JobRepositoryPort.  Thread-safe / async-safe using a, from_domain(), JobRecord, JobStatusHistoryRecord, SQLModel-backed implementation of JobRepositoryPort.  Two-model pattern (hexagon, Append-only audit log of every status transition for a job., Async PostgreSQL-backed job repository using SQLModel + asyncpg.      The caller (+13 more)

### Community 7 - "Community 7"
Cohesion: 0.0
Nodes (17): LoggingAdapter, Concrete logging adapter.      Delegates to Python's logging. It intentionally d, BaseSettings, DelegatingLogger, get_logger(), NoOpLogger, Core settings module.  Hexagonal note: the core must not depend on concrete adap, Prints the settings for debugging purposes (+9 more)

### Community 8 - "Community 8"
Cohesion: 0.0
Nodes (25): Status Derivation Strategy Pattern, HttpClientPort, Protocol defining the interface for status derivation strategies.          Each, Check if this strategy can handle the given context.                  Args:, Derive status information from provider response.                  Args:, StatusDerivationStrategy, Orchestrator for status derivation strategies.  Manages the chain of status deri, Orchestrates status derivation using a chain of strategies.          Evaluates p (+17 more)

### Community 9 - "Community 9"
Cohesion: 0.0
Nodes (15): _enrich_status_info(), _ensure_results_link(), _ensure_self_link(), _extract_status_info(), _halt(), _halt_with_accepted(), _is_inline_small(), _notify_observers_changed() (+7 more)

### Community 10 - "Community 10"
Cohesion: 0.0
Nodes (18): _401(), _503(), JwtAuthAdapter, JwtAuthAdapter — generic OIDC JWT authentication adapter.  Validates inbound Bea, Return the JWKS key that matches the token's 'kid' header., Fetch the JWKS endpoint and update the in-memory key cache., Walk each configured dot-path and merge the discovered role arrays., Client error: the token itself is invalid/expired. (+10 more)

### Community 11 - "Community 11"
Cohesion: 0.0
Nodes (14): NoOpPollLock, NoOpPollLock — always grants the lock.  Used for single-instance deployments, th, PgAdvisoryPollLock, PgAdvisoryPollLock — PostgreSQL session-level advisory lock.  Maps a job UUID to, Convert a UUID string to a signed int64 advisory lock key., PostgreSQL advisory lock for exclusive poll-loop ownership.      One persistent, _uuid_to_lock_key(), Duplicate Polling Failure Mode (+6 more)

### Community 12 - "Community 12"
Cohesion: 0.0
Nodes (11): Result of status derivation containing all extracted information.          Attri, StatusDerivationResult, Create failed status when statusInfo parsing fails., Synthesize successful statusInfo from immediate results., Follow Location header to fetch initial status snapshot., Resolve relative Location header to absolute URL., Extract statusInfo from response body., Create failed status when Location follow-up fails. (+3 more)

### Community 13 - "Community 13"
Cohesion: 0.0
Nodes (10): _coerce_level(), configure_logging(), _CorrelationIdFilter, generate_uvicorn_log_config(), _MaxLevelFilter, _MinLevelFilter, Central logging configuration utilities.  Adds a single composition-root driven, Return a uvicorn-compatible log_config dict that preserves UMP logger level. (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.0
Nodes (6): AioHttpClientAdapter, Fetch URL, returning (body_bytes, content_type).          Never attempts JSON pa, Async context manager entry, Async context manager exit, Fetch JSON from URL with OGC API-specific error handling.          Translates HT, HttpClientPort

### Community 15 - "Community 15"
Cohesion: 0.0
Nodes (7): Protocol for status derivation strategies.  Defines the interface for different, Context object containing all data needed for status derivation.          Encaps, StatusDerivationContext, Job, Domain Job model (internal) distinct from user-facing OGC statusInfo.      Notes, InitiatePollingStep, Schedule background polling if the job is non-terminal and has a remote status U

### Community 16 - "Community 16"
Cohesion: 0.0
Nodes (8): JobExecutionError, JobTimeoutError, Base exception for job execution failures.          Attributes:         message:, Raised when job exceeds configured timeout waiting for remote completion., Raised when remote provider returns error response or fails to respond., Raised when results cannot be fetched for a successful job.          This indica, RemoteProviderError, ResultsFetchError

### Community 17 - "Community 17"
Cohesion: 0.0
Nodes (7): JobStateObserver, Observer protocols for job state transitions.  This module defines the Observer, Observer protocol for job state transitions.          Implementations can react, Called after job is created with initial accepted status.                  Args:, Called after job status changes.                  Args:             job: The job, Called after job reaches terminal state.                  Args:             job:, Protocol

### Community 18 - "Community 18"
Cohesion: 0.0
Nodes (6): Concrete observer implementations for job state transitions.  This module provid, Verifies remote results are accessible for successful jobs.          Extracts re, Job creation doesn't trigger verification., Status changes don't trigger verification (wait for completion)., Verify remote results are accessible for successful jobs., ResultsVerificationObserver

### Community 19 - "Community 19"
Cohesion: 0.0
Nodes (7): Job State Observer Pattern, PollingSchedulerObserver, Terminal jobs don't need polling., Schedules background polling for running jobs.          Extracts polling schedul, Initialize with callback to JobManager._schedule_poll method.                  A, Job creation doesn't trigger polling (wait for status change)., Schedule polling if job is running with remote status URL.

### Community 20 - "Community 20"
Cohesion: 0.0
Nodes (4): ABC, PollingService, JobResultsPort, SiteInfoPort

### Community 21 - "Community 21"
Cohesion: 0.0
Nodes (5): Tenacity-based retry adapter implementing RetryPort.      Provides exponential b, TenacityRetryAdapter, _job_manager_factory(), _process_manager_factory(), ASGI entry point for production deployments.  Exposes the FastAPI application at

### Community 22 - "Community 22"
Cohesion: 0.0
Nodes (10): Alembic Migrations, Federated Job Registry, Hybrid CRUD + Append-only Status History (CQRS decision), Job ID Strategy (local UUID vs remote id), Job Link Normalization, OGC statusInfo / JobList Schema, Remote Status Polling, Status History Endpoint (pending) (+2 more)

### Community 23 - "Community 23"
Cohesion: 0.0
Nodes (5): Records all status changes to job repository.          Extracts status history r, Record initial status in history., Record status change in history., Terminal status already recorded in on_status_changed., StatusHistoryObserver

### Community 25 - "Community 25"
Cohesion: 0.0
Nodes (8): build_problem(), create_app(), Create the FastAPI app.      Adapters and concrete infrastructure (logging, repo, Returns a 400 problem response if process_id fails validation, else None., Re-schedule poll loops for jobs that survived a previous instance crash.      Ca, _recover_orphaned_polls(), render_problem(), validate_process_id()

### Community 29 - "Community 29"
Cohesion: 0.0
Nodes (3): migrate(), UMP command-line entry points.  Registered as Poetry scripts in pyproject.toml:, Run ``alembic upgrade head`` using credentials from environment variables.

## Knowledge Gaps
- **222 isolated node(s):** `Start uvicorn pointing at the ASGI module (single composition root).`, `ASGI entry point for production deployments.  Exposes the FastAPI application at`, `UMP command-line entry points.  Registered as Poetry scripts in pyproject.toml:`, `Run ``alembic upgrade head`` using credentials from environment variables.`, `Configuration models for core domain components.  This module provides Pydantic-` (+217 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **35 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.