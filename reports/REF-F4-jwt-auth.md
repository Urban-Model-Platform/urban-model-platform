_Last_updated: 2026-07-23

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature IV: JWT-based Auth (user → UMP) ✅ (implemented)

## Scope

User-to-UMP authentication via JWT (OIDC standard). Distinct from Feature VII (UMP → remote servers). Supports any OIDC-compliant IdP (Keycloak, Auth0, Okta, Azure AD, …) with no IdP-specific adapter — differences in claim location are handled by configuration.

## Authentication flow

```
Client              UMP                     IdP (Keycloak / any OIDC)
  │                   │                              │
  │─ GET /token ───────────────────────────────────►│
  │◄─ access_token (JWT, RS256-signed) ─────────────│
  │                   │                              │
  │─ POST /processes/{id}/execution                  │
  │  Authorization: Bearer <token> ──────────────►  │
  │                   │                              │
  │          [offline validation]                    │
  │          1. fetch JWKS if not cached             │
  │             (once, TTL-based refresh)            │
  │          2. verify signature (RS256/ES256)       │
  │          3. check exp, nbf, iss, aud             │
  │          4. extract sub → user_id                │
  │          5. extract roles from configured claims │
  │                   │                              │
  │          [authorization check]                   │
  │          does user have role for this process?   │
  │                   │                              │
  │◄─ 201 / 401 / 403 │                              │
```

No call to the IdP on every request. JWKS keys are cached and refreshed on TTL expiry or when an unknown `kid` is seen (key rotation defence).

### What UMP validates in every token

| Check | Claim | Detail |
|---|---|---|
| Signature | header + `kid` → JWKS | Proves IdP origin |
| Expiry | `exp` | Reject stale tokens |
| Issuer | `iss` | Must equal `UMP_JWT_ISSUER` |
| Audience | `aud` | Must contain `UMP_JWT_AUDIENCE` |
| Not-before | `nbf` | Edge case; reject future-dated tokens |
| Clock skew | — | ±30 s tolerance on `exp`/`nbf` |

**UMP does NOT handle**: token refresh (client's job), token revocation before expiry, session management — JWTs are stateless.

### JWKS caching and key rotation

```
On startup / cache miss:
  fetch {UMP_JWKS_URL}  ─►  parse key set  ─►  store in cache (TTL = UMP_JWKS_CACHE_TTL_SECONDS)

Per request:
  decode JWT header → kid
  if kid in cache  → verify signature
  else             → re-fetch JWKS (key rotation)
                     if still not found → 401
```

### Authorization rules (role-based)

Roles are extracted from the token and compared against a two-level rule:

- **Provider role** `{provider_name}` — grants access to execute any process from that provider
- **Process role** `{provider_name}:{process_bare_id}` — grants access to one specific process

Examples with canonical UMP process IDs:
- Role `fair2adapt` → can execute all `fair2adapt:*` processes
- Role `fair2adapt:pluvial-flood-risk-citywide` → can execute only that process

Additionally:
- Processes with `anonymous-access: true` in `providers.yaml` are executable without a token
- `GET /processes` and `GET /processes/{id}` are optionally public via `UMP_PUBLIC_PROCESSES=true`
- All other routes (jobs, execution) require a valid token when `UMP_AUTH_ENABLED=true`

When `UMP_AUTH_ENABLED=false` (development/testing):
- All tokens are silently ignored (including malformed ones)
- Every request is treated as having full access

Anonymous-access jobs store `user_id = null` — no user tracking required.

## Hexagonal architecture split

**Core** (`src/ump/core/interfaces/auth.py`):
```python
@dataclass
class AuthContext:
    user_id: Optional[str]    # None for anonymous requests
    roles: List[str]          # flat list merged from all configured claim paths
    is_authenticated: bool

class AuthPort(ABC):
    @abstractmethod
    async def verify(self, token: Optional[str]) -> AuthContext:
        """Validate token and return context.
        
        Returns AuthContext(is_authenticated=False) when auth is disabled or
        token is absent and the route allows anonymous access.
        Raises OGCProcessException(401) for invalid/expired tokens.
        """
```

**Adapter** (`src/ump/adapters/jwt_auth_adapter.py`):
- Fetches + caches JWKS from `UMP_JWKS_URL`
- Verifies RS256/ES256 signatures using `python-jose[cryptography]`
- Validates standard claims (`exp`, `nbf`, `iss`, `aud`)
- Walks configured `UMP_JWT_ROLES_CLAIMS` paths to extract and merge roles
- Implements `UMP_AUTH_ENABLED=false` bypass

No Keycloak-specific code. Operators configure claim paths for their IdP.

**FastAPI wiring** — dependency injection per route (not middleware):
```python
# fastapi.py
async def _require_auth(request: Request) -> AuthContext:
    token = _extract_bearer_token(request)  # None if header absent
    return await app.state.auth_port.verify(token)

@api_router.post("/processes/{process_id}/execution")
async def execute_process(request: Request, process_id: str,
                          auth: AuthContext = Depends(_require_auth)):
    _check_process_access(auth, process_id, app.state.process_port)
    ...

def _check_process_access(auth: AuthContext, process_id: str, process_port) -> None:
    """Raise 403 if the user lacks access to this process."""
    # resolve provider_name from process_id
    provider_name, _ = process_id.split(":", 1) if ":" in process_id else (None, process_id)
    # anonymous-access bypass (read from provider config)
    proc_config = process_port.get_process_config(provider_name, process_id)
    if proc_config and proc_config.anonymous_access:
        return
    if not auth.is_authenticated:
        raise OGCProcessException(401, ...)
    if provider_name not in auth.roles and process_id not in auth.roles:
        raise OGCProcessException(403, ...)
```

`Depends` is preferred over middleware: individual routes declare their auth requirement; public routes simply don't inject the dependency.

## Configuration settings

| Setting | Description | Default |
|---|---|---|
| `UMP_AUTH_ENABLED` | Master switch; `false` disables all auth | `true` |
| `UMP_JWKS_URL` | JWKS endpoint (e.g. `https://keycloak:8080/.../certs`) | required |
| `UMP_JWT_ISSUER` | Expected `iss` claim | required |
| `UMP_JWT_AUDIENCE` | Expected `aud` claim (client ID) | required |
| `UMP_JWT_ROLES_CLAIMS` | Comma-separated dot-paths to role arrays in token | `realm_access.roles` |
| `UMP_JWKS_CACHE_TTL_SECONDS` | Public-key cache TTL | `3600` |
| `UMP_PUBLIC_PROCESSES` | Allow unauthenticated process discovery | `false` |

**Keycloak example** (both realm and client roles):
```
UMP_JWT_ROLES_CLAIMS=realm_access.roles,resource_access.ump-client.roles
```

**Azure AD example** (flat roles claim):
```
UMP_JWT_ROLES_CLAIMS=roles
```

## Library

**`python-jose[cryptography]`** — standard FastAPI JWT library, supports RS256/ES256, parses all standard claims, no IdP dependency.

JWKS fetching uses `aiohttp` (already a dependency) with a simple in-memory async cache.

## Files to create / modify

| File | Action | Notes |
|---|---|---|
| `src/ump/core/interfaces/auth.py` | CREATE | `AuthPort` + `AuthContext` |
| `src/ump/adapters/jwt_auth_adapter.py` | CREATE | Generic OIDC adapter, configurable claim paths, JWKS cache |
| `src/ump/core/settings.py` | MODIFY | Add `UMP_AUTH_ENABLED`, `UMP_JWKS_URL`, `UMP_JWT_ISSUER`, `UMP_JWT_AUDIENCE`, `UMP_JWT_ROLES_CLAIMS`, `UMP_JWKS_CACHE_TTL_SECONDS`, `UMP_PUBLIC_PROCESSES` |
| `src/ump/adapters/web/fastapi.py` | MODIFY | Add `_require_auth` dependency; inject `auth_port`; add `_check_process_access` helper |
| `src/ump/main.py` | MODIFY | Instantiate `JwtAuthAdapter`; wire into `create_app` |
