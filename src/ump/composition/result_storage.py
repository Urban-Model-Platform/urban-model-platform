"""Composition for Feature V result storage.

This module answers exactly one question for the process as a whole: *which*
``ResultStoragePort`` should the rest of UMP use?  Every function here is pure
wiring — it takes its dependencies as parameters and returns fully constructed
objects.  Nothing here reaches for module-level globals or triggers I/O at
import time, so tests can call these functions directly with fakes and never
touch ``ump.asgi`` (which does have import-time side effects: file watchers,
DB connections, app construction).

Design summary (see reports/REF-F5-result-storage.md, for the full
discussion this codifies):

  * If no configured process uses ``result-storage: ldproxy``, the whole
    ldproxy stack (GDAL/geopandas imports included) is skipped entirely and
    ``NullResultStorage`` is returned.  A plain pass-through UMP deployment
    never pays for, or depends on, the storage feature.
  * ``result-storage: geoserver`` is out of scope for Feature V and is treated
    identically to ``remote`` here — the legacy geoserver path is wired
    elsewhere (``Process._store_results_if_needed`` in the old Flask code) and
    is not part of this composition.
  * The backend (filesystem vs. Kubernetes) is selected once, here, precisely
    because this is the first point in the codebase where both concrete
    backends exist side by side
  * Exactly one ``ServiceRegistry`` is built per process. Its ``asyncio.Lock``
    only serialises concurrent collection registrations correctly if every
    caller shares this single instance — constructing a second one anywhere
    would silently reintroduce the lost-update race the lock exists to
    prevent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple, Optional

from ump.adapters.result_storage.entity_config_backend import EntityConfigBackendPort
from ump.adapters.result_storage.entity_config_fs import FilesystemEntityConfigBackend
from ump.adapters.result_storage.ldproxy_result_storage import LdproxyResultStorage
from ump.adapters.result_storage.service_registry import ServiceRegistry
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.result_storage import NullResultStorage, ResultStoragePort
from ump.core.settings import UmpSettings

logger = logging.getLogger(__name__)


class ConfirmBudget(NamedTuple):
    """How long to keep re-checking that a stored collection went live."""

    max_attempts: int
    base_wait: float
    max_wait: float


def _require(value: Optional[str], setting_name: str) -> str:
    """Narrow an Optional setting to str, raising a clear error if unset.

    The settings type is ``str | None`` because the field is optional *unless*
    ldproxy is in use — a constraint the type system cannot express on its
    own. ``_validate_resultstore_settings`` in ``ump.asgi`` already checks this
    before these factories run, so in the normal startup path this can never
    fire. It exists here anyway as defence in depth: these are pure functions
    that anyone could call directly (as the composition tests do), and a
    silent ``None`` flowing into an adapter constructor would fail with a
    confusing error far from its cause.
    """
    if not value:
        raise ValueError(
            f"{setting_name} must be set when result-storage: ldproxy is in use."
        )
    return value


def resolve_gpkg_path(settings: UmpSettings) -> str:
    """Directory the per-job GeoPackages (+ manifests) are written to.

    Defaults to ``{ROOTPATH}/resources/features`` because that is where
    ldproxy resolves a provider's ``database: {job}.gpkg`` from.  Operators can
    override it, but it must stay inside the store root the ldproxy instance
    mounts, or ldproxy cannot open the data.
    """
    if settings.UMP_RESULTSTORE_GPKG_PATH:
        return settings.UMP_RESULTSTORE_GPKG_PATH
    root = _require(
        settings.UMP_RESULTSTORE_LDPROXY_ROOTPATH, "UMP_RESULTSTORE_LDPROXY_ROOTPATH"
    )
    return str(Path(root) / "resources" / "features")


def resolve_entities_path(settings: UmpSettings) -> str:
    """Directory the ldproxy entity YAMLs are written to (filesystem backend).

    ``providers/`` and ``services/`` are created underneath it.  Defaults to
    ``{ROOTPATH}/entities/instances``, the layout ldproxy scans.  Irrelevant
    under the ``k8s`` backend, where entities are ConfigMaps.
    """
    if settings.UMP_RESULTSTORE_ENTITIES_PATH:
        return settings.UMP_RESULTSTORE_ENTITIES_PATH
    root = _require(
        settings.UMP_RESULTSTORE_LDPROXY_ROOTPATH, "UMP_RESULTSTORE_LDPROXY_ROOTPATH"
    )
    return str(Path(root) / "entities" / "instances")


def ldproxy_required(providers: ProvidersPort) -> bool:
    """Return True if any configured process needs the ldproxy result store.

    The single source of truth for "is the ldproxy feature in use at all" —
    both the startup settings validation and the storage-port factory below
    must agree on this, so it lives in one place rather than being
    re-derived twice.
    """
    return any(
        process.result_storage == "ldproxy"
        for provider in providers.get_providers()
        for process in provider.processes
    )


def build_entity_config_backend(settings: UmpSettings) -> EntityConfigBackendPort:
    """Select and construct the entity-config backend named by settings.

    Raises ``ValueError`` for an unrecognised backend name — this is a
    deterministic configuration error and must surface immediately, not at
    first use.  (Missing required settings for a given backend are already
    caught earlier by the startup settings validation; this function assumes
    that check has passed.)
    """
    backend_name = settings.UMP_RESULTSTORE_CONFIG_BACKEND

    if backend_name == "filesystem":
        return FilesystemEntityConfigBackend(resolve_entities_path(settings))

    if backend_name == "k8s":
        # Imported lazily inside the module itself (see entity_config_k8s
        # docstring) so the `kubernetes` package stays an optional dependency;
        # importing the module here is safe either way.
        from ump.adapters.result_storage.entity_config_k8s import (
            K8sConfigMapEntityConfigBackend,
        )

        namespace = _require(
            settings.UMP_RESULTSTORE_K8S_NAMESPACE, "UMP_RESULTSTORE_K8S_NAMESPACE"
        )
        return K8sConfigMapEntityConfigBackend(
            namespace=namespace,
            service_configmap=settings.UMP_RESULTSTORE_K8S_SERVICE_CONFIGMAP,
            provider_configmap=settings.UMP_RESULTSTORE_K8S_PROVIDER_CONFIGMAP,
        )

    raise ValueError(
        f"Unknown UMP_RESULTSTORE_CONFIG_BACKEND={backend_name!r}; "
        "expected 'filesystem' or 'k8s'."
    )


# How long ``LdproxyResultStorage`` keeps re-touching the service entity until
# it can confirm a collection is live.  This is the one place where the two
# backends differ *operationally* rather than structurally, so the composition
# root — which is the only layer that knows which backend was selected — sets
# it; the adapter itself stays backend-agnostic and just consumes the budget.
#
# filesystem: ldproxy's store watcher reloads within seconds  -> ~25 s is ample.
# k8s:        the kubelet only syncs mounted ConfigMaps about once a minute, so
#             the entity does not even reach ldproxy's disk for up to ~60 s;
#             the budget must cover that plus the reload, or every job would be
#             failed by V-11's gate while its collection is merely still in
#             transit.
_CONFIRM_BUDGETS = {
    "filesystem": ConfirmBudget(max_attempts=6, base_wait=1.5, max_wait=8.0),
    "k8s": ConfirmBudget(max_attempts=12, base_wait=2.0, max_wait=30.0),
}


def resolve_confirm_budget(settings: UmpSettings) -> ConfirmBudget:
    """Publication-confirmation budget appropriate for the selected backend."""
    return _CONFIRM_BUDGETS[settings.UMP_RESULTSTORE_CONFIG_BACKEND]


def build_result_storage_port(
    settings: UmpSettings, providers: ProvidersPort
) -> tuple[ResultStoragePort, Optional[ServiceRegistry]]:
    """Build the storage port the rest of UMP should use, plus its registry.

    Returns ``(NullResultStorage(), None)`` when no process requires ldproxy —
    no backend, no registry, no GDAL/geopandas import is triggered in that
    case.  Otherwise returns the fully wired ``LdproxyResultStorage`` together
    with the single ``ServiceRegistry`` instance that owns the shared service
    entity's lock, so the caller can also use it for the startup bootstrap
    (see ``ensure_ldproxy_bootstrapped`` below) without constructing a second,
    lock-incompatible instance.
    """
    if not ldproxy_required(providers):
        return NullResultStorage(), None

    backend = build_entity_config_backend(settings)
    registry = ServiceRegistry(
        backend=backend,
        service_id=settings.UMP_RESULTSTORE_LDPROXY_SERVICE_ID,
    )
    budget = resolve_confirm_budget(settings)
    storage_port = LdproxyResultStorage(
        backend=backend,
        service_registry=registry,
        gpkg_path=resolve_gpkg_path(settings),
        base_url=_require(
            settings.UMP_RESULTSTORE_LDPROXY_BASE_URL,
            "UMP_RESULTSTORE_LDPROXY_BASE_URL",
        ),
        native_crs_epsg=settings.UMP_RESULTSTORE_LDPROXY_NATIVE_CRS,
        service_id=settings.UMP_RESULTSTORE_LDPROXY_SERVICE_ID,
        internal_url=settings.UMP_RESULTSTORE_LDPROXY_INTERNAL_URL,
        confirm_max_attempts=budget.max_attempts,
        confirm_base_wait=budget.base_wait,
        confirm_max_wait=budget.max_wait,
    )
    return storage_port, registry


async def ensure_ldproxy_bootstrapped(
    registry: Optional[ServiceRegistry],
    storage_port: Optional[ResultStoragePort] = None,
) -> None:
    """Best-effort startup bootstrap of the shared ldproxy service entity.

    Two idempotent steps, both required before ldproxy can publish any result.
    Order matters: the *default provider* is written **before** the service
    entity. ldproxy (verified on 3.6.x and 4.6.x) refuses to start an
    ``OGC_API`` service that cannot resolve its default feature provider, and
    on a cold start its file watcher processes the two new files in write
    order — so if the service appeared first it would fail to start ("No
    feature provider found") and would *not* be retried when the provider file
    arrived a moment later. Writing the provider first makes the service start
    on the first pass.

    1. The service's *default* feature provider exists
       (``LdproxyResultStorage.ensure_default_provider``) — even though every
       published collection overrides ``featureProvider`` per-collection.
    2. The shared service entity exists (``ServiceRegistry.ensure_bootstrapped``).

    Deliberately swallows failures: both steps are idempotent and get retried
    on the first successful ``register_collection`` / job store anyway, and the
    failure modes here are typically *transient reachability* problems — the
    file share not mounted yet, the Kubernetes API briefly unavailable — rather
    than configuration errors. Configuration errors (missing settings, unknown
    backend name) are caught earlier and deliberately raise instead of landing
    here, so anything reaching this except-block is an operational condition
    that can plausibly resolve itself before the first job completes.

    No-op when ``registry`` is None (no process needs ldproxy).
    """
    if registry is None:
        return

    try:
        # Write the default provider FIRST, then the service entity, so ldproxy's
        # cold-start watcher sees the provider before the service and the service
        # starts on the first pass (see docstring). Only LdproxyResultStorage
        # carries a default provider; NullResultStorage (which never comes with a
        # non-None registry) does not — guard by type so the composition stays
        # honest about optional wiring.
        if isinstance(storage_port, LdproxyResultStorage):
            await storage_port.ensure_default_provider()
        await registry.ensure_bootstrapped()
    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
        logger.warning(
            "[result_storage] could not bootstrap the shared ldproxy service "
            "entity at startup (will retry automatically on first stored "
            "result): %s",
            exc,
        )
