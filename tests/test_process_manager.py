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
        # Build ProviderConfig using model_validate so Pydantic parses the URL.
        # A single 'echo' process is declared so config-driven (bare-id)
        # resolution can find it without fetching the remote process list.
        return ProviderConfig.model_validate(
            {
                "name": self._provider.name,
                "url": self._provider.url,
                "processes": [{"id": "echo"}],
            }
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

    async def get_content(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> tuple[bytes, str]:
        return b"", "application/json"

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

    fetch_url = "http://provider.local/processes/echo"
    # config-driven resolution fetches the process description directly.
    # (Note: list_url would be a prefix of fetch_url and collide under the
    # fake's prefix matching, so only the fetch response is registered.)
    responses = {
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
        # bare-id resolution is config-driven: it resolves the provider from
        # providers.yaml and fetches the process description directly (no list fetch).
        assert any(fetch_url in r for r in http_client.requests)


@pytest.mark.asyncio
async def test_execute_process_delegates_to_job_manager():
    provider = FakeProvider(name="infra", url="http://provider.local/")
    providers = FakeProvidersService(provider)
    validator = ColonProcessId()

    http_client = FakeHttpClient({})

    class FakeJobManager:
        def __init__(self) -> None:
            self.calls: List[Any] = []

        async def run_execution_pipeline(
            self, process_id, payload, headers, user_id=None
        ):
            self.calls.append((process_id, payload, headers, user_id))
            return {
                "status": 202,
                "headers": {"Location": "/jobs/1"},
                "body": {"job": "1"},
            }

    async with http_client as client:
        manager = ProcessManager(
            cast(ProvidersPort, providers),
            cast(HttpClientPort, client),
            process_id_validator=cast(ProcessIdValidatorPort, validator),
        )
        fake_jm = FakeJobManager()
        manager.attach_job_manager(cast(Any, fake_jm))
        resp = await manager.execute_process(
            "infra:echo",
            payload={"x": 1},
            headers={"Prefer": "respond-async"},
        )
        assert isinstance(resp, dict)
        assert resp.get("status") == 202
        # ProcessManager delegates verbatim to JobManager, forwarding the
        # Prefer header and payload unchanged.
        assert fake_jm.calls == [
            ("infra:echo", {"x": 1}, {"Prefer": "respond-async"}, None)
        ]
