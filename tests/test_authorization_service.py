"""Unit tests for AuthorizationService (core process-execution access control).

These tests exercise the pure authorization policy in isolation from FastAPI:
anonymous-access bypass, provider-level and process-level roles, and the
401/403 taxonomy. Provider configuration is supplied via a tiny fake so no
network or file I/O is involved.
"""

from typing import List, cast

import pytest

from ump.adapters.colon_process_id_validator import ProcessIdValidator
from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.providers_config import ProviderConfig
from ump.core.services.authorization import AuthorizationService


class FakeProviders:
    """Minimal ProvidersPort stub exposing only get_provider."""

    def __init__(self, providers: dict[str, ProviderConfig], raises: bool = False):
        self._providers = providers
        self._raises = raises

    def get_provider(self, provider_name: str) -> ProviderConfig:
        if self._raises:
            raise RuntimeError("boom: config source unavailable")
        return self._providers[provider_name]


def _provider(name: str, processes: List[dict]) -> ProviderConfig:
    return ProviderConfig.model_validate(
        {"name": name, "url": f"http://{name}.local/", "processes": processes}
    )


def _service(provider: ProviderConfig, raises: bool = False) -> AuthorizationService:
    fake = FakeProviders({provider.name: provider}, raises=raises)
    return AuthorizationService(cast(ProvidersPort, fake), ProcessIdValidator())


def _anon() -> AuthContext:
    return AuthContext(user_id=None, roles=[], is_authenticated=False)


def _user(roles: List[str]) -> AuthContext:
    return AuthContext(user_id="alice", roles=roles, is_authenticated=True)


# --- anonymous-access bypass ------------------------------------------------


def test_anonymous_process_allows_unauthenticated():
    svc = _service(_provider("infra", [{"id": "echo", "anonymous-access": True}]))
    # No exception → access granted
    svc.check_process_access(_anon(), "infra:echo")


def test_anonymous_process_allows_when_configured_id_is_canonical():
    # provider.yaml may already store the canonical (prefixed) id
    svc = _service(_provider("infra", [{"id": "infra:echo", "anonymous-access": True}]))
    svc.check_process_access(_anon(), "infra:echo")


# --- unauthenticated on protected process -----------------------------------


def test_protected_process_rejects_unauthenticated_with_401():
    svc = _service(_provider("infra", [{"id": "echo"}]))
    with pytest.raises(OGCProcessException) as exc:
        svc.check_process_access(_anon(), "infra:echo")
    assert exc.value.response.status == 401


def test_unknown_process_is_not_anonymous():
    # process not declared in config → treated as protected
    svc = _service(_provider("infra", [{"id": "echo"}]))
    with pytest.raises(OGCProcessException) as exc:
        svc.check_process_access(_anon(), "infra:does-not-exist")
    assert exc.value.response.status == 401


# --- role-based authorization -----------------------------------------------


def test_provider_role_grants_access_to_any_process():
    svc = _service(_provider("infra", [{"id": "echo"}]))
    svc.check_process_access(_user(roles=["infra"]), "infra:echo")


def test_process_role_grants_access_to_that_process():
    svc = _service(_provider("infra", [{"id": "echo"}]))
    svc.check_process_access(_user(roles=["infra:echo"]), "infra:echo")


def test_authenticated_without_matching_role_gets_403():
    svc = _service(_provider("infra", [{"id": "echo"}]))
    with pytest.raises(OGCProcessException) as exc:
        svc.check_process_access(
            _user(roles=["other", "infra:something-else"]), "infra:echo"
        )
    assert exc.value.response.status == 403


def test_process_role_does_not_leak_to_sibling_process():
    svc = _service(_provider("infra", [{"id": "echo"}, {"id": "square"}]))
    with pytest.raises(OGCProcessException) as exc:
        svc.check_process_access(_user(roles=["infra:echo"]), "infra:square")
    assert exc.value.response.status == 403


# --- resilience: provider lookup failure ------------------------------------


def test_provider_lookup_failure_denies_anonymous_and_requires_auth():
    # If the config source blows up, anonymous access must NOT be granted;
    # an unauthenticated caller is rejected with 401 (fail closed).
    svc = _service(
        _provider("infra", [{"id": "echo", "anonymous-access": True}]), raises=True
    )
    with pytest.raises(OGCProcessException) as exc:
        svc.check_process_access(_anon(), "infra:echo")
    assert exc.value.response.status == 401


def test_provider_lookup_failure_still_honors_valid_role():
    # A caller holding the right role is still allowed even if the anonymous
    # lookup path failed — the failure only suppresses the anonymous bypass.
    svc = _service(_provider("infra", [{"id": "echo"}]), raises=True)
    svc.check_process_access(_user(roles=["infra"]), "infra:echo")
