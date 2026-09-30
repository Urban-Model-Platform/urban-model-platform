# Graph Report - ./src  (2026-09-30)

## Corpus Check
- 74 files · ~53,418 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1194 nodes · 2273 edges · 117 communities (59 shown, 58 thin omitted)
- Extraction: 68% EXTRACTED · 32% INFERRED · 0% AMBIGUOUS · INFERRED: 717 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Status Derivation Strategies|Status Derivation Strategies]]
- [[_COMMUNITY_Refactoring Reference Docs|Refactoring Reference Docs]]
- [[_COMMUNITY_Ldproxy Result Storage|Ldproxy Result Storage]]
- [[_COMMUNITY_GeoPackage Writer|GeoPackage Writer]]
- [[_COMMUNITY_Job Repository Persistence|Job Repository Persistence]]
- [[_COMMUNITY_JWT Auth & Authorization|JWT Auth & Authorization]]
- [[_COMMUNITY_Result Storage Coordinator|Result Storage Coordinator]]
- [[_COMMUNITY_Logging & Settings|Logging & Settings]]
- [[_COMMUNITY_Execution Pipeline Context|Execution Pipeline Context]]
- [[_COMMUNITY_Status Derivation Orchestrator|Status Derivation Orchestrator]]
- [[_COMMUNITY_K8s ConfigMap Backend|K8s ConfigMap Backend]]
- [[_COMMUNITY_Job Observers & Polling|Job Observers & Polling]]
- [[_COMMUNITY_Process Manager|Process Manager]]
- [[_COMMUNITY_Execution Steps|Execution Steps]]
- [[_COMMUNITY_Remote Provider Auth|Remote Provider Auth]]
- [[_COMMUNITY_Provider Config File|Provider Config File]]
- [[_COMMUNITY_Process Cache|Process Cache]]
- [[_COMMUNITY_Job Manager Polling Loop|Job Manager Polling Loop]]
- [[_COMMUNITY_Poll Lock & Scaling|Poll Lock & Scaling]]
- [[_COMMUNITY_Result Storage Factory|Result Storage Factory]]
- [[_COMMUNITY_OGC Process Models|OGC Process Models]]
- [[_COMMUNITY_Execute Request Models|Execute Request Models]]
- [[_COMMUNITY_Aiohttp Client Adapter|Aiohttp Client Adapter]]
- [[_COMMUNITY_Logging Configuration|Logging Configuration]]
- [[_COMMUNITY_Process Description Proxy|Process Description Proxy]]
- [[_COMMUNITY_Result Value Cache|Result Value Cache]]
- [[_COMMUNITY_Job Domain & Observers|Job Domain & Observers]]
- [[_COMMUNITY_Filesystem Entity Backend|Filesystem Entity Backend]]
- [[_COMMUNITY_Domain Exceptions|Domain Exceptions]]
- [[_COMMUNITY_Job Status Finalization|Job Status Finalization]]
- [[_COMMUNITY_Ldproxy Service Registry|Ldproxy Service Registry]]
- [[_COMMUNITY_Landing Page Template|Landing Page Template]]
- [[_COMMUNITY_Job Results Retrieval|Job Results Retrieval]]
- [[_COMMUNITY_Job Repository Port|Job Repository Port]]
- [[_COMMUNITY_Job Cleanup Service|Job Cleanup Service]]
- [[_COMMUNITY_Remote Forwarding & Retry|Remote Forwarding & Retry]]
- [[_COMMUNITY_Periodic Task Runner|Periodic Task Runner]]
- [[_COMMUNITY_FastAPI App Factory|FastAPI App Factory]]
- [[_COMMUNITY_Execution Mode Enums|Execution Mode Enums]]
- [[_COMMUNITY_Polling & Results Ports|Polling & Results Ports]]
- [[_COMMUNITY_Reference Storage Policy|Reference Storage Policy]]
- [[_COMMUNITY_Process ID Resolution|Process ID Resolution]]
- [[_COMMUNITY_Process ID Validator|Process ID Validator]]
- [[_COMMUNITY_Entity Config Backend Port|Entity Config Backend Port]]
- [[_COMMUNITY_Null Storage & Confirm Budget|Null Storage & Confirm Budget]]
- [[_COMMUNITY_Execution Entry Point|Execution Entry Point]]
- [[_COMMUNITY_Status History Observer|Status History Observer]]
- [[_COMMUNITY_API Extension Service|API Extension Service]]
- [[_COMMUNITY_Status Info Enrichment|Status Info Enrichment]]
- [[_COMMUNITY_Result Publication|Result Publication]]
- [[_COMMUNITY_CLI Entry Points|CLI Entry Points]]
- [[_COMMUNITY_Persist Accepted Step|Persist Accepted Step]]
- [[_COMMUNITY_Background Runner|Background Runner]]
- [[_COMMUNITY_Link Rewriting|Link Rewriting]]
- [[_COMMUNITY_ASGI Bootstrap|ASGI Bootstrap]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 90|Community 90]]
- [[_COMMUNITY_Community 91|Community 91]]
- [[_COMMUNITY_Community 92|Community 92]]
- [[_COMMUNITY_Community 93|Community 93]]
- [[_COMMUNITY_Community 94|Community 94]]
- [[_COMMUNITY_Community 95|Community 95]]
- [[_COMMUNITY_Community 96|Community 96]]
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 98|Community 98]]
- [[_COMMUNITY_Community 99|Community 99]]
- [[_COMMUNITY_Community 101|Community 101]]
- [[_COMMUNITY_Community 102|Community 102]]
- [[_COMMUNITY_Community 103|Community 103]]
- [[_COMMUNITY_Community 104|Community 104]]
- [[_COMMUNITY_Community 105|Community 105]]
- [[_COMMUNITY_Community 106|Community 106]]
- [[_COMMUNITY_Community 107|Community 107]]
- [[_COMMUNITY_Community 108|Community 108]]
- [[_COMMUNITY_Community 111|Community 111]]
- [[_COMMUNITY_Community 112|Community 112]]
- [[_COMMUNITY_Community 113|Community 113]]
- [[_COMMUNITY_Community 114|Community 114]]
- [[_COMMUNITY_Community 115|Community 115]]
- [[_COMMUNITY_Community 116|Community 116]]

## God Nodes (most connected - your core abstractions)
1. `JobManager` - 72 edges
2. `JobStatusInfo` - 54 edges
3. `Link` - 48 edges
4. `Job` - 47 edges
5. `ProcessManager` - 42 edges
6. `OGCExceptionResponse` - 42 edges
7. `JobExecutionContext` - 38 edges
8. `JobRepositoryPort` - 36 edges
9. `PipelineStep` - 35 edges
10. `TransientOGCError` - 35 edges

## Surprising Connections (you probably didn't know these)
- `UMP_SUPPORTED_API_VERSIONS Setting` --implemented_by--> `UmpSettings`  [INFERRED]
  reports/REF-F1-api-versioning.md → core/settings.py
- `Federated Job Registry` --implemented_by--> `JobRepositoryPort`  [INFERRED]
  reports/REF-F3-jobs.md → core/interfaces/job_repository.py
- `Ambiguous Bare Process IDs` --depends_on--> `ProcessIdValidatorPort`  [INFERRED]
  reports/REF-F2-processes.md → core/interfaces/process_id_validator.py
- `Anonymous Access Processes` --configured_in--> `ProcessConfig`  [INFERRED]
  reports/REF-F4-jwt-auth.md → core/models/providers_config.py
- `Job Link Normalization` --uses--> `Link`  [INFERRED]
  reports/REF-F3-jobs.md → core/models/link.py

## Hyperedges (group relationships)
- **Landing page render context (title, versions, links, contact)** — template_landing_page_template, template_var_supported_versions, template_var_links, template_var_contact [EXTRACTED 1.00]

## Communities (117 total, 58 thin omitted)

### Community 0 - "Status Derivation Strategies"
Cohesion: 0.06
Nodes (34): Status Derivation Strategy Pattern, Protocol for status derivation strategies.  Defines the interface for different, Result of status derivation containing all extracted information.          Attri, Protocol defining the interface for status derivation strategies.          Each, Check if this strategy can handle the given context.                  Args:, Derive status information from provider response.                  Args:, StatusDerivationResult, StatusDerivationStrategy (+26 more)

### Community 1 - "Refactoring Reference Docs"
Cohesion: 0.05
Nodes (46): Tenacity-based retry adapter implementing RetryPort.      Provides exponential b, TenacityRetryAdapter, StaticSiteInfoAdapter, Alembic Migrations, Composition Root (asgi.py / main.py), Provider Config Validation Rules, Deferred Pipeline Steps (output formats, transmission/response policy), OGC Execution Proxy (+38 more)

### Community 2 - "Ldproxy Result Storage"
Cohesion: 0.05
Nodes (34): What the result store returns after successfully persisting one output.      The, StoredReference, build_collection_block(), build_default_provider_entity(), _build_feature_type(), build_provider_entity(), build_provider_entity_multi(), build_service_skeleton() (+26 more)

### Community 3 - "GeoPackage Writer"
Cohesion: 0.06
Nodes (48): Raised when an output's media_type is not in the store's format whitelist., UnsupportedResultError, atomic_write_bytes(), atomic_write_path(), atomic_write_text(), Atomic filesystem write utilities.  Every file written by the ldproxy result sto, Write *data* to *path* atomically.      The destination directory must already e, Write *text* to *path* atomically.      The destination directory must already e (+40 more)

### Community 4 - "Job Repository Persistence"
Cohesion: 0.06
Nodes (21): InMemoryJobRepository, In-memory implementation of JobRepositoryPort.  Thread-safe / async-safe using a, from_domain(), JobRecord, JobStatusHistoryRecord, SQLModel-backed implementation of JobRepositoryPort.  Two-model pattern (hexagon, Append-only audit log of every status transition for a job., Async PostgreSQL-backed job repository using SQLModel + asyncpg.      The caller (+13 more)

### Community 5 - "JWT Auth & Authorization"
Cohesion: 0.06
Nodes (30): _401(), _503(), JwtAuthAdapter, JwtAuthAdapter — generic OIDC JWT authentication adapter.  Validates inbound Bea, Return the JWKS key that matches the token's 'kid' header., Fetch the JWKS endpoint and update the in-memory key cache., Walk each configured dot-path and merge the discovered role arrays., Client error: the token itself is invalid/expired. (+22 more)

### Community 6 - "Result Storage Coordinator"
Cohesion: 0.06
Nodes (30): Port: ResultStoragePort — the boundary between UMP core and result stores.  UMP, One output from the remote server, ready to be handed to a result store.      At, ResultPayload, _append_message(), _apply_stored_references(), _extract_payloads(), _extract_payloads_from_document(), _extract_single_raw_payload() (+22 more)

### Community 7 - "Logging & Settings"
Cohesion: 0.06
Nodes (17): LoggingAdapter, Concrete logging adapter.      Delegates to Python's logging. It intentionally d, BaseSettings, DelegatingLogger, get_logger(), NoOpLogger, Core settings module.  Hexagonal note: the core must not depend on concrete adap, # IMPORTANT: like UMP_RESULTSTORE_LDPROXY_BASE_URL, this must include the (+9 more)

### Community 8 - "Execution Pipeline Context"
Cohesion: 0.12
Nodes (33): JobManagerConfig, Configuration for JobManager behavior.      Consolidates global job execution se, OptimisticLockError, Raised when a DB update fails because another instance modified the row.      Ca, PollLockPort, Acquire / release exclusive poll-loop ownership for a job., ProcessIdValidatorPort, ProvidersPort (+25 more)

### Community 9 - "Status Derivation Orchestrator"
Cohesion: 0.1
Nodes (26): OGCProcessException, Base exception for OGC Process API errors., Composable Job Execution Pipeline, Context object containing all data needed for status derivation.          Encaps, StatusDerivationContext, Construct the step pipeline with all dependencies wired., Orchestrator for status derivation strategies.  Manages the chain of status deri, Orchestrates status derivation using a chain of strategies.          Evaluates p (+18 more)

### Community 10 - "K8s ConfigMap Backend"
Cohesion: 0.1
Nodes (23): EntityConfigBackendPort, Exception, Raised when the store adapter fails to persist a result.      Covers I/O errors,, ResultStorageError, ConfigConflict, The service entity changed since it was read — the write was rejected.      Rais, _api_status(), _backoff() (+15 more)

### Community 11 - "Job Observers & Polling"
Cohesion: 0.08
Nodes (21): Job State Observer Pattern, HttpClientPort, JobStateObserver, Observer protocols for job state transitions.  This module defines the Observer, Observer protocol for job state transitions.          Implementations can react, Called after job is created with initial accepted status.                  Args:, Called after job status changes.                  Args:             job: The job, Called after job reaches terminal state.                  Args:             job: (+13 more)

### Community 12 - "Process Manager"
Cohesion: 0.09
Nodes (13): ProcessManager, Leniently accept partially non-spec processes by synthesizing         reasonable, Drop malformed metadata entries (missing required keys) instead of failing., Return canonical process ID, avoiding double-prefixing.          If *configured_, Look up the remote server's process ID for a given UMP canonical ID.          Th, Return (remote_id, canonical_id) pairs for all non-excluded processes., Fetches processes for all providers concurrently using asyncio.gather.         A, Retrieve a single process by id. The id may include a provider prefix         (e (+5 more)

### Community 13 - "Execution Steps"
Cohesion: 0.1
Nodes (18): _enrich_status_info(), _ensure_results_link(), _ensure_self_link(), FinalizeJobStep, _halt(), _halt_with_accepted(), _is_inline_small(), _notify_observers_changed() (+10 more)

### Community 14 - "Remote Provider Auth"
Cohesion: 0.1
Nodes (19): RemoteAuthAdapter — converts provider AuthConfig to HTTP headers.  This adapter, Stateless adapter — safe to share as a singleton.      Supported auth types:, RemoteAuthAdapter, OAuth2 Client Credentials (future), Outbound Call Sites Needing Auth Headers, Outbound Remote Provider Authentication, REF-F7 Remote Server Authentication, ProviderCredentials (+11 more)

### Community 15 - "Provider Config File"
Cohesion: 0.12
Nodes (8): _ConfigFileHandler, ProviderConfigFileAdapter, Atomar und thread-safe die Provider-Konfiguration aktualisieren, bei Fehlern Rol, providers.yaml List-based Format, FileSystemEventHandler, ProvidersConfig, Root configuration containing all providers, ProvidersPort

### Community 16 - "Process Cache"
Cohesion: 0.11
Nodes (9): Ambiguous Bare Process IDs, Remote Link Rewriting (UMP_REWRITE_REMOTE_LINKS), Per-provider and Per-process Caching, Process Handler Pipeline (ID enforcement, defaults, sanitize), REF-F2 /processes/{process_id}, ProcessCache, ProcessListCache, Remove cache entries whose keys are no longer in valid_keys. (+1 more)

### Community 17 - "Job Manager Polling Loop"
Cohesion: 0.13
Nodes (8): JobManager, Orchestrates job lifecycle: creation, forwarding, status derivation, polling, re, Notify all observers that a job was created., Handle error responses (>=400) from upstream provider.          Returns propagat, Check if inputs are small enough for inline storage., Continuously poll remote status until terminal or shutdown.          Acquires a, Check if polling should stop for a job.          Returns (should_stop, reason) t, Poll remote status and update job if status changed.          Returns True if te

### Community 18 - "Poll Lock & Scaling"
Cohesion: 0.12
Nodes (14): NoOpPollLock, NoOpPollLock — always grants the lock.  Used for single-instance deployments, th, PgAdvisoryPollLock, PgAdvisoryPollLock — PostgreSQL session-level advisory lock.  Maps a job UUID to, Convert a UUID string to a signed int64 advisory lock key., PostgreSQL advisory lock for exclusive poll-loop ownership.      One persistent, _uuid_to_lock_key(), Duplicate Polling Failure Mode (+6 more)

### Community 19 - "Result Storage Factory"
Cohesion: 0.17
Nodes (17): build_entity_config_backend(), build_result_storage_port(), ensure_ldproxy_bootstrapped(), ldproxy_required(), Composition for Feature V result storage.  This module answers exactly one quest, Return True if any configured process needs the ldproxy result store.      The s, Select and construct the entity-config backend named by settings.      Raises ``, Publication-confirmation budget appropriate for the selected backend. (+9 more)

### Community 20 - "OGC Process Models"
Cohesion: 0.32
Nodes (14): BaseModel, GET /processes/{process_id}, ProcessesPort, AdditionalParameter, Link, Config, DescriptionType, Metadata (+6 more)

### Community 21 - "Execute Request Models"
Cohesion: 0.13
Nodes (12): Minimal DDD / CQRS Scaffolding (optional), Large Input Data Strategies, Sync Execution (deferred), REF-F6 Job Execution Pipeline, _coerce_inline(), ExecuteRequest, from_raw(), InlineOrRef (+4 more)

### Community 22 - "Aiohttp Client Adapter"
Cohesion: 0.17
Nodes (7): AioHttpClientAdapter, Fetch URL, returning (body_bytes, content_type).          Never attempts JSON pa, Build a ClientTimeout for a request.          When a caller supplies an explicit, Async context manager entry, Async context manager exit, Fetch JSON from URL with OGC API-specific error handling.          Translates HT, HttpClientPort

### Community 23 - "Logging Configuration"
Cohesion: 0.17
Nodes (10): _coerce_level(), configure_logging(), _CorrelationIdFilter, generate_uvicorn_log_config(), _MaxLevelFilter, _MinLevelFilter, Central logging configuration utilities.  Adds a single composition-root driven, Return a uvicorn-compatible log_config dict that preserves UMP logger level. (+2 more)

### Community 24 - "Process Description Proxy"
Cohesion: 0.16
Nodes (12): PassThroughProcessDescriptionProxy, PolicyBasedProcessDescriptionProxy, Adapter: process description proxy implementations.  Two concrete adapters live, Return the ``outputTransmission`` list UMP should advertise for *policy*.      T, No-op proxy: returns the process description unchanged.      Used when no UMP Pr, Rewrites the process description to reflect UMP's policy commitments.      UMP i, Return the policy-adjusted process description.          A model_copy is used so, _rewrite_output_transmission() (+4 more)

### Community 25 - "Result Value Cache"
Cohesion: 0.15
Nodes (10): _Entry, InMemoryResultValueCache, In-memory adapter for ``ResultValueCachePort``.  Keeps a completed job's inline, Return the cached outputs for *job_id*, or ``None`` on any miss., Cache *values* for *job_id*, unless they exceed ``max_item_bytes``., Serialised byte size of *values*, or ``None`` if it cannot be measured., One cached job: its inline outputs and the monotonic deadline., Process-local, TTL-bounded LRU cache of inline ``value`` outputs.      Only popu (+2 more)

### Community 26 - "Job Domain & Observers"
Cohesion: 0.15
Nodes (8): JobRepositoryPort, Port abstraction for Job persistence and status history., Eagerly stores a job's result the moment the job completes successfully.      Th, A job that just started has no result to store., Intermediate statuses carry no result; we wait for completion., ResultStorageObserver, Job, Domain Job model (internal) distinct from user-facing OGC statusInfo.      Notes

### Community 27 - "Filesystem Entity Backend"
Cohesion: 0.22
Nodes (5): FilesystemEntityConfigBackend, Filesystem implementation of ``EntityConfigBackendPort`` (dev / Docker).  Writes, Persist entity YAML as files under ``entities_path``., Raise ConfigConflict if the on-disk version differs from expected.          ``ex, _version_of()

### Community 28 - "Domain Exceptions"
Cohesion: 0.23
Nodes (8): JobExecutionError, JobTimeoutError, Base exception for job execution failures.          Attributes:         message:, Raised when job exceeds configured timeout waiting for remote completion., Raised when remote provider returns error response or fails to respond., Raised when results cannot be fetched for a successful job.          This indica, RemoteProviderError, ResultsFetchError

### Community 29 - "Job Status Finalization"
Cohesion: 0.19
Nodes (6): Process a status update from remote provider.          Normalizes IDs, enriches, Check if status info needs enrichment (status changed or fields missing)., Ensure a relative results link is present in statusInfo.links when successful., Guarantee a local self link (remove remote self/results with foreign job id)., Notify all observers that job status changed., Notify all observers that a job reached terminal state.

### Community 30 - "Ldproxy Service Registry"
Cohesion: 0.19
Nodes (7): ServiceRegistry: race-safe read-modify-write of the shared service entity.  Ever, Make sure the shared service entity exists, creating it if absent.          This, Run one read -> mutate -> write cycle, retrying on ``ConfigConflict``., Registers and deregisters ldproxy collections in the shared service entity., Add one collection to the shared service entity.          Idempotent: registerin, Remove one collection from the shared service entity.          Idempotent: remov, ServiceRegistry

### Community 31 - "Landing Page Template"
Cohesion: 0.15
Nodes (13): API Endpoints Table (links), Contact Footer, CSS Stylesheet Link, API Description Section, Landing Page HTML Template (template.html), Powered-By Attribution (safe HTML), Supported API Versions Section, Title and Version Header (+5 more)

### Community 32 - "Job Results Retrieval"
Cohesion: 0.2
Nodes (7): _parse_json_document(), Fetch remote results for a terminal successful job.          We never persist re, Best-effort lookup of the process's configured transmission-mode-policy., Build the OGC ``document`` response for a job with stored outputs.          Stor, Single-attempt results fetch; wraps transient OGC errors for the retry adapter., Parse a remote results body as an OGC document response, if it is one.      Retu, OGCProcessException

### Community 34 - "Job Cleanup Service"
Cohesion: 0.24
Nodes (5): JobCleanupService, Service: JobCleanupService (V-9).  Periodically removes finished jobs whose rete, Finds and removes expired jobs, best-effort on the storage side., Delete every currently-expired job. Returns how many were removed.          Safe, Remove one job's stored result (best-effort) and its record.          Storage cl

### Community 35 - "Remote Forwarding & Retry"
Cohesion: 0.2
Nodes (5): Check if exception represents a transient error worth retrying.          Transie, Classify an OGC error, wrapping transient ones so the retry adapter retries them, Handle exceptions during forward request, marking job as failed., Single-attempt forward POST; wraps transient OGC errors for the retry adapter., Forward execution request to remote provider with retry logic.          Uses Ten

### Community 36 - "Periodic Task Runner"
Cohesion: 0.22
Nodes (5): PeriodicTaskRunner, PeriodicTaskRunner: a small, generic asyncio background-loop adapter.  Runs an a, Runs ``task()`` every ``interval_seconds`` until ``stop()`` is awaited., Start the background loop. Safe to call at most once per instance., Signal the loop to stop and wait for it to finish the current cycle.

### Community 37 - "FastAPI App Factory"
Cohesion: 0.24
Nodes (9): build_problem(), create_app(), Structural protocol for any start/stop background loop (e.g. cleanup).      Deli, Create the FastAPI app.      Adapters and concrete infrastructure (logging, repo, Returns a 400 problem response if process_id fails validation, else None., Re-schedule poll loops for jobs that survived a previous instance crash.      Ca, _recover_orphaned_polls(), render_problem() (+1 more)

### Community 38 - "Execution Mode Enums"
Cohesion: 0.36
Nodes (9): UMP as Authoritative Process Description, OGC Execution Response Table, Enum, ResponseMode, TransmissionMode, ProcessJobControlOptions, ProcessOutputTransmission, ResponseType (+1 more)

### Community 39 - "Polling & Results Ports"
Cohesion: 0.32
Nodes (3): ABC, PollingService, JobResultsPort

### Community 40 - "Reference Storage Policy"
Cohesion: 0.25
Nodes (6): V-11: does this job's policy (+ client request) require a stored         referen, _client_requested_reference(), Return True if storage is required for this completed job.          This is the, Pure policy check: does this job require a stored reference?      Extracted from, Return True if any output in the execute request asked for reference.      Looks, should_store_reference()

### Community 42 - "Process ID Resolution"
Cohesion: 0.29
Nodes (5): Validate process_id format and resolve provider prefix + remote process id., Return the verbatim configured remote ID for a canonical UMP process ID., Return the canonical process ID for *configured_id*, avoiding double-prefixing., _to_canonical_via_validator(), ValidateAndResolveStep

### Community 43 - "Process ID Validator"
Cohesion: 0.29
Nodes (3): ProcessIdValidator, Process ID validator using a configurable separator between provider and process, ProcessIdValidatorPort

### Community 44 - "Entity Config Backend Port"
Cohesion: 0.25
Nodes (3): EntityConfigBackendPort, Where ldproxy entity YAML files physically live.  ldproxy is driven by two kinds, Persist ldproxy entity YAML, abstracting filesystem vs. Kubernetes.      Methods

### Community 45 - "Null Storage & Confirm Budget"
Cohesion: 0.25
Nodes (5): ConfirmBudget, How long to keep re-checking that a stored collection went live., NullResultStorage, No-op result store for development and test environments.      Every method is a, NamedTuple

### Community 47 - "Execution Entry Point"
Cohesion: 0.29
Nodes (3): process(), Pipeline-based execution entrypoint. - Renamed from "create_and_forward", Fetch remote results for terminal successful job; return True if fetched.

### Community 48 - "Status History Observer"
Cohesion: 0.33
Nodes (3): Record initial status in history., Record status change in history., Terminal status already recorded in on_status_changed.

### Community 50 - "Status Info Enrichment"
Cohesion: 0.33
Nodes (3): Enrich status info with contextual fields based on current status.          Fill, Normalize and enrich valid statusInfo with local context.          Modifies stat, Derive statusInfo from provider response using Strategy pattern.          Delega

### Community 51 - "Result Publication"
Cohesion: 0.33
Nodes (3): Store the result if policy and client intent call for it.          V-11: when st, Persist the deferred terminal transition.          Re-reads the freshest job sna, Look up the process config that carries the transmission-mode policy.          `

### Community 52 - "CLI Entry Points"
Cohesion: 0.4
Nodes (3): migrate(), UMP command-line entry points.  Registered as Poetry scripts in pyproject.toml:, Run ``alembic upgrade head`` using credentials from environment variables.

### Community 54 - "Persist Accepted Step"
Cohesion: 0.4
Nodes (3): _notify_observers_created(), PersistAcceptedStep, Persist the new job and store its initial 'accepted' statusInfo snapshot.      S

### Community 56 - "Link Rewriting"
Cohesion: 0.4
Nodes (3): Rewrite remote links to local links if enabled in settings.         This handler, Replace remote links with local links when configured to do so.     - Keep links, rewrite_links_to_local()

### Community 57 - "ASGI Bootstrap"
Cohesion: 0.4
Nodes (3): ASGI entry point for production deployments.  Exposes the FastAPI application at, Fail fast if any configured process needs the ldproxy result store but     the r, _validate_resultstore_settings()

## Knowledge Gaps
- **397 isolated node(s):** `Start uvicorn pointing at the ASGI module (single composition root).`, `ASGI entry point for production deployments.  Exposes the FastAPI application at`, `UMP command-line entry points.  Registered as Poetry scripts in pyproject.toml:`, `Run ``alembic upgrade head`` using credentials from environment variables.`, `Configuration models for core domain components.  This module provides Pydantic-` (+392 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **58 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `JobManager` connect `Job Manager Polling Loop` to `Status Derivation Strategies`, `Refactoring Reference Docs`, `Execution Pipeline Context`, `Status Derivation Orchestrator`, `Job Observers & Polling`, `Process Manager`, `Remote Provider Auth`, `Poll Lock & Scaling`, `OGC Process Models`, `Execute Request Models`, `Job Domain & Observers`, `Job Status Finalization`, `Job Results Retrieval`, `Remote Forwarding & Retry`, `Reference Storage Policy`, `Execution Entry Point`, `Status Info Enrichment`, `Background Runner`, `Community 65`?**
  _High betweenness centrality (0.166) - this node is a cross-community bridge._
- **Why does `ProcessManager` connect `Process Manager` to `Refactoring Reference Docs`, `Execution Mode Enums`, `Execution Pipeline Context`, `Status Derivation Orchestrator`, `Job Observers & Polling`, `Remote Provider Auth`, `Process Cache`, `Job Manager Polling Loop`, `OGC Process Models`, `Background Runner`, `Process Description Proxy`, `Link Rewriting`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Why does `ResultStorageError` connect `K8s ConfigMap Backend` to `Job Cleanup Service`, `GeoPackage Writer`, `Result Storage Coordinator`, `Execution Pipeline Context`, `Job Observers & Polling`, `Job Domain & Observers`, `Ldproxy Service Registry`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Are the 24 inferred relationships involving `JobManager` (e.g. with `_job_manager_factory()` and `JobManagerConfig`) actually correct?**
  _`JobManager` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 51 inferred relationships involving `JobStatusInfo` (e.g. with `PipelineStep` and `ExecutionResult`) actually correct?**
  _`JobStatusInfo` has 51 INFERRED edges - model-reasoned connections that need verification._
- **Are the 46 inferred relationships involving `Link` (e.g. with `PipelineStep` and `ExecutionResult`) actually correct?**
  _`Link` has 46 INFERRED edges - model-reasoned connections that need verification._
- **Are the 35 inferred relationships involving `Job` (e.g. with `PipelineStep` and `ExecutionResult`) actually correct?**
  _`Job` has 35 INFERRED edges - model-reasoned connections that need verification._