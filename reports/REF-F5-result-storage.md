_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# 🔲 Feature V: Result storage (ldproxy first)

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

## ⚠️ Dependency: a minimal slice of Feature VIII is a prerequisite

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

## Storage trigger: eager at job completion (recommended)

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

## Core additions

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

## Adapter: `LdproxyResultStorage`

```
src/ump/adapters/result_storage/ldproxy_adapter.py         — LdproxyResultStorage(ResultStoragePort)
src/ump/adapters/result_storage/gpkg_writer.py             — GeoJSON FeatureCollection -> GeoPackage table
src/ump/adapters/result_storage/ldproxy_entities.py        — build provider YAML + one collection block
src/ump/adapters/result_storage/service_registry.py        — read-modify-write the shared service entity (locked)
src/ump/adapters/result_storage/atomic_fs.py               — atomic write (temp file + os.replace)
src/ump/adapters/result_storage/entity_config_backend.py   — EntityConfigBackendPort ABC + ConfigConflict (factory deferred to V-8, see decision note)
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

### ldproxy 4.x deployment (verified on 4.6.1)

The store layout above is **unchanged** by the 3.x→4.x upgrade: ldproxy 4.6.1
reads the same `entityStorageVersion: 2` entities UMP writes, with **no schema
migration**. Only the *deployment* differs, verified empirically end-to-end:

- **Store mount = the data-dir itself.** In 4.x `--data-dir` *is* the store
  root (`FS . [ALL]`), so the shared volume mounts at `/ldproxy/data`, **not**
  `/ldproxy/data/store` as in 3.x. UMP still writes `entities/` and
  `resources/` at `{root}`.
- **API path dropped the `/rest/services` prefix.** Collections are served at
  `/{service-id}/collections/{collection_id}/items` (so
  `UMP_RESULTSTORE_LDPROXY_BASE_URL` ends in `…/ump-results`, no `/rest/services`).
- **Hot-reload actually works.** With `store.watch: true` (see
  `ldproxy-cfg.yml`) 4.x reloads the affected service/provider on change
  ("Reloading configuration for service … reloaded successfully") — so a new
  per-job collection becomes queryable within seconds **without a restart**.
  This is the concrete win over 3.6.4, which detected changes but never applied
  them (every job would otherwise have needed a restart).
- **Default provider still required.** 4.x, like 3.x, refuses to start an
  `OGC_API` service that cannot resolve a default feature provider whose id
  equals the service id (verified: removing it yields "No feature provider
  found"). UMP therefore still writes a seed GPKG + default provider on startup
  (`ensure_default_provider`), and writes it **before** the service entity so
  ldproxy's cold-start watcher sees the provider first (ordering is asserted by
  a unit test).
- **Fresh-volume bootstrap.** A brand-new volume is root-owned and empty; UMP
  runs as uid 1000 and ldproxy's watcher only tracks store subdirs that exist
  at start. The ldproxy service's entrypoint pre-creates the
  `entities/instances/{providers,services}` + `resources/features` skeleton and
  `chown`s it to 1000:2000, so first-run needs no manual `chown`/restart. The
  idle landing page may briefly 404 (no collections yet); the first job's
  collection self-heals it (service reload once the default provider is
  `AVAILABLE`).

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
`database: {job_uuid}.gpkg`, one `types.{output_id}` entry per output with a
`fid` integer primary key (the GeoPackage feature id) as the `ID` property, a
`geom` `PRIMARY_GEOMETRY`, and one typed property per GeoJSON attribute.

> **Column names (`fid`/`geom`) are load-bearing.** pyogrio/GDAL write the
> GeoPackage primary key as `fid` and the geometry column as `geom`. The
> provider entity's `sourcePath`s must match those exact names or ldproxy fails
> at query time with a missing-column SQL error. (An earlier draft used Esri-
> style `OBJECTID`/`Shape`; that was wrong and is fixed — verified end-to-end.)

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

## Defensive concerns (explicit)

1. **Write ordering & atomicity** — ldproxy watches the store and must never
   read a half-written file, or a service collection that references a
   not-yet-written provider/gpkg. Strict order, each individual file written via
   atomic `os.replace` from a temp file on the *same* filesystem:
   1. write `.gpkg`  2. write provider `.yml`  3. register collection in
   `ump-results.yml` (read-modify-write, see concern 9).
   On any failure, clean up temp files and raise `ResultStorageError`.
2. **Shared-service-file concurrency (the topology's main hazard)** — with a
   single `ump-results.yml`, two jobs completing at once do a read-modify-write
   of the *same* file. A lost update would drop a collection.

   > **DECISION (2026-08-04, implemented in V-5a) — changed from the original
   > advisory-lock plan.** We do **not** use `PollLockPort` /
   > `PgAdvisoryPollLock`. Instead the `EntityConfigBackendPort` exposes
   > **optimistic concurrency**: `read_service_entity` returns
   > `(yaml_text, version)` and `write_service_entity(..., expected_version)`
   > raises `ConfigConflict` if the stored version moved on. Each backend uses
   > its **native** version primitive — Kubernetes `resourceVersion` (concern
   > 10), the filesystem backend a **SHA-256 hash of the file content**
   > (changed from `st_mtime_ns` during V-5b hardening: mtime resolution can be
   > too coarse to distinguish rapid successive writes, causing a stale token
   > to pass; a content hash is deterministic and semantically exact — identical
   > content does not conflict). Rationale: the
   > dangerous multi-pod RMW only ever happens through the **k8s API** (entity
   > YAML lives in ConfigMaps in production, concern 8); the filesystem backend
   > is single-instance only, so an in-process `asyncio.Lock` in the service
   > registry (V-5c) plus the version guard is sufficient there. This removes
   > the Postgres advisory-lock dependency from the storage path entirely and
   > avoids holding any lock across slow File-share writes.

   The `service_registry` (V-5c) still owns the read → mutate collections map →
   atomic-write sequence; on `ConfigConflict` it re-reads and retries.
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

## Security (result access)

- Today: **collection id = job UUID = unguessable** is the only protection.
  Anyone with the link can read the collection. Acceptable interim per user.
- ldproxy is OIDC-secured at the *instance* level (same realm as UMP). That
  gates "is a valid user", not "is this the user who created the job".
- **Per-user result isolation is a future enhancement**: ldproxy PDP policies
  keyed on `ldproxy:collection:id` + a per-job permission claim. Requires UMP to
  provision a policy/permission per job. Out of scope for the first cut —
  documented as a known gap.

## Configuration additions

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

## New dependency

GeoPackage writing needs GDAL bindings — **`geopandas` + `pyogrio`** (pragmatic,
well-maintained). Adds a non-trivial native dependency to the UMP image;
flagged as a deliberate decision. Alternative (raw `fiona`) noted but not
preferred.

Kubernetes entity config backend needs the **`kubernetes`** Python client
(`kubernetes` on PyPI). Added as an optional/conditional dependency; only
required when `UMP_RESULTSTORE_CONFIG_BACKEND=k8s`.

## Implementation steps (sequenced, defensive)

| # | Step | Depends on |
|---|---|---|
| ~~V-0a~~ | ~~Persist `job.response_mode` + `job.outputs_spec` (+ Alembic migration)~~ | ✅ done |
| ~~V-0b~~ | ~~`ProcessConfig.transmission_mode_policy` + startup validation rules~~ | ✅ done |
| ~~V-1~~ | ~~`ResultStoragePort` + dataclasses + exceptions (core)~~ | ✅ done |
| ~~V-2~~ | ~~`ResultStorageCoordinator` (core, decide-fetch-store-linkinject)~~ | ✅ done |
| ~~V-3~~ | ~~`atomic_fs` + `gpkg_writer` (GeoJSON→gpkg) with unit tests over temp dir~~ ✅ done. **V-6 groundwork added:** (1) `validate_output_id` — a shared identifier safeguard (letters/digits/underscore, must start with a letter) since `output_id` becomes a GeoPackage layer name, an ldproxy `types` key, and half of the collection id; called from `write_layers_to_gpkg` and from `ldproxy_entities.build_provider_entity`/`collection_id_for`; (2) `write_layers_to_gpkg` — additive multi-layer counterpart to `write_to_gpkg`, writes every output of one job into a single GeoPackage (one layer per output) in one atomic operation, validating and parsing all layers *before* writing any — matches the "one provider, one `types` entry per output" model from V-4 for jobs with multiple storable outputs. | V-4 |
| ~~V-4~~ | ~~`ldproxy_entities` (provider YAML + one collection block from a FeatureCollection)~~ ✅ done. **V-6 groundwork:** `build_provider_entity_multi(job_uuid, {output_id: schema})` builds one provider with one `types` entry per output; the original single-output `build_provider_entity` now delegates to it (one source of truth, V-4 signature/tests unchanged). | ✅ done |
| ~~V-5a~~ | ~~`EntityConfigBackendPort` + `FilesystemEntityConfigBackend`~~ ✅ done. **Deviations from plan:** (1) port uses **optimistic versioning** (`read`→`(text, version)`, `write(..., expected_version)`, `ConfigConflict`) instead of an advisory lock — see concern 2 decision note; (2) **no factory** in this step (deferred to V-8) to avoid dead `NotImplementedError` code before the k8s backend exists; (3) `delete_provider_entity` already on the port for V-9. | V-4 |
| ~~V-5b~~ | ~~`K8sConfigMapEntityConfigBackend` (same port; maps k8s 409 → `ConfigConflict`; `resourceVersion` is the version token)~~ ✅ done. **Design notes:** (1) `kubernetes` is an **optional** dependency behind the `k8s` extra, imported lazily in `_build_default_api` (`load_incluster_config`) so dev/Docker never pulls it; (2) `core_v1_api` is **injectable** for tests; (3) ConfigMap bodies are plain **dicts** and errors are dispatched on the exception's `status` attribute — both keep the module import-free of `kubernetes`, so unit tests run without it installed; (4) provider write uses **`replace`→(404)→`create`** (idempotent re-store is one API call); (5) error mapping: 409→`ConfigConflict`, 404 read→`None`, 404 delete→no-op, other API errors→`ResultStorageError`, non-API errors re-raised untouched. | V-4 |
| ~~V-5c~~ | ~~`service_registry` (in-process `asyncio.Lock` + read → mutate → `write_service_entity`, retrying on `ConfigConflict`; bootstraps skeleton when `read` returns `None`)~~ ✅ done. **Design notes:** (1) `register_collection(collection_id, job_uuid, output_id)` takes the collection id **explicit** from the caller (V-6 derives it via `collection_id_for` once, reuses it for the V-10 back-link — `ServiceRegistry` never derives ids itself); (2) both mutations (`register`/`deregister`) are idempotent (`dict.update` / `dict.pop(..., None)`) so a retried V-6 step or unconditional V-9 cleanup never breaks; (3) backend calls run through `asyncio.to_thread` inside the lock — the lock is held across the `await`, which is correct here since the whole RMW is the critical section; (4) proven against a **real** `FilesystemEntityConfigBackend` with genuine concurrent `asyncio.gather` tasks, not just an isolated mock, for the lost-update hazard. | V-5a |
| ~~V-6~~ | ~~`LdproxyResultStorage` adapter wiring 3-5 together, atomic ordering~~ ✅ done. **Design notes:** (1) crash-safe write order gpkg → provider → collections, each stage referencing only the previous, so a crash never leaves a collection pointing at nothing; (2) `store` is **transactional** — a failure after stage 1 rolls back (deregister collections → delete provider entity → delete gpkg) so `exists` stays an honest "fully stored" signal and no orphan survives for a retry; (3) `exists` tests the gpkg (always on the filesystem for both backends — no k8s call); (4) blocking calls (`write_layers_to_gpkg`, backend writes) run via `asyncio.to_thread`; (5) `UnsupportedResultError`/`ResultStorageError` propagate unchanged for the coordinator to apply policy; (6) `delete` is the idempotent minimal form — **collection deregistration + cleanup wiring is V-9** (documented in the docstring). | V-3, V-4, V-5c |
| ~~V-7~~ | ~~`ResultStorageObserver` (eager trigger on `on_job_completed`)~~ ✅ done. **Design notes:** (1) deliberately thin — it gates on `status == successful`, resolves `ProcessConfig`, asks `coordinator.should_store`, then delegates; no storage concepts leak into it; (2) non-successful jobs short-circuit *before* the provider lookup, so failed jobs cost nothing; (3) an unresolvable `ProcessConfig` (provider removed from `providers.yaml` mid-run) and a raising `ProvidersPort` are both logged and skipped — never raised into the notify loop; (4) `JobRepositoryPort` is constructor-injected because `coordinate()` needs it but `on_job_completed` does not supply it; (5) **`emulate-ref-only` failure handling:** `coordinator.coordinate` re-raises `ResultStorageError`, but `JobManager._notify_job_completed` swallows observer exceptions — so the observer catches it and records the reason on `job.diagnostic` (best-effort persist), leaving the job **successful** since the computation itself succeeded. This is the hand-off to V-10. | V-2 |
| ~~V-8~~ | ~~Compose in `asgi.py`; inject port + coordinator + observer. **Backend factory (`filesystem`\|`k8s`) lives here** (moved out of V-5a) so it is only built once both backends exist — matches explicit-DI rule.~~ ✅ done. **Design notes:** (1) the wiring logic itself lives in a new, import-side-effect-free module `ump/composition/result_storage.py` — `ldproxy_required`, `build_entity_config_backend`, `build_result_storage_port`, `ensure_ldproxy_bootstrapped` — so it is unit-testable without touching `ump.asgi` (which starts file watchers/DB connections at import time); (2) `_validate_resultstore_settings` in `asgi.py` now calls the shared `ldproxy_required` instead of re-deriving the same check; (3) when no process configures `result-storage: ldproxy`, `build_result_storage_port` returns `(NullResultStorage(), None)` — no backend, no `ServiceRegistry`, no GDAL/geopandas import, and `_job_manager_factory` skips attaching `ResultStorageObserver` entirely; `result-storage: geoserver` is treated the same as `remote` here (legacy path, out of Feature-V scope); (4) exactly **one** `ServiceRegistry` is built per worker process at module scope in `asgi.py` and shared by every job — constructing a second one anywhere would silently reintroduce the lost-update race the registry's lock exists to prevent; a composition test asserts the adapter and registry share the same backend instance; (5) **bootstrap failure semantics settled during design review:** configuration errors (missing settings, unknown backend name) still fail startup hard via `_validate_resultstore_settings`/`build_entity_config_backend`, unchanged; but the new `ServiceRegistry.ensure_bootstrapped()` — added without duplicating the skeleton-creation logic, it reuses the existing `_read_modify_write` path with a no-op mutation — is invoked from `_job_manager_factory` as a fire-and-forget `asyncio.create_task`, and `ensure_ldproxy_bootstrapped` swallows any exception from it as a logged warning. Rationale: a reachability failure (share not yet mounted, k8s API briefly down) is transient and must never block UMP's own startup or crash the process — the registry self-heals on the first successful `register_collection` regardless. | V-2, V-6, V-7 |
| ~~V-9~~ | ~~`delete()` + cleanup wiring (anonymous/expiry, deregister collection)~~ ✅ done. **Design notes (three open questions settled during design review):** (1) **manifest, not a port extension** — `store()` writes a small JSON sidecar (`{job_id}.manifest.json`, next to the `.gpkg`) listing the job's output ids; `delete()` reads it back to reconstruct which collection ids to deregister. Rejected alternative: adding a `read_provider_entity` method to `EntityConfigBackendPort` — that would force *both* backends to support reading and re-parsing their own YAML just for this one call. The manifest is always a plain file on the shared filesystem, exactly like the `.gpkg` itself, regardless of which `EntityConfigBackendPort` is configured; a missing/corrupt manifest is treated as "nothing to deregister" rather than an error, so `delete` stays safe to call on jobs that were never stored; (2) **generic scheduler, not ldproxy-specific** — the new `JobCleanupService` (core) and `PeriodicTaskRunner` (adapter) run for *every* deployment regardless of whether a result store is configured; when none is, `ResultStoragePort` is `NullResultStorage` and its `delete()` is a harmless no-op, so the service never needs to know or care which backend (if any) is active; (3) **two independent, separately configurable retention settings** — `UMP_JOB_DELETE_INTERVAL` (anonymous jobs, unchanged default 240 min) and the new `UMP_JOB_DELETE_INTERVAL_AUTHENTICATED` (default `None` = never auto-delete), because an anonymous job's creator has no account to revisit it later, while an authenticated user's job is part of their history and should not silently disappear unless an operator opts in. Further implementation notes: a new indexed `finished` column (Alembic migration `0004`, backfilled from the JSONB `status_info` for historical rows) lets `list_expired` filter/index directly instead of doing per-row JSONB extraction on every cleanup cycle; `JobRepositoryPort.list_expired` returns `[]` immediately if *both* cutoffs are `None` — deliberately, so a caller can never accidentally sweep the entire table; `LdproxyResultStorage.delete()` and `JobCleanupService._delete_one` are both **best-effort on the storage side** (a `ResultStorageError`, or a single failed collection deregistration, is logged and does not block the rest of cleanup or the job-record delete — an orphaned GeoPackage is a much smaller problem than a job that can never be cleaned up); `PeriodicTaskRunner` is a small, deliberately generic asyncio background-loop adapter (not named after job cleanup) so future periodic background tasks can reuse it; wired into `fastapi.py`'s `create_app(background_runners=[...])` via a new structural `BackgroundRunner` Protocol, started after job-manager setup and stopped in the lifespan `finally` block. | V-6 |
| ~~V-10~~ | ~~Ref links in statusInfo `links` + `GET /results` always returns an OGC `document` response (per-output `value` **or** `href`, never a 302 redirect). **Also: surface the `emulate-ref` silent downgrade**~~ ✅ done. **Design notes:** (1) **bugfix, not a new feature** — `_inject_reference_links` (renamed `_apply_stored_references`) previously wrote to `job.links`, a field the API never serves; it now writes to `status_info.links` (client-visible, one `rel="item"` link per stored output) and, additively, to a new structured `job.stored_outputs: dict[output_id, {collection_id, collection_url, items_url}]` — both idempotent across a repeated observer run, keyed by href / output_id so a retry never duplicates a link or an entry; (2) **no 302** (see decision log below) — `JobManager.get_results` gained `_build_stored_results_document`, invoked only when `job.stored_outputs` is non-empty: it fetches the remote document once (best-effort — a failure still returns the stored refs, matching the V-9 best-effort philosophy), then **overlays** the stored outputs as authoritative `href` entries over any inline value the remote returned for the same output_id. Jobs that never triggered storage (`pass-through`, `value-only`, or `emulate-ref` that never downgraded) are completely unaffected — they keep the original raw-proxy passthrough; (3) **downgrade transparency, Option A** — `_handle_storage_failure` no longer swallows the `emulate-ref` fallback; it returns a bool telling `coordinate` to call `_record_downgrade`, which sets the additive `JobStatusInfo.transmissionModeApplied = "value"` plus a human-readable `message` (OGC explicitly permits additional statusInfo properties, so this stays schema-conformant); `emulate-ref-only` is unchanged — it still raises, since there the value channel was never open; (4) `StoredReference` order from `ResultStoragePort.store()` is guaranteed to match its `payloads` input 1:1 (see `LdproxyResultStorage.store`), so `zip(payloads, references)` reliably recovers each reference's `output_id` without any string-parsing of `collection_id`; (5) no schema migration — both new fields (`Job.stored_outputs`, `JobStatusInfo.transmissionModeApplied`) live in existing JSONB columns. | V-2 |
| ✅ V-11 | **Defer the `successful` transition until the reference is live.** A job whose policy (+ client request) demands a stored reference must NOT report `successful` while its reference URL is not yet reachable. Store + verify run in a **background task**; the job stays `running` with a progressing `message` throughout, then flips to `successful` only after a generic liveness probe (GET an adapter-supplied `liveness_url`, success = HTTP status < 400) passes, or to **`failed`** (final) with a standardized error if publication cannot be confirmed within a deadline. See the design section **"V-11 — Defer `successful` until the reference is live"** below for the full rationale, sub-state model, probe, settings, and failure semantics. Removes the now-obsolete V-10 `_is_storage_pending` / `503 Results Finalizing` window. | V-7, V-10 |

Steps V-3, V-4 and V-5a/c are pure/file-only and fully unit-testable against a
temp directory with no ldproxy or Kubernetes running — that is where most of
the defensive test coverage goes (schema derivation, atomic ordering, concurrent
registry edits under the lock, malformed/empty/mixed-geometry FeatureCollections,
409-retry loop). V-5b is tested against a mocked `kubernetes` client.

### ✅ Resolved in V-10 — the `emulate-ref` downgrade is now visible

*Raised by the user during V-7; deferred to V-10 on purpose; closed 2026-08-06.*

**The gap.** Under `emulate-ref`, `should_store` returns True only when the
client **explicitly** asked for `transmissionMode: reference`. So the storage
failure path in `ResultStorageCoordinator._handle_storage_failure` is precisely
the case "the client asked for a reference and silently gets a value instead".
Today that leaves nothing but a log line — unlike the `emulate-ref-only` path,
which V-7 records on `job.diagnostic`. The client cannot detect that its
explicit request was not honoured.

**Why not fix it in V-7.** The behaviour itself is correct and stays: with
`emulate-ref` the value channel is legitimately open, so delivering the result
beats erroring out on a successfully computed job (that is the whole difference
to `emulate-ref-only`, where value is blocked and an error is the only honest
answer). Only the *transparency* is missing — and the client-facing
representation (`links`, `GET /results`) is built in V-10. Persisting a marker
in V-7 would populate a field nothing reads yet.

**To do in V-10.**
1. Record the downgrade on the job (same mechanism V-7 uses for
   `emulate-ref-only`, i.e. `job.diagnostic` or a dedicated field) so it is
   visible in the job status, not only in the logs. Requires
   `_handle_storage_failure` to report the fallback to its caller instead of
   swallowing it — e.g. return a result object or take a callback; keep the
   coordinator free of repository writes if possible.
2. Make it **machine-detectable** for the client via a **dedicated additive
   field in the statusInfo** (Option A, chosen by the user 2026-08-06):
   a `transmissionModeApplied` marker (e.g. `"value"` when the client asked for
   `reference` but got the inline value). OGC explicitly permits additive
   properties on the statusInfo schema, so this stays schema-conform. Preferred
   over overloading a link `rel`, which would hide the signal in the results
   response instead of surfacing it in the job status. A human-readable
   `status_info.message` accompanies it.
3. Test: client requests `reference` + storage fails under `emulate-ref` →
   value is delivered **and** the downgrade is discoverable via the API
   (`transmissionModeApplied` present in statusInfo).

**Decision (2026-08-06) — no 302 redirect.** An earlier plan sketch had
`GET /jobs/{id}/results` answer with `302 Found` + a `Location` header pointing
at the stored ldproxy collection. Dropped: a redirect has exactly one target,
so it only works for a single stored output and cannot represent a job that
mixes stored references with inline values. The OGC `document` response already
carries the reference as a per-output `href`, so the redirect solved a problem
that does not exist. `GET /jobs/{id}/results` therefore **always** returns a
`document` — one code path, any number of outputs, each an inline `value` or an
`href` link to its stored collection's `items` endpoint.
## V-11 — Defer `successful` until the reference is live

*Requested by the user 2026-08-19. Design discussed and confirmed the same day.*

### The problem

Today the poll loop marks a job `successful` the instant the remote reports
`successful`, and only *then* does `ResultStorageObserver.on_job_completed`
run the eager store (fetch → GeoPackage → provider → collection). That leaves a
window in which `GET /jobs/{id}` already says `successful` while the promised
`href` (the ldproxy `items` endpoint) does not yet resolve — a lie to the
client. V-10 patched the *symptom* on the results path (`_is_storage_pending`
answered `GET /results` with a `503 Results Finalizing` + `Retry-After`), but
the *status* itself was still prematurely `successful`.

**The fix inverts the ordering:** when a job's resolved policy (+ client request)
requires a stored reference, the job must remain `running` until the reference
is not only written but **verified live**, then transition to `successful`. If
the reference cannot be confirmed within a bounded deadline, the job transitions
to **`failed`** with a standardized error — the computation succeeded, but a
result the contract promised as a working reference could not be published, and
silently reporting `successful` with a dead link is worse than an honest failure.

### Scope / gating (user decision, 2026-08-19)

Gate purely on **`should_store`** — i.e. the resolved `transmission-mode-policy`
(`emulate-ref` with a client-requested `reference`, or `emulate-ref-only`).
This is exactly the condition under which a reference is produced, so it is the
right and only trigger; no separate "is a store configured?" check is needed,
because startup validation already forbids a ref-policy without a store (a
ref-policy therefore *guarantees* a store exists). Legacy `result-storage:
remote` / `geoserver` produce no eager reference and are entirely unaffected —
those jobs keep flipping straight to `successful` as before. Jobs under
`pass-through` / `value-only`, or `emulate-ref` where the client asked for
`value`, are likewise untouched.

### Sub-state model (OGC-conformant)

OGC API Processes has no intermediate "finalizing" status, so we do not invent
one on the wire. The job stays externally **`running`** and carries an
**internal** sub-state (e.g. `awaiting_publication`) that never leaves the core;
`progress` stays `< 100`. What the client sees change over time is the
human-readable **`message`**, updated as the background task advances so a
caller can tell *where in the pipeline* the job is (user requirement, point 1):

- `"fetching result from provider"`
- `"writing result to storage (GeoPackage)"`
- `"registering result collection"`
- `"verifying result publication"`

Only when the probe passes does the job transition to terminal `successful`
(with the V-10 `stored_outputs` + `links` already populated). This is strictly
additive to `JobStatusInfo` (message text only); no new external status value.

### Execution model — background task + internal sub-state (Option B)

The poll tick that observes the remote `successful` does **not** run the store
inline (that would block the poll worker for the full fetch + convert + probe
duration, up to the fetch timeout + publication deadline, starving other jobs it
polls). Instead it:

1. derives that storage is required (`should_store`),
2. sets the job to internal `awaiting_publication` (external `running`, initial
   message), and
3. hands off to a **background asyncio task** that owns the whole
   store-and-verify sequence and the final `successful`/`failed` transition.

This mirrors the existing polling design (`_schedule_poll` already runs polling
as background tasks) and keeps the poll loop's hot path clean. Hexagonally, the
core only orchestrates *state transitions* through ports
(`ResultStoragePort`, `HttpClientPort`); the actual store/probe I/O stays behind
those ports — no GeoPackage/ldproxy/k8s concept leaks into the core.

### Liveness probe — a generic status check of an adapter-supplied URL

After the store reports success, the background task verifies the reference is
actually reachable. The **key design rule (user decision, 2026-08-19):** the
core must **not** know any store-specific path (`/items`, `?limit=1`, …). Such
knowledge belongs to the adapter, exactly like "how to store" does. So:

- **The adapter supplies the probe URL.** `StoredReference` gains an optional
  `liveness_url: str | None`. The `LdproxyResultStorage` adapter — which already
  constructs the public `items_url` — additionally builds `liveness_url` from
  the **internal** base URL it is injected with
  (`UMP_RESULTSTORE_LDPROXY_INTERNAL_URL`), e.g.
  `{internal_base}/collections/{collection_id}/items?limit=1`. This is not new
  knowledge the core has to discover: it is the same information the adapter
  already assembles for the reference links, only pointed at the in-network
  address and handed back on the returned object.
- **Why the internal base lives on the adapter, not the core.** The adapter is
  constructed at the composition root with both base URLs injected (public → for
  `items_url`/client links, internal → for `liveness_url`). Explicit DI, matching
  the project rule. The core then knows *both* base URLs = none: it only ever
  receives a finished `StoredReference`.
- **The core's probe is fully generic:** `GET liveness_url`, success =
  **HTTP status < 400**. No body parsing, no `FeatureCollection` check, no
  `/items` assumption — path- and format-independent, so a future store whose
  references are not `/items`-shaped works unchanged.
- **Fallback.** When `liveness_url is None`, the core probes the reference URL
  it already has (`items_url` / `collection_url`). A simpler future adapter that
  has no dedicated health URL therefore still gets a generic reachability check
  without teaching the core anything about paths.

**Where does the URL come from when the reference was produced by the *remote*
server, not UMP? (user question, 2026-08-19).** It doesn't — and it doesn't need
to. A reference that arrives ready-made from the remote provider is the
`pass-through` case, where UMP stores nothing: there is **no** storage adapter,
**no** `StoredReference`, and `should_store` is **False**, so V-11 never engages
for it. Such jobs flip straight to `successful` as before. This is deliberate:
UMP did not perform that publication and does not own the remote's URL scheme,
so it is neither able nor entitled to fail a job because a *foreign* server's
link is not yet (or not from inside UMP's network) reachable. The invariant is
clean: **wherever V-11 runs there is always a UMP storage adapter that supplies
`liveness_url`; wherever no adapter exists, V-11 does not run.**

**Internal URL as a probe prerequisite.** Because the probe needs an address
reachable *from inside the UMP container*, `UMP_RESULTSTORE_LDPROXY_INTERNAL_URL`
is required — but (user decision, 2026-08-19) **only when ldproxy is active**,
gated on the existing `ldproxy_required` check
(`ump/composition/result_storage.py`), the same condition already used for
`UMP_RESULTSTORE_LDPROXY_BASE_URL` / `_ROOTPATH`. When no process configures
`result-storage: ldproxy`, the setting stays optional and nothing about the
legacy path changes. (`UMP_RESULTSTORE_LDPROXY_BASE_URL` is the public,
browser-facing URL baked into client links and is usually not reachable from
within UMP — hence the separate internal URL for the probe.)

### Publication deadline + backoff (a setting, user decision on point 3)

The probe retries with exponential backoff until it passes **or** a single
configurable **publication deadline** elapses. Modelled as a real operator
setting rather than a code constant, because the realistic wait differs by
deployment (this is the one place where the k8s-vs-Docker difference is
material, unlike the internal `_FETCH_*` tuning constants):

- **Docker / filesystem backend:** ldproxy hot-reloads within seconds → a ~30 s
  deadline is ample.
- **Kubernetes / ConfigMap backend:** kubelet ConfigMap propagation can take up
  to ~60 s before ldproxy even sees the new collection → default the deadline
  higher (e.g. ~120 s).

Proposed: `UMP_RESULTSTORE_PUBLICATION_TIMEOUT` (seconds, the deadline) with a
sensible default, plus a small backoff base reused/renamed from the existing
storage-fetch backoff constants. During the whole window the job stays `running`
with the "verifying result publication" message.

### Failure semantics — final `failed` with a standardized message

If the store fails, or the probe never passes before the deadline, the job
transitions to **terminal `failed`** (user decision on points 4 + 6). This is
**final — no auto-retry** after `failed` (the backoff retries *within* the
deadline are the only reattempts). The failure carries a **standardized OGC
error / message**, e.g.:

> *"Result computed successfully but its result collection could not be
> published (reference not queryable within the publication timeout)."*

This unifies what were previously two divergent behaviours:
- the old `emulate-ref-only` handling (job left `successful` + `diagnostic`
  marker) is replaced by this honest `failed`;
- the old V-10 `_is_storage_pending` / `503 Results Finalizing` results-path
  window becomes **obsolete and is removed** (user decision on point 7): once
  the status is only `successful` when the link is live, `GET /results` no
  longer needs a "come back shortly" branch. `UMP_RESULTS_FINALIZING_RETRY_AFTER`
  and `_is_storage_pending` are deleted along with it.

### Interaction with the existing `emulate-ref` downgrade (V-10)

V-10's `transmissionModeApplied = "value"` downgrade only applies to *storable-
but-value-allowed* fallbacks (e.g. an `UnsupportedResultError` under
`emulate-ref` where the client permits value). That path is unchanged: it still
resolves to `successful` with the inline value, because a working result *was*
delivered. V-11 governs the different case where a reference *is* required and
the reference channel itself fails — there the answer is `failed`, not a silent
value downgrade. The two are complementary: downgrade = "value was acceptable
and delivered"; V-11 failure = "a working reference was required and could not
be produced".

### Open implementation questions — resolved during coding

1. **Where the sub-state lives.** Resolved as **message-only**, no new
   persisted field on `Job`. The gate is entirely expressed through
   `status_info.status` staying `running` plus a fixed progressing
   `message` (`_AWAITING_PUBLICATION_MESSAGE` in `job_manager.py`). No JSONB
   schema change was needed; `ResultStorageObserver.on_job_completed` still
   receives the *true* final status (`successful`/`failed`) as a plain
   function argument from `JobManager._process_status_update`, so no
   additional state needs to survive between the gate and the finalize step
   within a single poll-loop iteration.
2. **Restart recovery.** Not a new failure mode: a job gated by V-11 is,
   from the repository's point of view, simply `running`. If UMP restarts
   before `ResultStorageObserver` finishes, the job stays `running` exactly
   like any other in-flight job that lost its poll loop — normal poll-loop
   restart/reconciliation (Feature IX) drives it again on the next poll,
   which re-derives `successful` from the remote and re-enters the same
   gate. No bespoke sweep was required.
3. **Naming of the probe/deadline settings.** No new settings were
   introduced. The liveness check reuses the existing, adapter-owned
   publication-confirmation loop (`LdproxyResultStorage._confirm_publication`,
   `UMP_RESULTSTORE_LDPROXY_INTERNAL_URL`) that V-6 already built for exactly
   this purpose; `ResultStorageCoordinator.coordinate()` now returns the
   `StoredReference` list so the observer can inspect each
   `publication_pending` flag once, after `store()` has already exhausted its
   own backoff budget. A separate deadline/backoff setting would have
   duplicated that budget for no operational benefit.
4. **`StoredReference.liveness_url` shape.** Implemented as
   `Optional[str] = None` (see `ump/core/interfaces/result_storage.py`).
   `LdproxyResultStorage._reference_for` builds it as
   `{internal_url}/collections/{collection_id}/items?limit=1` when
   `UMP_RESULTSTORE_LDPROXY_INTERNAL_URL` is configured, `None` otherwise.
   In practice the field is exposed for adapters that want a cheap, generic
   `GET`-and-check-status probe; the ldproxy adapter itself already performs
   a richer confirmation internally (`_confirm_publication`) and surfaces the
   result via `publication_pending`, which is what V-11's gate consumes.

### Implementation summary (2026-07-25)

V-11 is implemented as follows:

- `JobManager._process_status_update` (job_manager.py): when the remote
  reports `successful`, checks `_requires_stored_reference(job)` (a thin
  wrapper around the new pure function
  `result_storage_coordinator.should_store_reference`). If required, the
  *persisted* status is downgraded to `running` with the
  `_AWAITING_PUBLICATION_MESSAGE` appended, no `results`/self links are added
  yet, and the poll loop stops (`terminal_reached = True`) — but observers are
  notified with the **true** final `status_info` (still `successful`), so
  `ResultStorageObserver` knows the remote is actually done.
- `ResultStorageObserver.on_job_completed` (observers.py): unchanged trigger
  conditions (`should_store`), but now owns the terminal transition via the
  new `_finalize_publication` helper: on a clean `coordinate()` result with no
  `publication_pending` references, it persists `successful` (adding the
  self/results links V-11 withheld); on a `ResultStorageError` or any
  `publication_pending` reference, it persists final `failed` with the
  `Job.RESULT_STORAGE_FAILED_MARKER` diagnostic and a standardized message.
  Both paths retry on `OptimisticLockError` like the rest of this module.
- `ResultStorageCoordinator.coordinate()` (result_storage_coordinator.py) now
  returns the `StoredReference` list (or `None`) instead of `None`
  unconditionally, so the observer can inspect `publication_pending` per
  output without re-deriving it.
- `StoredReference.liveness_url` (result_storage.py) and
  `LdproxyResultStorage._reference_for` (ldproxy_result_storage.py) as
  described in question 4 above.
- Removed: `JobManager._is_storage_pending`, the `503 Results Finalizing`
  branch in `get_results`, and `JobManagerConfig.results_finalizing_retry_after`
  (config.py) — all superseded by the gate, since a client can no longer
  observe `successful` before the reference is live.
- Tests: `tests/test_observers.py::TestResultStorageObserver` (gated
  success/failure finalization), `tests/test_job_manager_polling_retry.py::
  TestGatedSuccessfulTransition` (gate persists `running` + notifies true
  status), `tests/test_result_storage_v6.py::TestLivenessUrl` (adapter builds
  `liveness_url`), and the obsolete `TestRequiredStorePendingFinalizingHint`
  503 scenarios were removed from `tests/test_result_storage_v10.py` as
  unreachable under V-11.

## Decisions (confirmed by user 2026-07-24)

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