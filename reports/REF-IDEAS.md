_Last_updated: 2026-08-14

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Ideas (not ordered, no exact location within the current implementation plan)

## Usage accounting / metering

**Scope decision: UMP does metering + attribution, not billing.**

Why it belongs in UMP: identity terminates at UMP. Models see requests from UMP's
service account and cannot attribute work to a user/tenant. Attribution is
structurally unavailable anywhere else. `Job` already carries `user_id`,
`process_id`, `provider`, timestamps and `inputs_size`.

Why billing does not belong in UMP:

- UMP measures wall-clock of a job it does not execute — a poor proxy for compute cost.
- Rating/invoicing/tax/disputes is a separate domain with its own lifecycle.
- Observer dispatch swallows exceptions by design → a naive metering observer loses events silently.
- Models are reachable via cluster DNS; without NetworkPolicy the numbers are advisory only.
- Anonymous execution is first-class (`user_id` optional, `UMP_PUBLIC_PROCESSES`).
- For true-remote providers the operator's invoice is authoritative; UMP data is verification/chargeback.

### Shape

New port + observer, wired in the existing observer list in `asgi.py` — no `JobManager` change:

```
core/interfaces/usage_metering.py   UsageMeteringPort.record(event)
core/models/usage_event.py           UsageEvent (frozen, facts only — no prices)
core/managers/observers.py           UsageMeteringObserver(JobStateObserver)
adapters/usage_metering/             Null | SqlOutbox | Otel | Webhook
```

`UsageEvent`: `event_id` (idempotency), `job_id`, `user_id`, `tenant_id`, `process_id`,
`provider`, `provider_kind` (local|remote), submitted/started/finished, `terminal_status`,
`wall_seconds`, `input_bytes`, `output_bytes`, open `attributes` for provider cost hints.

Durability: V1 derive reports from the existing `jobs` table (zero new failure modes).
V2 transactional outbox — write the event in the same transaction as the terminal job
update. Never fire an HTTP call to a billing service from the observer.

### Real cost in k8s

UMP supplies the attribution key, the cluster supplies the cost. Propagate
`X-UMP-Job-Id` on the forwarded execute request (extends existing correlation-ID
middleware). Models that run one Pod per job label it `ump.io/job-id` → exact
per-job cost via OpenCost. Long-lived Deployments only yield per-namespace cost →
allocate proportionally by UMP wall-clock share.

### Notes

- Higher value than billing: `QuotaPort.check(user_id, process_id)` at execute admission,
  called from `AuthorizationService`. Prevents runaway cost instead of reporting it.
- Prerequisite: NetworkPolicy limiting model-namespace ingress to UMP.
- Config follows the per-process pattern in `providers.yaml`: `billable`, `cost_unit`, `quota_group`.
- Retention policy needed — usage records tie identity to activity over time.
- Out of scope: rating, invoices, currency, tax, credits, payment.

