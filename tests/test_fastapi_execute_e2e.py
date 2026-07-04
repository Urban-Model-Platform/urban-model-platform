import asyncio
from typing import Any, Dict

import pytest
from aioresponses import aioresponses
from fastapi.testclient import TestClient

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.web.fastapi import create_app
from ump.core.config import JobManagerConfig
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.job_manager import JobManager
from ump.core.managers.observers import (
    PollingSchedulerObserver,
    ResultsVerificationObserver,
    StatusHistoryObserver,
)
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.providers_config import ProviderConfig


class SimpleProvider:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class SimpleProviders(ProvidersPort):
    def __init__(self, provider: SimpleProvider):
        self._provider = provider

    def load_providers(self) -> None:
        return None

    def get_providers(self):
        return []

    def get_provider(self, provider_name: str) -> ProviderConfig:
        return ProviderConfig.model_validate(
            {"name": self._provider.name, "url": self._provider.url}
        )

    def get_process_config(self, provider_name: str, process_id: str):
        raise NotImplementedError

    def list_providers(self):
        return [self._provider.name]

    def get_processes(self, provider_name: str):
        return []

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        return True


class SimpleValidator(ProcessIdValidatorPort):
    def validate(self, process_id_with_prefix: str) -> bool:
        return ":" in process_id_with_prefix

    def extract(self, process_id_with_prefix: str):
        if ":" in process_id_with_prefix:
            return process_id_with_prefix.split(":", 1)
        raise ValueError("no prefix")

    def create(self, provider_prefix: str, process_id: str) -> str:
        return f"{provider_prefix}:{process_id}"


@pytest.mark.asyncio
async def test_e2e_forward_statusinfo_with_real_adapter():
    # this is an e2e test that test the routes, uses the real AioHttpClientAdapter
    # and ProcessManager to
    # forward a process execution request to a mocked provider endpoint
    # and returns the provider's statusInfo response as-is to the client
    provider = SimpleProvider(name="infra", url="http://provider.local/")
    providers = SimpleProviders(provider)
    validator = SimpleValidator()
    job_repo = InMemoryJobRepository()

    test_config = JobManagerConfig(
        poll_interval=0.01,
        poll_timeout=1,
        rewrite_remote_links=True,
        inline_inputs_size_limit=64 * 1024,
        verify_remote_results=False,
    )

    def process_manager_factory(client: HttpClientPort):
        return ProcessManager(providers, client, process_id_validator=validator)

    def job_manager_factory(client: HttpClientPort, process_manager: ProcessManager):
        jm = JobManager(
            providers=providers,
            http_client=client,
            process_id_validator=validator,
            job_repo=job_repo,
            config=test_config,
            observers=[],
        )
        observers = [
            StatusHistoryObserver(repository=job_repo),
            PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
            ResultsVerificationObserver(http_client=client),
        ]
        jm._observers = observers
        process_manager.attach_job_manager(jm)
        return jm

    # provider will reply to POST execution with a JSON statusInfo
    post_url = "http://provider.local/processes/echo/execution"
    status_info = {"status": "accepted", "id": "remote-job-1", "type": "process"}

    # patch aiohttp requests via aioresponses
    with aioresponses() as m:  # aioresponses fakes remote server responses
        m.post(
            post_url,
            payload=status_info,
            status=201,
            headers={"Location": "http://provider.local/jobs/remote-job-1"},
        )

        adapter = AioHttpClientAdapter()
        app = create_app(
            process_manager_factory=process_manager_factory,
            http_client=adapter,
            job_manager_factory=job_manager_factory,
            job_repo=job_repo,
        )
        # Run the test via TestClient; lifespan should open the adapter via async context
        with TestClient(app) as client:
            r = client.post(
                "/processes/infra:echo/execution",
                json={"inputs": {}},
                headers={"Prefer": "respond-async"},
            )
            # TDD: expect 201 and forwarded statusInfo with at least the core fields
            assert r.status_code == 201
            body = r.json()
            assert body.get("status") == status_info["status"]
            assert body.get("type") == status_info["type"]
            assert "Location" in r.headers
