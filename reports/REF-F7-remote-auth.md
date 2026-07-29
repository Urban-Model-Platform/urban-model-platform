_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature VII: Remote server authentication (UMP → provider) ✅ (implemented)

This is a distinct concern from Feature IV. Feature IV secures *inbound* requests (clients authenticating against UMP). Feature VII secures *outbound* requests (UMP authenticating against remote OGC API Processes servers).

## Current state

`ProviderConfig.authentication: AuthConfig` already exists in the core model (`src/ump/core/models/providers_config.py`) with four variants:

| Type | Fields | Use case |
|---|---|---|
| `NoAuth` | — | Public servers (default) |
| `BasicAuth` | `user`, `password: SecretStr` | HTTP Basic Auth |
| `ApiKey` | `key_name`, `key_value: SecretStr` | API key header (any header name) |
| `BearerToken` | `token: SecretStr` | Static bearer token |

The data model is complete. What is missing is the port + adapter that converts `AuthConfig` to HTTP headers, and the wiring that applies them at every outbound call site.

## Hexagonal architecture split

**Core** (no infrastructure knowledge):
- `ProviderCredentials` — a simple `dataclass` with `headers: Dict[str, str]`; result of credential resolution
- `RemoteAuthPort` — interface: `resolve(auth_config: AuthConfig) -> ProviderCredentials`

**Adapter** (owns encoding knowledge):
- `RemoteAuthAdapter` — implements `RemoteAuthPort`; converts each auth type to headers:
  - `BasicAuth` → `Authorization: Basic base64(user:pass)`
  - `BearerToken` → `Authorization: Bearer <token>`
  - `ApiKey` → `{key_name}: <key_value>` (uses the configured header name)
  - `NoAuth` → empty headers
  
  The adapter imports `base64`; the core does not. `HttpClientPort` stays unchanged — auth is expressed purely as additional `headers` entries.

## Call sites that need auth headers

All four places where UMP makes outbound requests to a remote provider need the credentials merged into the request headers:

| Call site | File | Has `ProviderConfig`? |
|---|---|---|
| `ProcessManager._fetch_process` | `process_manager.py` | ✅ via `provider.authentication` |
| `ForwardToProviderStep.process` | `steps/execution_steps.py` | ✅ via `context.provider.authentication` |
| `JobManager._poll_and_update_status` | `job_manager.py` | ✅ via `job.provider` → `get_provider()` |
| `JobManager.get_results` | `job_manager.py` | ✅ via `job.provider` → `get_provider()` |

The pattern at each site is:
```python
auth_headers = self._remote_auth.resolve(provider.authentication).headers
merged_headers = {**auth_headers, **forward_headers}
await self._http.post(url, json=payload, headers=merged_headers)
```

## Injection

`RemoteAuthAdapter` is stateless — a single shared instance injected at the composition root:

```python
# main.py
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
remote_auth = RemoteAuthAdapter()
# pass to process_manager_factory and job_manager_factory
```

`ProcessManager.__init__` and `JobManager.__init__` gain `remote_auth: RemoteAuthPort`; `ForwardToProviderStep.__init__` gains it too (injected via `_build_execution_pipeline()`).

## Future extension: OAuth2 client credentials

When a provider requires dynamic token refresh (OAuth2 client credentials flow), add a new union member to `AuthConfig`:

```python
class OAuthClientCredentialsConfig(BaseModel):
    type: Literal["OAuthClientCredentials"]
    client_id: str
    client_secret: SecretStr
    token_url: HttpUrl
    scope: Optional[str] = None
```

The adapter's `resolve()` method fetches and caches a token using the `token_url`. The port signature (`resolve(AuthConfig) -> ProviderCredentials`) remains unchanged. No core changes needed.

## Files to create / modify

| File | Action | Notes |
|---|---|---|
| `RemoteAuthPort` + `ProviderCredentials` | ✅ | `src/ump/core/interfaces/remote_auth.py` |
| `RemoteAuthAdapter` (BasicAuth, BearerToken, ApiKey, NoAuth) | ✅ | `src/ump/adapters/remote_auth_adapter.py` |
| `src/ump/core/managers/process_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in `_fetch_process` |
| `src/ump/core/managers/steps/execution_steps.py` | MODIFY | `ForwardToProviderStep` accepts + uses `RemoteAuthPort` |
| `src/ump/core/managers/job_manager.py` | MODIFY | Accept + use `RemoteAuthPort` in polling and results proxy |
| `src/ump/main.py` | MODIFY | Instantiate `RemoteAuthAdapter`, inject into factories |
