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

import os

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.jwt_auth_adapter import JwtAuthAdapter
from ump.adapters.logging_adapter import LoggingAdapter
from ump.adapters.poll_lock_noop import NoOpPollLock
from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.retry_tenacity import TenacityRetryAdapter
from ump.adapters.site_info_static_adapter import StaticSiteInfoAdapter
from ump.adapters.web.fastapi import create_app
from ump.core.config import JobManagerConfig
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.logging_config import configure_logging
from ump.core.managers.job_manager import JobManager
from ump.core.managers.observers import (
    PollingSchedulerObserver,
    ResultsVerificationObserver,
    StatusHistoryObserver,
)
from ump.core.managers.process_manager import ProcessManager
from ump.core.services.authorization import AuthorizationService
from ump.core.settings import app_settings, set_logger

# ---------------------------------------------------------------------------
# Infrastructure adapters (one set per worker process)
# ---------------------------------------------------------------------------

_config_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../providers.yaml")
)
providers_port = ProviderConfigFileAdapter(_config_path)
providers_port.start_file_watcher()

http_client = AioHttpClientAdapter()
process_id_validator = ColonProcessId()
remote_auth = RemoteAuthAdapter()
jwt_auth = JwtAuthAdapter(app_settings)

if app_settings.UMP_JOB_STORE == "postgres":
    from ump.adapters.job_repository_sql import SQLModelJobRepository
    from ump.adapters.poll_lock_pg import PgAdvisoryPollLock

    if not app_settings.UMP_DATABASE_URL:
        raise RuntimeError("UMP_DATABASE_URL must be set when UMP_JOB_STORE=postgres")
    _sql_repo = SQLModelJobRepository(app_settings.UMP_DATABASE_URL)
    job_repo: JobRepositoryPort = _sql_repo
    poll_lock = PgAdvisoryPollLock(_sql_repo._session_factory)
else:
    job_repo = InMemoryJobRepository("scratch/ump_jobs")
    poll_lock = NoOpPollLock()

configure_logging(app_settings.UMP_LOG_LEVEL)
set_logger(LoggingAdapter("ump", app_settings.UMP_LOG_LEVEL))

# ---------------------------------------------------------------------------
# App factory (called once per worker)
# ---------------------------------------------------------------------------


def _process_manager_factory(client):
    return ProcessManager(
        providers_port,
        client,
        process_id_validator=process_id_validator,
        remote_auth=remote_auth,
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
        observers=[],
    )
    jm._observers = [
        StatusHistoryObserver(repository=job_repo),
        PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
        ResultsVerificationObserver(http_client=client),
    ]
    process_manager.attach_job_manager(jm)
    return jm


app = create_app(
    process_manager_factory=_process_manager_factory,
    http_client=http_client,
    job_manager_factory=_job_manager_factory,
    job_repo=job_repo,
    process_id_validator=process_id_validator,
    auth_port=jwt_auth,
    authorization_service=AuthorizationService(providers_port),
    site_info=StaticSiteInfoAdapter(),
)
