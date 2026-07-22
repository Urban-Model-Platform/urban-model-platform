"""JwtAuthAdapter — generic OIDC JWT authentication adapter.

Validates inbound Bearer tokens offline using cached JWKS public keys.
Works with any OIDC-compliant IdP (Keycloak, Auth0, Okta, Azure AD, …).
IdP-specific differences (where roles live in the token) are handled
entirely through configuration (UMP_JWT_ROLES_CLAIMS dot-paths).

Key features:
- Offline validation: no call to the IdP per request
- JWKS cache with configurable TTL
- Automatic cache refresh on unknown 'kid' (key-rotation defence)
- Clock-skew tolerance of ±30 s on exp / nbf
- UMP_AUTH_ENABLED=false: returns unauthenticated context for every token
  (including malformed ones) — safe bypass for dev/test
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp
from jose import JWTError, jwt

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.settings import UmpSettings

# Clock-skew tolerance applied to 'exp' and 'nbf' claims (seconds)
_LEEWAY_SECONDS = 30


def _401(detail: str) -> OGCProcessException:
    return OGCProcessException(
        OGCExceptionResponse(
            type="about:blank",
            title="Unauthorized",
            status=401,
            detail=detail,
            instance=None,
        )
    )


class JwtAuthAdapter(AuthPort):
    """Generic OIDC JWT adapter.

    Stateful (holds the JWKS cache). Inject one shared instance.
    Thread- and async-safe via asyncio.Lock on cache refresh.
    """

    def __init__(self, settings: UmpSettings) -> None:
        self._settings = settings
        # kid → raw JWKS key dict
        self._jwks: Dict[str, Any] = {}
        self._fetched_at: Optional[float] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # AuthPort implementation
    # ------------------------------------------------------------------

    async def verify(self, token: Optional[str]) -> AuthContext:
        """Validate *token* and return the caller's AuthContext.

        - Auth disabled or no token → unauthenticated context
        - Valid token               → authenticated context with roles
        - Expired / invalid         → OGCProcessException 401
        """
        if not self._settings.UMP_AUTH_ENABLED:
            return AuthContext(user_id=None, roles=[], is_authenticated=False)

        if not token:
            return AuthContext(user_id=None, roles=[], is_authenticated=False)

        key = await self._get_signing_key(token)

        try:
            options: Dict[str, Any] = {"leeway": _LEEWAY_SECONDS}
            decode_kwargs: Dict[str, Any] = {
                "algorithms": ["RS256", "ES256"],
                "options": options,
            }
            if self._settings.UMP_JWT_ISSUER:
                decode_kwargs["issuer"] = self._settings.UMP_JWT_ISSUER
            if self._settings.UMP_JWT_AUDIENCE:
                decode_kwargs["audience"] = self._settings.UMP_JWT_AUDIENCE

            claims = jwt.decode(token, key, **decode_kwargs)
        except JWTError as exc:
            raise _401(f"Token validation failed: {exc}") from exc

        user_id: Optional[str] = claims.get("sub")
        roles = self._extract_roles(claims)
        return AuthContext(user_id=user_id, roles=roles, is_authenticated=True)

    # ------------------------------------------------------------------
    # JWKS helpers
    # ------------------------------------------------------------------

    async def _get_signing_key(self, token: str) -> Dict[str, Any]:
        """Return the JWKS key that matches the token's 'kid' header."""
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise _401(f"Cannot decode token header: {exc}") from exc

        kid: Optional[str] = header.get("kid")

        # Try from cache first (refreshing if TTL expired)
        await self._refresh_if_stale()
        if kid and kid in self._jwks:
            return self._jwks[kid]

        # Unknown kid: key may have been rotated — force one re-fetch
        await self._fetch_jwks()
        if kid and kid in self._jwks:
            return self._jwks[kid]

        # No kid in header — try the only key if exactly one is present
        if not kid and len(self._jwks) == 1:
            return next(iter(self._jwks.values()))

        raise _401(
            f"No matching public key found for kid={kid!r}. "
            "Check UMP_JWKS_URL points to the correct IdP."
        )

    async def _refresh_if_stale(self) -> None:
        ttl = self._settings.UMP_JWKS_CACHE_TTL_SECONDS
        if self._fetched_at is None or (time.monotonic() - self._fetched_at) > ttl:
            await self._fetch_jwks()

    async def _fetch_jwks(self) -> None:
        """Fetch the JWKS endpoint and update the in-memory key cache."""
        async with self._lock:
            # Double-checked: another coroutine may have fetched while we waited
            ttl = self._settings.UMP_JWKS_CACHE_TTL_SECONDS
            if self._fetched_at and (time.monotonic() - self._fetched_at) < ttl:
                return

            url = self._settings.UMP_JWKS_URL
            if not url:
                raise _401(
                    "UMP_JWKS_URL is not configured. "
                    "Set it to the JWKS endpoint of your IdP."
                )

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
            except Exception as exc:
                raise _401(f"Failed to fetch JWKS from {url}: {exc}") from exc

            keys = data.get("keys", [])
            self._jwks = {k["kid"]: k for k in keys if "kid" in k}
            # Also index keyless entries by their 'n' (RSA modulus) for single-key IdPs
            for k in keys:
                if "kid" not in k:
                    self._jwks[k.get("n", "__no_kid__")] = k
            self._fetched_at = time.monotonic()

    # ------------------------------------------------------------------
    # Role extraction
    # ------------------------------------------------------------------

    def _extract_roles(self, claims: Dict[str, Any]) -> List[str]:
        """Walk each configured dot-path and merge the discovered role arrays."""
        roles: List[str] = []
        for raw_path in self._settings.UMP_JWT_ROLES_CLAIMS.split(","):
            path = raw_path.strip()
            if not path:
                continue
            value: Any = claims
            for part in path.split("."):
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            if isinstance(value, list):
                roles.extend(str(r) for r in value if r)
        return roles
