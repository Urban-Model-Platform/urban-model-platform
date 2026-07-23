"""Unit tests for JwtAuthAdapter (generic OIDC token validation).

A throwaway RSA keypair is generated in-process; its public half is published
as a JWKS dict seeded directly into the adapter's cache, so no network I/O is
performed. Tokens are minted with the private half and validated offline —
exactly as the adapter does in production.
"""

import time
from typing import Any, Dict

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from ump.core.exceptions import OGCProcessException
from ump.core.settings import UmpSettings

_KID = "test-kid"
_ISSUER = "https://idp.test/realms/ump"
_AUDIENCE = "ump-api"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    jwk_dict = jwk.construct(public_pem, algorithm="RS256").to_dict()
    jwk_dict["kid"] = _KID
    jwk_dict["alg"] = "RS256"
    return private_pem, jwk_dict


def _mint(private_pem: str, claims: Dict[str, Any], kid: str | None = _KID) -> str:
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, private_pem, algorithm="RS256", headers=headers)


def _base_claims(**overrides: Any) -> Dict[str, Any]:
    now = int(time.time())
    claims: Dict[str, Any] = {
        "sub": "user-123",
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": now,
        "nbf": now - 10,
        "exp": now + 3600,
        "realm_access": {"roles": ["infra", "infra:echo"]},
    }
    claims.update(overrides)
    return claims


def _settings(**overrides: Any) -> UmpSettings:
    params: Dict[str, Any] = {
        "UMP_AUTH_ENABLED": True,
        "UMP_JWKS_URL": "http://idp.test/jwks",
        "UMP_JWT_ISSUER": _ISSUER,
        "UMP_JWT_AUDIENCE": _AUDIENCE,
        "UMP_JWT_ROLES_CLAIMS": "realm_access.roles",
    }
    params.update(overrides)
    return UmpSettings(**params)


def _adapter(settings: UmpSettings, jwk_dict: Dict[str, Any], kid: str = _KID):
    from ump.adapters.jwt_auth_adapter import JwtAuthAdapter

    adapter = JwtAuthAdapter(settings)
    # Seed the JWKS cache so no network fetch occurs.
    adapter._jwks = {kid: jwk_dict}
    adapter._fetched_at = time.monotonic()
    return adapter


# --- auth-disabled bypass ---------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_ignores_even_garbage_tokens(keypair):
    _, jwk_dict = keypair
    adapter = _adapter(_settings(UMP_AUTH_ENABLED=False), jwk_dict)
    ctx = await adapter.verify("this-is-not-a-jwt")
    assert ctx.is_authenticated is False
    assert ctx.user_id is None
    assert ctx.roles == []


@pytest.mark.asyncio
async def test_missing_token_is_anonymous(keypair):
    _, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    ctx = await adapter.verify(None)
    assert ctx.is_authenticated is False
    assert ctx.user_id is None


# --- valid token ------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token_is_authenticated_with_roles(keypair):
    private_pem, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    token = _mint(private_pem, _base_claims())
    ctx = await adapter.verify(token)
    assert ctx.is_authenticated is True
    assert ctx.user_id == "user-123"
    assert set(ctx.roles) == {"infra", "infra:echo"}


# --- rejected tokens --------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_token_rejected_401(keypair):
    private_pem, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    token = _mint(private_pem, _base_claims(exp=int(time.time()) - 3600))
    with pytest.raises(OGCProcessException) as exc:
        await adapter.verify(token)
    assert exc.value.response.status == 401


@pytest.mark.asyncio
async def test_wrong_audience_rejected_401(keypair):
    private_pem, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    token = _mint(private_pem, _base_claims(aud="some-other-api"))
    with pytest.raises(OGCProcessException) as exc:
        await adapter.verify(token)
    assert exc.value.response.status == 401


@pytest.mark.asyncio
async def test_wrong_issuer_rejected_401(keypair):
    private_pem, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    token = _mint(private_pem, _base_claims(iss="https://evil.test/"))
    with pytest.raises(OGCProcessException) as exc:
        await adapter.verify(token)
    assert exc.value.response.status == 401


@pytest.mark.asyncio
async def test_unknown_kid_rejected_401(keypair, monkeypatch):
    private_pem, jwk_dict = keypair
    # Cache holds "test-kid"; token is signed with a different kid.
    adapter = _adapter(_settings(), jwk_dict)

    async def _noop_fetch():
        return None  # simulate a re-fetch that still lacks the kid

    monkeypatch.setattr(adapter, "_fetch_jwks", _noop_fetch)
    token = _mint(private_pem, _base_claims(), kid="rotated-away")
    with pytest.raises(OGCProcessException) as exc:
        await adapter.verify(token)
    assert exc.value.response.status == 401


# --- role extraction --------------------------------------------------------


@pytest.mark.asyncio
async def test_roles_merged_from_multiple_claim_paths(keypair):
    private_pem, jwk_dict = keypair
    settings = _settings(
        UMP_JWT_ROLES_CLAIMS="realm_access.roles,resource_access.ump.roles"
    )
    adapter = _adapter(settings, jwk_dict)
    claims = _base_claims(
        realm_access={"roles": ["infra"]},
        resource_access={"ump": {"roles": ["infra:echo", "reports"]}},
    )
    token = _mint(private_pem, claims)
    ctx = await adapter.verify(token)
    assert set(ctx.roles) == {"infra", "infra:echo", "reports"}


@pytest.mark.asyncio
async def test_missing_roles_claim_yields_empty_list(keypair):
    private_pem, jwk_dict = keypair
    adapter = _adapter(_settings(), jwk_dict)
    claims = _base_claims()
    del claims["realm_access"]
    token = _mint(private_pem, claims)
    ctx = await adapter.verify(token)
    assert ctx.is_authenticated is True
    assert ctx.roles == []
