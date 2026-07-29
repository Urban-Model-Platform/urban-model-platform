_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature VI: Job Execution Pipeline ✅ (implemented)

**Goal**: replace the monolithic `create_and_forward` method (~200 lines) with a composable `JobExecutionPipeline` of discrete, independently testable `PipelineStep` objects. Each step receives and mutates a shared `JobExecutionContext`; any step can abort by setting `context.should_halt = True`.

**Status**: pipeline is implemented and active. 
1. `ProcessManager.execute_process` calls `create_and_forward_ii` (pipeline entrypoint). The old `create_and_forward` remains as dead code and can be deleted in a cleanup pass. 
2. renamed `create_and_forward_ii` to create_and_forward and delete old `create_and_forward` dead code
3. renamed `create_and_forward` to `run_execution_pipeline`

**`ShapeClientResponseStep` currently implements the async row only** (201 + accepted statusInfo). The full OGC sync response table is deferred until sync execution is added (see deferred items below).

## Implemented steps (`src/ump/core/managers/steps/execution_steps.py`)

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

## Deferred extension steps (not yet implemented) 🔲

- `ResolveOutputFormatsStep` — per-output `(media_type, is_binary)` from execute request + process description
- `ApplyTransmissionModePolicyStep` — rewrite `transmissionMode` per provider config
- `ApplyResponseModePolicyStep` — override `response` field sent to remote

## OGC execution response table

`ShapeClientResponseStep` implements this:

| execution_mode | response_mode | transmissionMode | # outputs | HTTP code | Content-Type | Body |
|---|---|---|---|---|---|---|
| async | any | any | any | 201 | application/json | statusInfo |
| sync | raw | value | 1 | 200 | per output definition | raw output bytes |
| sync | raw | value | >1 | 200 | multipart/related | one part per output |
| sync | raw | reference | 1 | 204 | — | empty + Link headers |
| sync | raw | mixed | >1 | 200 | multipart/related | one part per output |
| sync | document | value | any | 200 | application/json | results document |

The decision belongs in core; the adapter only serialises the dict to HTTP (e.g., builds the multipart MIME body for `multipart/related` rows).

## Adapter/core boundary for sync

The adapter currently passes `Prefer` as `headers["Prefer"]` but does NOT pass `exec_req.response` (raw vs document) or `exec_req.outputs` (per-output `transmissionMode`) as first-class parameters to the core's decision logic — they are buried inside `provider_payload` and forwarded to the remote without being used by UMP itself. To support `ShapeClientResponseStep`, the pipeline entrypoint needs:

```python
# What the adapter must extract and pass as first-class context (not just in provider_payload):
exec_req.response           # ResponseMode.raw | ResponseMode.document
exec_req.outputs            # Dict[output_id, OutputSpec] — contains transmissionMode per output
# Derived from Prefer header:
execution_mode              # "sync" | "async"
```

These become first-class fields on `JobExecutionContext` (see below); `ShapeClientResponseStep` reads them to select the correct row in the OGC table.

## `JobExecutionContext` fields (updated — additions marked with ★)

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

## Migration status

1. ✅ Implemented steps one at a time.
2. ✅ Wired into `_build_execution_pipeline()`.
3. ✅ Switched `ProcessManager.execute_process` to call `create_and_forward_ii`.
4. ✅ Delete `create_and_forward` (now dead code) and rename `_ii` → `create_and_forward`. -> renamed to `run_execution_pipeline`

## Minimal DDD scaffolding (optional, for future evolution toward CQRS)

- `src/ump/core/commands.py` — `CreateJobCommand`, `ForwardExecutionCommand`, etc.
- `src/ump/core/events.py` — `JobCreated`, `JobForwarded`, `JobStatusUpdated`, `JobFailed`.
- `src/ump/core/aggregates/job_aggregate.py` — pure `handle_command`/`apply_event` with no IO.
- `append_event(event, expected_version)` on `JobRepositoryPort` (already exists as no-op).
These are optional and deferred unless complexity grows sufficiently to justify them.
