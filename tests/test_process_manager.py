from typing import Any, Dict, List, Tuple, cast

import pytest

from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.process import Process
from ump.core.models.providers_config import ProviderConfig

class FakeProvider:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class FakeProvidersService(ProvidersPort):
    def __init__(self, provider: FakeProvider):
        self._provider = provider

    def load_providers(self) -> None:
        return None

    def get_providers(self) -> List[ProviderConfig]:
        return []

    def get_provider(self, provider_name: str) -> ProviderConfig:
        # Build ProviderConfig using model_validate so Pydantic parses the URL
        return ProviderConfig.model_validate(
            {"name": self._provider.name, "url": self._provider.url}
        )

    def get_process_config(self, provider_name: str, process_id: str):
        raise NotImplementedError

    def list_providers(self) -> List[str]:
        return [self._provider.name]

    def get_processes(self, provider_name: str) -> List[str]:
        return []

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        return True

class FakeHttpClient(HttpClientPort):
    """A tiny fake async HTTP client used by ProcessManager tests.

    - `responses` maps either url prefixes or exact urls to return values.
    - supports ('POST', url) keys for post responses.
    - records requests in `self.requests`.
    """

    def __init__(self, responses: Dict[Any, Any]):
        self._responses = responses
        self.requests: List[str] = []

    async def __aenter__(self) -> HttpClientPort:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get(self, url: str, timeout: float | None = None, headers: dict | None = None) -> Dict[str, Any]:
        self.requests.append(url)
        # allow prefix matching like the tests use (startswith)
        for k, v in self._responses.items():
            if isinstance(k, str) and url.startswith(k):
                return cast(Dict[str, Any], v)
            if k == url:
                return cast(Dict[str, Any], v)
        raise RuntimeError(f"no response registered for {url}")

    async def close(self) -> None:
        return None

    async def post(
        self,
        url: str,
        json: Dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        self.requests.append(f"POST {url}")
        key = ("POST", url)
        if key in self._responses:
            return cast(Dict[str, Any], self._responses[key])
        if url in self._responses:
            return cast(Dict[str, Any], self._responses[url])
        raise RuntimeError(f"no response registered for POST {url}")


@pytest.mark.asyncio
async def test_get_process_with_prefixed_id_from_provider():
    provider = FakeProvider(name="infra", url="http://provider.local/")
    providers = FakeProvidersService(provider)
    validator = ColonProcessId()

    proc_url = "http://provider.local/processes/echo"
    fake_proc = {
        "id": "infra:echo",
        "version": "1.0",
        "jobControlOptions": ["sync-execute"],
        "outputTransmission": ["value"],
        "inputs": {},
        "outputs": {},
        "links": [],
    }

    http_client = FakeHttpClient({proc_url: fake_proc})

    async with http_client as client:
        manager = ProcessManager(
            cast(ProvidersPort, providers),
            cast(HttpClientPort, client),
            process_id_validator=cast(ProcessIdValidatorPort, validator),
        )
        model: Process = await manager.get_process("infra:echo")
        assert model.pid == "infra:echo"


@pytest.mark.asyncio
async def test_get_process_bare_id_fallback():
    provider = FakeProvider(name="infra", url="http://provider.local/")
    providers = FakeProvidersService(provider)
    validator = ColonProcessId()

    list_url = "http://provider.local/processes"
    fetch_url = "http://provider.local/processes/echo"
    # summary contains provider-prefixed id; full description available at fetch_url
    responses = {
        list_url: {
            "processes": [
                {
                    "id": "infra:echo",
                    "version": "1.0",
                    "jobControlOptions": ["sync-execute"],
                    "outputTransmission": ["value"],
                    "links": [],
                }
            ]
        },
        fetch_url: {
            "id": "infra:echo",
            "version": "1.0",
            "jobControlOptions": ["sync-execute"],
            "outputTransmission": ["value"],
            "inputs": {},
            "outputs": {},
            "links": [],
        },
    }

    http_client = FakeHttpClient(responses)

    async with http_client as client:
        manager = ProcessManager(
            cast(ProvidersPort, providers),
            cast(HttpClientPort, client),
            process_id_validator=cast(ProcessIdValidatorPort, validator),
        )
        model = await manager.get_process("echo")
        assert model.pid == "infra:echo"
        # verify manager attempted to fetch the list and the full description
        assert any(list_url in r for r in http_client.requests)
        assert any(fetch_url in r for r in http_client.requests)


@pytest.mark.asyncio
async def test_execute_process_forwards_and_honors_prefer_header():
    provider = FakeProvider(name="infra", url="http://provider.local/")
    providers = FakeProvidersService(provider)
    validator = ColonProcessId()

    exec_url = "http://provider.local/processes/echo/execution"
    fake_response = {
        "status": 202,
        "headers": {"Location": "/jobs/1"},
        "body": {"job": "1"},
    }

    http_client = FakeHttpClient({("POST", exec_url): fake_response})

    async with http_client as client:
        manager = ProcessManager(
            cast(ProvidersPort, providers),
            cast(HttpClientPort, client),
            process_id_validator=cast(ProcessIdValidatorPort, validator),
        )
        resp = await manager.execute_process(
            "infra:echo",
            payload={"x": 1},
            headers={"Prefer": "respond-async"}
        )
        assert isinstance(resp, dict)
        assert resp.get("status") == 202
