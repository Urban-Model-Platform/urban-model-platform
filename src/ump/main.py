# main.py
import os

import uvicorn
from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
from ump.adapters.site_info_static_adapter import StaticSiteInfoAdapter
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.logging_adapter import LoggingAdapter
from ump.adapters.web.fastapi import create_app
from ump.core.config import JobManagerConfig
from ump.core.managers.process_manager import ProcessManager
from ump.core.managers.job_manager import JobManager
from ump.core.managers.observers import StatusHistoryObserver, PollingSchedulerObserver, ResultsVerificationObserver
from ump.adapters.retry_tenacity import TenacityRetryAdapter
from ump.core.settings import set_logger, app_settings
from ump.core.logging_config import configure_logging


# main lives at the outermost layer (not in core)
# Instantiates all the concrete adapters
# Wires dependencies together
# Starts the application

def main():
    config_path = os.path.join(os.path.dirname(__file__), "../../providers.yaml")
    config_path = os.path.abspath(config_path)

    # Instantiate infrastructure adapters
    providers_port = ProviderConfigFileAdapter(config_path)
    providers_port.start_file_watcher()
    http_client = AioHttpClientAdapter()
    process_id_validator = ColonProcessId()
    # Select job repository adapter based on UMP_JOB_STORE setting
    if app_settings.UMP_JOB_STORE == "postgres":
        from ump.adapters.job_repository_sql import SQLModelJobRepository
        if not app_settings.UMP_DATABASE_URL:
            raise RuntimeError(
                "UMP_DATABASE_URL must be set when UMP_JOB_STORE=postgres"
            )
        job_repo = SQLModelJobRepository(app_settings.UMP_DATABASE_URL)
    else:
        job_repo = InMemoryJobRepository("scratch/ump_jobs")
    site_info_adapter = StaticSiteInfoAdapter()

    # Central logging configuration BEFORE injecting adapter so uvicorn adopts level/format
    configure_logging(app_settings.UMP_LOG_LEVEL)
    # Inject logging adapter (decoupled from core) after root config
    set_logger(LoggingAdapter("ump", app_settings.UMP_LOG_LEVEL))

    # Factories passed to web adapter keep composition here
    def process_manager_factory(client):
        return ProcessManager(
            providers_port,
            client,
            process_id_validator=process_id_validator,
        )

    def job_manager_factory(client, process_manager):
        # Create config from app settings
        job_config = JobManagerConfig.from_app_settings(app_settings)
        # Use config values for retry adapter
        retry_adapter = TenacityRetryAdapter(
            attempts=job_config.forward_max_retries,
            wait_initial=job_config.forward_retry_base_wait,
            wait_max=job_config.forward_retry_max_wait
        )
        
        # Create JobManager first (observers need reference to its methods)
        jm = JobManager(
            providers=providers_port,
            http_client=client,
            process_id_validator=process_id_validator,
            job_repo=job_repo,
            config=job_config,
            retry_port=retry_adapter,
            observers=[],  # Will be set after creation
        )
        
        # Create observers with callback to JobManager's _schedule_poll
        observers = [
            StatusHistoryObserver(repository=job_repo),
            PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
            ResultsVerificationObserver(http_client=client),
        ]
        
        # Wire observers into JobManager
        jm._observers = observers
        
        # Attach here (composition root) so adapters remain pure HTTP concerns.
        process_manager.attach_job_manager(jm)
        return jm

    app = create_app(
        process_manager_factory=process_manager_factory,
        http_client=http_client,
        job_manager_factory=job_manager_factory,
        job_repo=job_repo,
        site_info=site_info_adapter,
    )

    # Let uvicorn inherit existing logging (separate sinks & correlation ids)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        log_level=str(app_settings.UMP_LOG_LEVEL).lower(),
    )


if __name__ == "__main__":
    main()
