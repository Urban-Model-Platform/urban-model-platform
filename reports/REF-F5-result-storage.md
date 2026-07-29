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