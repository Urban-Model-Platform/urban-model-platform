_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Ideas (not ordered, no exact location within the current implementation plan)

**Idea raised (not yet planned):** a new admin-only endpoint for
operational actions like this, whose first feature would be a manual retry —
re-run the fetch-and-store path for a specific job that is currently in the
`result-storage-failed` state, without re-executing the underlying remote
model job.

**Second idea raised (not yet planned):** the retry mechanism itself is the
wrong shape for the OOM-crash-loop failure mode it can't recover from — a
**circuit breaker** per remote/provider (or per job) would fit better than
tuning the existing retry budget. Blind exponential backoff assumes the
failure is transient and self-resolving; it does not help against a remote
that is deterministically failing — retrying just repeats the same failure
and may keep re-triggering the OOM on the remote. Tripping a breaker after
repeated failures would stop hammering an unhealthy remote, with the
manual-retry admin endpoint above serving as the deliberate reset once the
operator believes the underlying issue is addressed.

Shape: a pure technical/cross-cutting concern, reusable beyond just this one
call site — same reasoning that already justifies `RetryPort` /
`TenacityRetryAdapter` (`src/ump/core/interfaces/retry.py`,
`src/ump/adapters/retry_tenacity.py`) as a hexagonal port+adapter rather than
inline logic. Points to a `CircuitBreakerPort` Protocol in
`core/interfaces/` (e.g. `allow(key) -> bool`, `record_success(key)`,
`record_failure(key)`), with a concrete adapter under `adapters/`
(in-memory state to start), instantiated in `main.py` and injected wherever
needed (e.g. into `ResultStorageCoordinator` alongside the existing
`RetryPort`), matching the project's explicit-DI convention.