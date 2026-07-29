_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature VIII: UMP as execution proxy: add or remove skills to/from remote Models

## Context

Data is transferred from modelserver to UMP to client. The Problem: Large geodata and missing filtering. OGC API Processes v1.0.0 does not allow for subsetting or filtering of large geodata. The Results object is always passed as a block. Models can generate very large
geodata sets, which can cause bottlenecks.

An external result store that only comes into play **after** the data has been received by the UMP solves the
problem too late: for this to work, the data must already have flowed completely from `Model Server → UMP`.

The solution lies in expanding the UMP into an **execution proxy** that can actively
control the data flow-even before the data has completely passed through the UMP.

The UMP acts as a broker for the entire execution lifecycle according to OGC API Processes:

- `POST /processes/{id}/execution` - Receive and forward execution requests
- `GET /jobs` / `GET /jobs/{id}` - Federated job registry across all model servers
- `GET /jobs/{id}/results` - Intercept results and, if necessary, write them to an external store

**execution proxies central skills:**

- federated job registry
- central auth management
- process exclusion
- deterministic caching
- normalizing: UMP can add or deprive model servers of skills, e.g. add `transmissionMode: reference` capability

This proposal addresses `result-storage`, `transmission-mode-policy`, and `response-mode-policy`

## Configuration: `transmission-mode-policy`

For each process, the `providers.yaml` file explicitly configures how the UMP handles the
`transmissionMode` parameter of the OGC standard. The parameter can take four possible values:

---

### `pass-through`

The UMP acts as a transparent proxy. The `transmissionMode` from the client request
is forwarded to the model server unchanged. The UMP's result store is not
used - even if one is configured.

The process description that the UMP communicates externally reflects exactly the native
capabilities of the model server.

**Suitable for:**
- Model servers that natively support `ref` (the model server's link is accessible to clients)
- Model servers that only support `value` when no UMP-side store is desired
- Scenarios in which the UMP should not interfere with the data path

---

### `emulate-ref`

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

### `emulate-ref-only`

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

### `value-only`

The UMP completely blocks `transmissionMode: ref`- even if the model server natively
supports it. The process description is cleaned up accordingly.

A client request with `ref` is rejected by the UMP with an error.
The result store is not used.

**Suitable for:**
- Scenarios in which uniform `value` semantics must be enforced
- Model servers whose native `ref` links are not accessible to all clients and
  where no UMP store is to be operated


## Configuration: `result-storage`

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


## Behaviour Overview

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


## Validierungsregeln für die Konfiguration

When starting the UMP (or reloading `providers.yaml`), the configuration should be validated
as follows:

- `emulate-ref` without `result-storage` → **Error**
- `emulate-ref-only` without `result-storage` → **Error**
- `native` with `result-storage` → **Warning** (Store is ignored)
- `value-only` with `result-storage` → **Warning** (Store is ignored)
- Model can only be configured with `ref` or `result-storage` → **Error** (not supported)


## Impact on the Process Description

The UMP is the **authoritative source** of the process description for all configured processes.
It may modify the process description provided by the model server:

| `transmission-mode` | Change to the process description |
|---|---|
| `native` | None - 1:1 forwarding |
| `emulate-ref` | `transmissionMode: ref` is added if not present |
| `emulate-ref-only` | `transmissionMode` is set to `["ref"]` |
| `value-only` | `transmissionMode` is set to `["value"]` |


This modification is intentional and must be transparent to UMP operators. Clients should act exclusively based on the Process Description and should not have to consult the model server's Process description.

---

## Configuration: `response-mode-policy`

The OGC API Processes standard defines a `response` field in the execute request body with two values:

- `"document"` — the server wraps results in a structured JSON document (conforming to the OGC result schema). UMP can parse this, follow links, and store results.
- `"raw"` — the server returns the unstructured raw output (e.g., binary data, plain GeoJSON) without any JSON envelope.

This matters because UMP as an execution proxy often needs to **inspect** the remote response body (e.g., to extract `statusInfo`, store results, or rewrite links). A `raw` response from the remote may be opaque to UMP's business logic.

The `response-mode-policy` is therefore an operator-level decision that controls **what `response` value UMP actually sends to the remote OGC API Processes server**, independently of what the client requested.

It is a parameter separate from `transmission-mode-policy`:

- `transmission-mode-policy` → controls how result *links vs. values* are handled
- `response-mode-policy` → controls the *encoding format* UMP requests from the remote server

### Policy values

---

#### `pass-through` (default)

UMP forwards the `response` value from the client execute request to the remote server unchanged. The remote response is proxied as-is.

**Suitable for:**
- Remote servers whose `raw` response is directly consumable by clients (no UMP-side result storage needed).
- Scenarios where the operator does not want UMP to interfere with response encoding.

**Risk:** If UMP needs to parse the remote response (e.g., for result storage or link rewriting), a `raw` upstream response may be unreadable. This policy is therefore incompatible with `transmission-mode-policy: emulate-ref` / `emulate-ref-only`.

---

#### `force-document`

UMP always sends `response: "document"` to the remote server, regardless of what the client requested.

- If the client requested `document`: UMP proxies the document response directly.
- If the client requested `raw`: UMP extracts the raw result value from the document envelope before returning it to the client, preserving the client's expected response shape.

**Suitable for:**
- Any configuration where result storage is active (UMP must parse the structured response to store results and generate links).
- Remote servers that produce structured output that benefits from document-level validation and link injection.

**Required when:** `transmission-mode-policy` is `emulate-ref` or `emulate-ref-only`, because UMP must receive a parseable response to write to the result store.

---

#### `force-raw`

UMP always sends `response: "raw"` to the remote server, regardless of what the client requested.

Results are proxied as raw bytes. Result storage and link rewriting are bypassed (UMP cannot inspect raw binary responses).

**Suitable for:**
- Processes that produce binary or non-JSON output and where no UMP-side storage is needed.
- Performance-sensitive scenarios where avoiding JSON serialization overhead is important.

**Incompatible with:** `transmission-mode-policy: emulate-ref` / `emulate-ref-only` (no result store without a parseable response).

---

### Validation rules

When starting the UMP (or reloading `providers.yaml`), the following rules apply:

- `response-mode-policy: pass-through` with `transmission-mode-policy: emulate-ref` or `emulate-ref-only` → **Warning** (client may send `raw`, breaking result storage; consider `force-document`)
- `response-mode-policy: force-raw` with `transmission-mode-policy: emulate-ref` or `emulate-ref-only` → **Error** (result store requires a parseable `document` response)
- `response-mode-policy: force-raw` with `result-storage` configured → **Warning** (result store will never be reachable for raw responses)

### Behaviour overview

| Client `response` | `response-mode-policy` | What UMP sends to remote | What UMP returns to client | Store active? |
|---|---|---|---|---|
| `document` | `pass-through` | `document` | `document` (proxy) | Depends on `transmission-mode-policy` |
| `raw` | `pass-through` | `raw` | `raw` (proxy) | No |
| `document` | `force-document` | `document` | `document` (proxy) | Depends on `transmission-mode-policy` |
| `raw` | `force-document` | `document` | raw content extracted from document | Depends on `transmission-mode-policy` |
| `document` | `force-raw` | `raw` | `raw` (proxy, client expected document) | No |
| `raw` | `force-raw` | `raw` | `raw` (proxy) | No |

### Impact on process description

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

## Output format awareness in UMP

### OGC API Processes results response spec

The HTTP response shape for `GET /jobs/{id}/results` is fully determined by three fields from the original execute request: `response` (`"raw"` or `"document"`), per-output `transmissionMode` (`"value"` or `"reference"`), and the number of outputs requested.

**Full OGC table** (from OGC API - Processes, Part 1):

| Negotiated execute mode | `response` | `transmissionMode` | # outputs | HTTP code | `Content-Type` | Body |
|---|---|---|---|---|---|---|
| sync (server may create job anyway) | any | any | any | — | — | [job results re-fetchable as per async] |
| async | `raw` | `value` | 1 | 200 | as per output definition | raw output bytes |
| async | `raw` | `value` | >1 | 200 | `multipart/related` | one part per output |
| async | `raw` | `reference` | 1 | 204 | — | empty + `Link` headers |
| async | `raw` | mixed | >1 | 200 | `multipart/related` | one part per output |
| async | `document` | `value` | any | 200 | `application/json` | results document |
| async | `document` | `reference` | 1 | 200 | `application/json` | results document with links |

**Critical clarification — `response: "raw"` does NOT mean bytes:**

`"raw"` means the output is returned **without the OGC document envelope** — but the
content itself is whatever the output's `format.mediaType` declares.  A GeoJSON output
with `response: "raw"` returns JSON text.  A FlatGeobuf output returns binary bytes.
What the remote actually sends is determined by the per-output `format.mediaType`, not by
`response`.

`"document"` always returns a JSON wrapper — even binary outputs appear as base64-encoded
strings inside it.

**Consequence for UMP proxy (no local result storage):**

If UMP does not store results itself, it only needs to forward what the remote sends with
the `Content-Type` the remote declared.  No pre-prediction of content type is needed.
The correct proxy strategy is always:

```
body_bytes, content_type = await http.get_content(results_url)
return Response(content=body_bytes, media_type=content_type)
```

The remote's `Content-Type` response header carries the correct MIME type — UMP trusts it
and forwards it verbatim.  `response_mode` and per-output `format.mediaType` only matter
when UMP **stores or transforms** results (Feature VIII).

**`response: "raw"` with multiple outputs returns `multipart/related`** — each part is one output. UMP does not currently parse multipart responses; this case is deferred (see deferred items below).

### Results document format (OGC reference)

A `response: "document"` result body is a JSON object with one key per output. Each value is one of:

- Scalar (inline): `"stringOutput": "Value2"` or `"doubleOutput": "3.14159"`
- Qualified value with optional `mediaType`: `{"value": "<gml:...>", "mediaType": "application/gml+xml"}`
- Inline base64 binary: `{"value": "VBORw0...", "encoding": "base64", "mediaType": "image/tiff"}`
- Reference link: `{"href": "https://...", "type": "application/geo+json"}`

UMP as a proxy does not need to parse this structure today — it proxies the JSON as-is. Only `response-mode-policy: force-document` (Feature VIII) requires UMP to unwrap specific values from the document envelope.

### The problem

An OGC API Processes execute request body can declare, per output, which media type the client wants and how results should be transmitted:

```json
{
  "outputs": {
    "voronoi_diagram": {
      "format": {
        "mediaType": "application/flatgeobuf",
        "schema": "https://geojson.org/schema/FeatureCollection.json"
      },
      "transmissionMode": "value"
    },
    "classification_breaks": {
      "transmissionMode": "value"
    }
  },
  "response": "raw"
}
```

UMP, as a proxy, must be aware of the OGC table above in two places:

1. **Forwarding the execute request**: UMP forwards `outputs`, `response`, and all other fields to the remote server unchanged (as of the current implementation). No format resolution is needed at forward time — the remote handles OGC rules natively.

2. **Proxying the results** (`GET /jobs/{id}/results`): Since UMP does not store results,
   it only needs to forward what the remote sends with the `Content-Type` the remote
   declared.  No content-type prediction is required.  `response_mode` and
   `format.mediaType` are only needed when UMP **stores or transforms** results (Feature VIII).

### What UMP needs to track per job

**For the basic proxy (no result storage — current state):**
Nothing beyond what is already stored. The remote's `Content-Type` response header is
the authoritative source; UMP forwards it verbatim via `get_content()`.

**For Feature VIII (result storage / policy enforcement):**
UMP must additionally capture the original execute request's `response` field and the raw
`outputs` map so it can apply `response-mode-policy` and `transmission-mode-policy`:

```
job.response_mode:  str                        — "raw" | "document" from execute body
job.outputs_spec:   Optional[Dict[str, Any]]   — verbatim execute body "outputs" map
```

And the fully resolved per-output format (for base64-decode decisions):

```
job.output_formats: dict[output_id, ResolvedOutputFormat]   ← Feature VIII only
  output_id:         str        — e.g. "voronoi_diagram"
  media_type:        str        — canonical IANA type, e.g. "application/geo+json"
  transmission_mode: str        — "value" | "reference"
  is_binary:         bool       — True for FlatGeobuf, GeoTIFF, PNG etc.
```

`is_binary` is the critical flag: it tells UMP whether a remote `document` response will contain a base64-encoded string value (binary) or a JSON object/array (JSON-native), which controls how UMP can safely unwrap or pass through the result.

### Output format resolution

UMP resolves the per-output format at execute time by reading the process description it already fetched and cached (via `ProcessManager`):

- For each `output_id` in the execute request's `outputs` map:
  - If the client supplied `format.mediaType`, use it (after validating it is advertised in the process description output schema).
  - Otherwise pick the highest-priority default from the output schema's `oneOf` branches, using a priority list (e.g., `application/geo+json` > `application/json` > binary types).
- If the client omitted `outputs` entirely, resolve all described outputs with their defaults (OGC req. 27).

This logic closely mirrors the `OutputSchemaResolver` in the `fastprocesses` package the user referenced. UMP should implement an equivalent `OutputFormatResolver` in `src/ump/core/utils/output_format_resolver.py` (port-free pure function — no I/O). The key difference from the `fastprocesses` version is that UMP does **not** serialize results itself; it only needs the resolved `(media_type, is_binary, transmission_mode)` triple per output.

### Results proxy behavior

When serving `GET /jobs/{id}/results`, UMP fetches the result from the remote server. The behavior depends on `response-mode-policy` and the stored `output_formats`:

| `response-mode-policy` | Client's original `response` | Remote response shape | UMP action |
|---|---|---|---|
| `pass-through` | `raw` | raw bytes | Stream directly; use stored `media_type` for `Content-Type` |
| `pass-through` | `document` | JSON document | Proxy JSON document as-is |
| `force-document` | `raw`, 1 output | JSON document with value envelope | Unwrap the `value` field; return raw bytes with stored `media_type`. If `is_binary`, base64-decode first. |
| `force-document` | `document` | JSON document | Proxy JSON document as-is |
| `force-raw` | any | raw bytes | Stream directly; use stored `media_type` |

### What is reusable from `fastprocesses`

| Component | Relevance to UMP |
|---|---|
| `OutputSchemaResolver.resolve()` | Directly applicable. UMP needs the same schema-walking logic to derive `(media_type, is_binary)` per output. Adapt rather than import directly (keep UMP's core free of fastprocesses dependency). |
| `ResolvedOutputFormat` dataclass | The `output_id`, `media_type`, `is_binary`, `transmission_mode` fields are all needed. `schema_branch` is only needed during resolution, not at proxy time. |
| `_media_type_from_schema`, `_find_branch`, `_default_media_type`, `_is_binary` | Internal helpers — replicate the logic in `src/ump/core/utils/output_format_resolver.py`. |
| `serialize_result` / `BaseProcessResult` | **Not** applicable to UMP. UMP is a proxy; it never instantiates or calls process logic. Result serialization is handled by streaming the remote response body. |
| `_build_document_response` | Partially applicable: the unwrapping direction (`document → raw`) is the inverse of what fastprocesses does (`result → document`). UMP needs the inverse: extract `value` from a document envelope and return raw bytes. |

### Three concrete gaps that are NOT yet addressed

#### Gap 1 — `response_mode` and `outputs_spec` not stored on the job ← **deferred to Feature VIII**

For the basic proxy path (no result storage), these are not needed — the remote's
`Content-Type` response header tells UMP everything required.

They become necessary when UMP applies `response-mode-policy` or stores results
(Feature VIII): only then does UMP need to know the original `response` and per-output
`format.mediaType` to decide whether to unwrap a document envelope or base64-decode a value.

#### Gap 2 — `HttpClientPort.get()` hard-fails on non-JSON content (current bug)

`AioHttpClientAdapter._fetch_json` calls `resp.json()` unconditionally. For any output
whose content is not JSON (FlatGeobuf, GeoTIFF, multipart, etc.), `resp.json()` raises
`ContentTypeError`, which UMP converts to a 502. This **currently breaks** the results
proxy for all non-JSON outputs.

The fix is simpler than initially designed. Since UMP is a pure proxy (no result storage),
it does not need to predict the content type in advance. Just always fetch raw bytes and
forward the remote's `Content-Type`:

**Fix:** Add `get_content(url) -> tuple[bytes, str]` to the port:

```python
# src/ump/core/interfaces/http_client.py
@abstractmethod
async def get_content(
    self,
    url: str,
    timeout: float | None = None,
    headers: Dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Fetch URL, returning (body_bytes, content_type).

    Never attempts JSON parsing. Always use this for the results proxy.
    The remote's Content-Type header is the authoritative source.
    """
```

`AioHttpClientAdapter.get_content` implementation:

```python
async def get_content(self, url, timeout=None, headers=None) -> tuple[bytes, str]:
    async with self._session.get(url, timeout=..., headers=headers or {}) as resp:
        resp.raise_for_status()
        return await resp.read(), resp.content_type or "application/octet-stream"
```

`JobManager.get_results()` always uses `get_content()`:

```python
body_bytes, ct = await self._http.get_content(results_url, headers=auth_headers)
return {"status": 200, "content_type": ct, "body_bytes": body_bytes}
```

The `GET /jobs/{id}/results` route in `fastapi.py`:

```python
if "body_bytes" in resp:
    return Response(
        content=resp["body_bytes"],
        media_type=resp.get("content_type", "application/octet-stream"),
    )
return JSONResponse(status_code=resp.get("status", 200), content=resp.get("body", {}))
```

No dispatch on `response_mode` or `is_binary` — the remote tells UMP what it sent.

**Deferred:** `response: "raw"` + multiple outputs → `multipart/related`. Bytes are
forwarded opaquely with `Content-Type: multipart/related`. Structured per-part handling
is deferred to Feature VIII.

#### Gap 3 — Binary media-type detection (scope reduced)

With the OGC spec clarified, `is_binary_media_type()` is no longer needed for the basic
proxy path: the `response_mode` field (`"raw"` vs `"document"`) alone determines whether
UMP receives bytes or JSON from the remote.  `is_binary` remains useful only for Feature
VIII (`response-mode-policy: force-document`), where UMP receives a JSON document but must
base64-decode a binary value before returning it to a client that requested `response: "raw"`.

For now, `src/ump/core/utils/media_types.py` is **deferred** to Feature VIII.  The proxy
path only needs `job.response_mode` and a count of value outputs.

---

## ✅ Implemented (basic proxy)

| File | Change |
|---|---|
| `src/ump/core/interfaces/http_client.py` | ✅ `get_content(url, …) -> tuple[bytes, str]` |
| `src/ump/adapters/aiohttp_client_adapter.py` | ✅ `get_content` — `resp.read()` + `resp.content_type`, no JSON parsing |
| tests: fake `HttpClientPort` impls | ✅ `get_content` stub in all 4 fakes |
| `src/ump/core/managers/job_manager.py` | ✅ `get_results` uses `get_content`; returns `body_bytes` + `content_type` |
| `src/ump/adapters/web/fastapi.py` | ✅ results route returns `Response(content=bytes, media_type=ct)` |

## 🔲 Deferred to Feature VIII (result storage / policy)

- `job.response_mode` + `job.outputs_spec` fields + DB migration
- `job.output_formats` (`ResolvedOutputFormat`) + `src/ump/core/utils/output_format_resolver.py`
- `is_binary_media_type()` helper (for base64-decode in force-document path)
- Structured `multipart/related` parsing

---

## Large input data: implementation strategies

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
