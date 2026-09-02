"""ASGI entry point for production deployments.

Exposes the FastAPI application at module level so uvicorn and gunicorn
can import it directly, enabling multi-worker deployments:

  # uvicorn (recommended):
  uvicorn ump.asgi:app --host 0.0.0.0 --port 8000 --workers 4

  # gunicorn with uvicorn workers:
  gunicorn -k uvicorn.workers.UvicornWorker -w 4 ump.asgi:app

Each worker process imports this module independently, so every worker
gets its own adapter instances (file watchers, HTTP clients, DB connections).
"""

import asyncio
import os

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.adapters.colon_process_id_validator import ProcessIdValidator
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.jwt_auth_adapter import JwtAuthAdapter
from ump.adapters.logging_adapter import LoggingAdapter
from ump.adapters.periodic_task_runner import PeriodicTaskRunner
from ump.adapters.poll_lock_noop import NoOpPollLock
from ump.adapters.process_description_proxy import PolicyBasedProcessDescriptionProxy
from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.result_storage.inmemory_value_cache import InMemoryResultValueCache
from ump.adapters.retry_tenacity import TenacityRetryAdapter
from ump.adapters.site_info_static_adapter import StaticSiteInfoAdapter
from ump.adapters.web.fastapi import create_app
from ump.composition.result_storage import (
    build_result_storage_port,
    ensure_ldproxy_bootstrapped,
    ldproxy_required,
)
from ump.core.config import JobManagerConfig
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.result_value_cache import (
    NullResultValueCache,
    ResultValueCachePort,
)
from ump.core.logging_config import configure_logging
from ump.core.managers.job_manager import JobManager
from ump.core.managers.observers import (
    PollingSchedulerObserver,
    ResultStorageObserver,
    ResultsVerificationObserver,
    StatusHistoryObserver,
)
from ump.core.managers.process_manager import ProcessManager
from ump.core.services.authorization import AuthorizationService
from ump.core.services.job_cleanup_service import JobCleanupService
from ump.core.services.result_storage_coordinator import ResultStorageCoordinator
from ump.core.settings import app_settings, set_logger


def _validate_resultstore_settings() -> None:
    """Fail fast if any configured process needs the ldproxy result store but
    the required environment variables are missing.

    This check runs at startup, after providers have been loaded, so the error
    message names the offending process rather than showing a generic config
    complaint later at job-completion time.

    This only validates deterministic configuration — missing settings, an
    unrecognised backend name.  It deliberately does NOT try to reach the
    store itself; that reachability check is a separate, non-fatal startup
    step (see ``ensure_ldproxy_bootstrapped`` below and its call site).
    """
    if not ldproxy_required(providers_port):
        return  # nothing to validate

    missing: list[str] = []
    if not app_settings.UMP_RESULTSTORE_LDPROXY_BASE_URL:
        missing.append("UMP_RESULTSTORE_LDPROXY_BASE_URL")
    if not app_settings.UMP_RESULTSTORE_LDPROXY_ROOTPATH and not (
        app_settings.UMP_RESULTSTORE_GPKG_PATH
        and app_settings.UMP_RESULTSTORE_ENTITIES_PATH
    ):
        # ROOTPATH is the shared-volume mount point both store directories are
        # derived from; it is only dispensable if both are set explicitly.
        missing.append("UMP_RESULTSTORE_LDPROXY_ROOTPATH")
    if app_settings.UMP_RESULTSTORE_CONFIG_BACKEND == "k8s":
        if not app_settings.UMP_RESULTSTORE_K8S_NAMESPACE:
            missing.append("UMP_RESULTSTORE_K8S_NAMESPACE")

    if missing:
        raise RuntimeError(
            "One or more processes use result-storage: ldproxy but the following "
            f"required settings are not configured: {', '.join(missing)}.  "
            "Set these environment variables before starting UMP."
        )


# ---------------------------------------------------------------------------
# Logging must be configured first — before any adapter is instantiated.
#
# Every adapter that logs at startup (e.g. ProviderConfigFileAdapter calling
# load_providers() in __init__) goes through the DelegatingLogger from
# settings.py, which starts as NoOpLogger.  Calling configure_logging() and
# set_logger() here wires the delegate to a real LoggingAdapter backed by
# Python's logging module, so startup logs are visible from the very first
# line of adapter code.
# ---------------------------------------------------------------------------
configure_logging(app_settings.UMP_LOG_LEVEL)
set_logger(LoggingAdapter("ump", app_settings.UMP_LOG_LEVEL))


# ---------------------------------------------------------------------------
# Factories for core managers (one set per worker process)
# ---------------------------------------------------------------------------
def construct_database_url() -> str:
    if app_settings.UMP_DATABASE_URL:
        return app_settings.UMP_DATABASE_URL

    if not (app_settings.UMP_DATABASE_HOST and app_settings.UMP_DATABASE_NAME):
        raise RuntimeError(
            "UMP_DATABASE_HOST and UMP_DATABASE_NAME "
            "must be set if UMP_DATABASE_URL is not provided."
        )

    user: str = app_settings.UMP_DATABASE_USER
    password: str = (
        app_settings.UMP_DATABASE_PASSWORD.get_secret_value()
        if app_settings.UMP_DATABASE_PASSWORD
        else ""
    )
    host: str = app_settings.UMP_DATABASE_HOST
    port: int = app_settings.UMP_DATABASE_PORT
    name: str = app_settings.UMP_DATABASE_NAME

    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _process_manager_factory(client):
    return ProcessManager(
        providers_port,
        client,
        process_id_validator=process_id_validator,
        remote_auth=remote_auth,
        process_description_proxy=PolicyBasedProcessDescriptionProxy(),
    )


def _job_manager_factory(client, process_manager):
    job_config = JobManagerConfig.from_app_settings(app_settings)
    retry_adapter = TenacityRetryAdapter(
        attempts=job_config.forward_max_retries,
        wait_initial=job_config.forward_retry_base_wait,
        wait_max=job_config.forward_retry_max_wait,
    )
    jm = JobManager(
        providers=providers_port,
        http_client=client,
        process_id_validator=process_id_validator,
        job_repo=job_repo,
        config=job_config,
        retry_port=retry_adapter,
        remote_auth=remote_auth,
        poll_lock=poll_lock,
        result_storage_port=result_storage_port,
        value_cache=result_value_cache,
        observers=[],
    )
    jm._observers = [
        StatusHistoryObserver(repository=job_repo),
        PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
        ResultsVerificationObserver(http_client=client),
    ]
    # Only wire the storage-completion trigger when a real store is active —
    # NullResultStorage never needs a job's completion, so a plain
    # pass-through deployment does not pay for an extra no-op observer call
    # on every job.
    if result_storage_registry is not None:
        jm._observers.append(
            ResultStorageObserver(
                coordinator=result_storage_coordinator,
                providers=providers_port,
                repository=job_repo,
            )
        )
        # Best-effort startup bootstrap of the shared service entity (V-8).
        # `_job_manager_factory` runs once per worker inside the async
        # lifespan context (see fastapi.py), so this is a safe place to
        # schedule it as a background task: it must never block or fail
        # app startup (see ensure_ldproxy_bootstrapped docstring), only
        # shorten the delay before the first stored result becomes visible.
        asyncio.create_task(
            ensure_ldproxy_bootstrapped(result_storage_registry, result_storage_port)
        )
    process_manager.attach_job_manager(jm)
    return jm


# ---------------------------------------------------------------------------
# Infrastructure adapters (one set per worker process)
# ---------------------------------------------------------------------------

_config_path = os.path.abspath(app_settings.UMP_PROVIDERS_FILE)
providers_port = ProviderConfigFileAdapter(_config_path)
providers_port.start_file_watcher()

# Fail fast if result-store settings are incomplete — better to crash at startup
# with a clear error than to fail silently at job-completion time.
_validate_resultstore_settings()

http_client = AioHttpClientAdapter()
process_id_validator = ProcessIdValidator(app_settings.UMP_PROCESS_ID_SEPARATOR)
remote_auth = RemoteAuthAdapter()
jwt_auth = JwtAuthAdapter(app_settings)

# Feature V: result storage.  `result_storage_registry` is None when no
# process is configured with result-storage: ldproxy — in that case
# `result_storage_port` is a NullResultStorage and no ldproxy dependency
# (backend, registry, GDAL/geopandas) is ever constructed or imported.
result_storage_port, result_storage_registry = build_result_storage_port(
    app_settings, providers_port
)

# Result value cache: written by the coordinator when a job completes, read by
# the JobManager on /results. Both must share the SAME instance — two separate
# instances would be a cache that never hits. A TTL of 0 disables caching, in
# which case the null adapter keeps the code path free of conditionals.
result_value_cache: ResultValueCachePort = (
    InMemoryResultValueCache(
        ttl_seconds=app_settings.UMP_RESULTCACHE_TTL_SECONDS,
        max_item_bytes=app_settings.UMP_RESULTCACHE_MAX_ITEM_BYTES,
    )
    if app_settings.UMP_RESULTCACHE_TTL_SECONDS > 0
    else NullResultValueCache()
)

result_storage_coordinator = ResultStorageCoordinator(
    storage_port=result_storage_port,
    http_client=http_client,
    providers=providers_port,
    remote_auth=remote_auth,
    value_cache=result_value_cache,
    # Storage-fetch retry budget and per-attempt timeout are internal tuning
    # values with sensible defaults defined in ResultStorageCoordinator; they
    # are intentionally not exposed as operator settings.
)

if app_settings.UMP_JOB_STORE == "postgres":
    from ump.adapters.job_repository_sql import SQLModelJobRepository
    from ump.adapters.poll_lock_pg import PgAdvisoryPollLock

    database_url = construct_database_url()

    _sql_repo = SQLModelJobRepository(database_url)
    job_repo: JobRepositoryPort = _sql_repo
    poll_lock = PgAdvisoryPollLock(_sql_repo._session_factory)
else:
    job_repo = InMemoryJobRepository("scratch/ump_jobs")
    poll_lock = NoOpPollLock()

# Feature V-9: periodic removal of expired jobs (+ their stored results, if
# any). Generic — runs regardless of whether a result store is configured;
# `result_storage_port` is a harmless NullResultStorage in that case. The
# cleanup cycle itself decides nothing about *which* jobs to fetch beyond the
# two independent retention settings (anonymous vs. authenticated); see
# JobCleanupService for the rationale behind treating them separately.
job_cleanup_service = JobCleanupService(
    job_repo=job_repo,
    result_storage=result_storage_port,
    anonymous_retention_minutes=app_settings.UMP_JOB_DELETE_INTERVAL,
    authenticated_retention_minutes=app_settings.UMP_JOB_DELETE_INTERVAL_AUTHENTICATED,
)
job_cleanup_runner = PeriodicTaskRunner(
    task=job_cleanup_service.run_once,
    # The interval between *runs* reuses the anonymous retention window as a
    # sensible cadence (checking more often than the shortest retention rule
    # would ever expire anything is pointless); jobs are only ever removed
    # once they are actually past their own cutoff, this only controls polling
    # frequency, not correctness.
    interval_seconds=app_settings.UMP_JOB_DELETE_INTERVAL * 60,
    name="job-cleanup",
)

# ---------------------------------------------------------------------------
# App factory (called once per worker)
# ---------------------------------------------------------------------------


app = create_app(
    process_manager_factory=_process_manager_factory,
    http_client=http_client,
    job_manager_factory=_job_manager_factory,
    job_repo=job_repo,
    process_id_validator=process_id_validator,
    auth_port=jwt_auth,
    authorization_service=AuthorizationService(providers_port, process_id_validator),
    site_info=StaticSiteInfoAdapter(),
    background_runners=[job_cleanup_runner],
)
