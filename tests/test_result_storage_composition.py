"""Tests for the V-8 composition wiring (ump.composition.result_storage).

These tests exercise only the pure factory functions — never `ump.asgi` —
so they run with zero infrastructure: no DB, no file watcher, no real
`kubernetes` client, no GDAL/geopandas import triggered when ldproxy is not
in use.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from ump.adapters.result_storage.entity_config_fs import FilesystemEntityConfigBackend
from ump.adapters.result_storage.ldproxy_result_storage import LdproxyResultStorage
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.composition.result_storage import (
    build_entity_config_backend,
    build_result_storage_port,
    ensure_ldproxy_bootstrapped,
    ldproxy_required,
    resolve_confirm_budget,
)
from ump.core.interfaces.result_storage import NullResultStorage
from ump.core.settings import UmpSettings


def _process(result_storage: str) -> Mock:
    process = Mock()
    process.result_storage = result_storage
    return process


def _providers_with(*result_storage_values: str) -> Mock:
    """Build a fake ProvidersPort exposing one provider with these processes."""
    provider = Mock()
    provider.processes = [_process(v) for v in result_storage_values]
    providers = Mock()
    providers.get_providers = Mock(return_value=[provider])
    return providers


def _settings(**overrides) -> UmpSettings:
    return UmpSettings(**overrides)


# --- ldproxy_required ---------------------------------------------------


class TestLdproxyRequired:
    def test_false_when_no_processes(self):
        assert ldproxy_required(_providers_with()) is False

    def test_false_when_only_remote_and_geoserver(self):
        providers = _providers_with("remote", "geoserver")
        assert ldproxy_required(providers) is False

    def test_true_when_any_process_uses_ldproxy(self):
        providers = _providers_with("remote", "ldproxy")
        assert ldproxy_required(providers) is True


# --- build_entity_config_backend -----------------------------------------


class TestBuildEntityConfigBackend:
    def test_filesystem_backend(self, tmp_path):
        settings = _settings(
            UMP_RESULTSTORE_CONFIG_BACKEND="filesystem",
            UMP_RESULTSTORE_LDPROXY_ROOTPATH=str(tmp_path),
        )
        backend = build_entity_config_backend(settings)
        assert isinstance(backend, FilesystemEntityConfigBackend)

    def test_k8s_backend(self):
        settings = _settings(
            UMP_RESULTSTORE_CONFIG_BACKEND="k8s",
            UMP_RESULTSTORE_K8S_NAMESPACE="ump-namespace",
        )
        # K8sConfigMapEntityConfigBackend lazily imports `kubernetes` unless
        # a core_v1_api is injected — build_entity_config_backend does not
        # inject one, so we only assert the *type*, not construct-and-call,
        # to keep this test free of the optional dependency. Constructing it
        # eagerly builds the default client, which fails without in-cluster
        # config; that failure itself proves the dispatch picked the k8s
        # branch, which is what this test cares about.
        from kubernetes.config.config_exception import (  # type: ignore[import-untyped]
            ConfigException,
        )

        with pytest.raises(ConfigException):
            build_entity_config_backend(settings)

    def test_unknown_backend_raises(self):
        settings = _settings(UMP_RESULTSTORE_CONFIG_BACKEND="carrier-pigeon")
        with pytest.raises(ValueError, match="carrier-pigeon"):
            build_entity_config_backend(settings)


# --- resolve_confirm_budget ------------------------------------------------


class TestResolveConfirmBudget:
    """The one operational difference between the two backends (V-13e).

    With the k8s backend an entity reaches ldproxy's disk only when the kubelet
    resyncs the mounted ConfigMap (~60 s), so a budget tuned for the filesystem
    watcher's seconds-scale reload would have V-11's gate fail every job while
    its collection is merely still in transit.
    """

    def test_k8s_budget_covers_kubelet_propagation(self):
        fs = resolve_confirm_budget(
            _settings(UMP_RESULTSTORE_CONFIG_BACKEND="filesystem")
        )
        k8s = resolve_confirm_budget(_settings(UMP_RESULTSTORE_CONFIG_BACKEND="k8s"))
        assert _worst_case_seconds(k8s) > 120
        assert _worst_case_seconds(k8s) > _worst_case_seconds(fs)


def _worst_case_seconds(budget) -> float:
    """Upper bound on how long the adapter will keep confirming."""
    return sum(
        min(budget.base_wait * 2**attempt, budget.max_wait)
        for attempt in range(budget.max_attempts)
    )


# --- build_result_storage_port --------------------------------------------


class TestBuildResultStoragePort:
    def test_null_storage_when_ldproxy_not_required(self):
        providers = _providers_with("remote", "geoserver")
        settings = _settings()

        port, registry = build_result_storage_port(settings, providers)

        assert isinstance(port, NullResultStorage)
        assert registry is None

    def test_ldproxy_storage_when_required(self, tmp_path):
        providers = _providers_with("ldproxy")
        settings = _settings(
            UMP_RESULTSTORE_CONFIG_BACKEND="filesystem",
            UMP_RESULTSTORE_LDPROXY_ROOTPATH=str(tmp_path),
            UMP_RESULTSTORE_LDPROXY_BASE_URL="https://geodata.example.com/ump-results",
        )

        port, registry = build_result_storage_port(settings, providers)

        assert isinstance(port, LdproxyResultStorage)
        assert isinstance(registry, ServiceRegistry)

    def test_registry_and_adapter_share_the_same_backend(self, tmp_path):
        """The adapter and the registry must operate on the same backend
        instance, otherwise the registry's lock/version guarantees would not
        cover writes the adapter makes through a *different* backend object.
        """
        providers = _providers_with("ldproxy")
        settings = _settings(
            UMP_RESULTSTORE_CONFIG_BACKEND="filesystem",
            UMP_RESULTSTORE_LDPROXY_ROOTPATH=str(tmp_path),
            UMP_RESULTSTORE_LDPROXY_BASE_URL="https://geodata.example.com/ump-results",
        )

        port, registry = build_result_storage_port(settings, providers)

        # Accessing the private `_backend` attribute is a deliberate,
        # narrowly-scoped exception to test a wiring invariant that has no
        # public API to observe otherwise: the ServiceRegistry's asyncio.Lock
        # only prevents lost updates if every writer to the shared service
        # entity — including the adapter itself — goes through the *same*
        # backend instance as the registry.
        assert port._backend is registry._backend  # type: ignore[union-attr]


# --- ensure_ldproxy_bootstrapped -------------------------------------------


class TestEnsureLdproxyBootstrapped:
    @pytest.mark.asyncio
    async def test_noop_when_registry_is_none(self):
        # Must not raise — this is the "no ldproxy configured" case.
        await ensure_ldproxy_bootstrapped(None)

    @pytest.mark.asyncio
    async def test_calls_ensure_bootstrapped_on_registry(self):
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()

        await ensure_ldproxy_bootstrapped(registry)

        registry.ensure_bootstrapped.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swallows_reachability_failures(self):
        """A transient failure (share not mounted, API unavailable) must not
        propagate — the composition root treats it as a startup warning, not
        a fatal error, and relies on the registry's lazy self-healing bootstrap
        on the first successful `register_collection`.
        """
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock(
            side_effect=ConnectionError("share not mounted yet")
        )

        # Must not raise.
        await ensure_ldproxy_bootstrapped(registry)

    @pytest.mark.asyncio
    async def test_ensures_default_provider_for_ldproxy_port(self):
        """When a real LdproxyResultStorage is passed, its default feature
        provider must be created — this is the second bootstrap step ldproxy
        3.x requires before it will start the shared OGC_API service.
        """
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()
        port = Mock(spec=LdproxyResultStorage)
        port.ensure_default_provider = AsyncMock()

        await ensure_ldproxy_bootstrapped(registry, port)

        registry.ensure_bootstrapped.assert_awaited_once()
        port.ensure_default_provider.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_default_provider_for_non_ldproxy_port(self):
        """A non-Ldproxy port (e.g. NullResultStorage) carries no default
        provider; the isinstance guard must skip it rather than call a method
        it does not have.
        """
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()
        port = NullResultStorage()

        # Must not raise (NullResultStorage has no ensure_default_provider).
        await ensure_ldproxy_bootstrapped(registry, port)

        registry.ensure_bootstrapped.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_default_provider_when_port_is_none(self):
        """The default storage_port argument is None (callers that only pass a
        registry). The registry step still runs; nothing else is attempted.
        """
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()

        await ensure_ldproxy_bootstrapped(registry)

        registry.ensure_bootstrapped.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_provider_failure_is_swallowed(self):
        """A failure in the default-provider step is transient (share not
        mounted yet) and must be swallowed exactly like the registry step \u2014
        it self-heals on the first stored result.
        """
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()
        port = Mock(spec=LdproxyResultStorage)
        port.ensure_default_provider = AsyncMock(
            side_effect=ConnectionError("share not mounted yet")
        )

        # Must not raise.
        await ensure_ldproxy_bootstrapped(registry, port)

    @pytest.mark.asyncio
    async def test_default_provider_written_before_service(self):
        """Order is a correctness requirement, not an incidental detail.

        ldproxy refuses to start a service whose default provider it cannot
        resolve, and on a cold start its file watcher processes new entity
        files in write order and does NOT retry a service that failed for a
        missing provider. So the provider MUST be written before the service
        entity, or the very first job's collection is unreachable until an
        ldproxy restart.
        """
        manager = Mock()
        registry = Mock()
        registry.ensure_bootstrapped = AsyncMock()
        port = Mock(spec=LdproxyResultStorage)
        port.ensure_default_provider = AsyncMock()
        # Attach both to a shared manager so call order is recorded globally.
        manager.attach_mock(registry.ensure_bootstrapped, "service")
        manager.attach_mock(port.ensure_default_provider, "provider")

        await ensure_ldproxy_bootstrapped(registry, port)

        order = [c[0] for c in manager.mock_calls]
        assert order == ["provider", "service"], (
            f"default provider must be written before the service entity, "
            f"got call order: {order}"
        )
