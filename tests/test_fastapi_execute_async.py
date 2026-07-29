from typing import Any, Dict, List, Tuple, cast

import pytest
from fastapi.testclient import TestClient

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.web.fastapi import create_app
from ump.core.config import JobManagerConfig
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.job_manager import JobManager
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.providers_config import ProviderConfig

# Integration tests for FastAPI adapter routes + ProcessManager/JobManager wiring.
# Updated to reflect factory-based composition (create_app expects factories),
# statusInfo extraction requiring jobID/status/type and local jobID rewriting.


class FakeProvider:
    """Minimal provider configuration object used by fake ProvidersPort."""

    test = False

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class FakeProvidersService(ProvidersPort):
    """Fake ProvidersPort exposing a single provider for tests."""

    test = False

    def __init__(self, provider: FakeProvider):
        self._provider = provider

    def load_providers(self) -> None:
        return None

    def get_providers(self) -> List[ProviderConfig]:
        return []

    def get_provider(self, provider_name: str) -> ProviderConfig:
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


class FakeProcessIdValidator(ProcessIdValidatorPort):
    """Colon-prefixed process id validator used by tests."""

    test = False

    def validate(self, process_id_with_prefix: str) -> bool:
        return ":" in process_id_with_prefix

    def extract(self, process_id_with_prefix: str) -> Tuple[str, str]:
        if ":" in process_id_with_prefix:
            provider, pid = process_id_with_prefix.split(":", 1)
            return provider, pid
        raise ValueError("no prefix")

    def create(self, provider_prefix: str, process_id: str) -> str:
        return f"{provider_prefix}:{process_id}"


class FakeHttpClient(HttpClientPort):
    """Single-response fake HTTP client for execution POST tests."""

    test = False

    def __init__(self, post_response: Dict[str, Any]):
        self._post_response = post_response

    async def __aenter__(self) -> HttpClientPort:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> Dict[str, Any]:
        raise RuntimeError("unexpected GET in this test")

    async def get_content(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> tuple[bytes, str]:
        raise RuntimeError("unexpected get_content in this test")

    async def post(
        self,
        url: str,
        json: Dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        return self._post_response

    async def close(self) -> None:
        return None


class MultiFakeHttpClient(HttpClientPort):
    """Mapping-based fake HTTP client supporting GET & POST for integration tests."""

    test = False

    def __init__(
        self,
        get_responses: Dict[str, Any] | None = None,
        post_responses: Dict[Tuple[str, str], Any] | None = None,
    ):
        self._get = get_responses or {}
        self._post = post_responses or {}
        self.requests: List[str] = []

    async def __aenter__(self) -> HttpClientPort:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> Dict[str, Any]:
        if url in self._get:
            return cast(Dict[str, Any], self._get[url])
        raise RuntimeError(f"no GET response for {url}")

    async def get_content(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> tuple[bytes, str]:
        return b"", "application/json"

    async def post(
        self,
        url: str,
        json: Dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        self.requests.append(f"POST {url}")
        key = ("POST", url)
        if key in self._post:
            resp = self._post[key]
            if isinstance(resp, Exception):
                raise resp
            return cast(Dict[str, Any], resp)
        raise RuntimeError(f"no POST response for {url}")

    async def close(self) -> None:
        return None


def make_app_with_factories(
    http_client: HttpClientPort, provider: FakeProvider | None = None
):
    """Compose FastAPI app using real ProcessManager & JobManager factories.

    Mirrors production composition root minimally; disables retry logic for simplicity.
    """
    provider = provider or FakeProvider(name="infra", url="http://provider.local/")
    providers_service = FakeProvidersService(provider)
    validator = FakeProcessIdValidator()
    job_repo = InMemoryJobRepository()

    # Test-friendly config: fast polling
    test_config = JobManagerConfig(
        poll_interval=0.01,
        rewrite_remote_links=True,
        inline_inputs_size_limit=64 * 1024,
        verify_remote_results=False,
    )

    def process_manager_factory(client: HttpClientPort):
        return ProcessManager(providers_service, client, process_id_validator=validator)

    def job_manager_factory(client: HttpClientPort, process_manager: ProcessManager):
        from ump.core.managers.observers import (
            PollingSchedulerObserver,
            ResultsVerificationObserver,
            StatusHistoryObserver,
        )

        # Create JobManager first
        jm = JobManager(
            providers=providers_service,
            http_client=client,
            process_id_validator=validator,
            job_repo=job_repo,
            config=test_config,
            observers=[],
        )

        # Create and wire observers
        observers = [
            StatusHistoryObserver(repository=job_repo),
            PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
            ResultsVerificationObserver(http_client=client),
        ]
        jm._observers = observers

        process_manager.attach_job_manager(jm)
        return jm

    return create_app(
        process_manager_factory=process_manager_factory,
        http_client=http_client,
        job_manager_factory=job_manager_factory,
        job_repo=job_repo,
        site_info=None,
    )


def test_forward_valid_statusinfo():
    """Provider returns statusInfo body + Location header.

    Design: Endpoint always returns initial accepted snapshot (local job creation)
    regardless of remote status; derived remote status becomes visible via
    subsequent GET /jobs/{id}. Here remote reported 'accepted' already so both
    initial and persisted statuses are accepted.
    """
    status_info = {"jobID": "remote-job-1", "status": "accepted", "type": "process"}
    provider_resp = {
        "status": 201,
        "headers": {"Location": "http://provider.local/jobs/remote-job-1"},
        "body": status_info,
    }
    http_client = FakeHttpClient(provider_resp)
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body.get("status") == "accepted"
        assert body.get("jobID") and body["jobID"] != "remote-job-1"
        assert "Location" in r.headers


def test_location_followup_fetches_statusinfo():
    """Provider returns only Location header; server follows to obtain remote statusInfo.

    Response body: initial accepted snapshot (contract). After follow-up GET,
    stored job status becomes 'running'. Test asserts both phases.
    """
    post_resp = {
        "status": 201,
        "headers": {"Location": "http://provider.local/jobs/remote-job-1"},
        "body": None,
    }
    job_status = {"jobID": "remote-job-1", "status": "running", "type": "process"}
    http_client = MultiFakeHttpClient(
        get_responses={"http://provider.local/jobs/remote-job-1": job_status},
        post_responses={
            ("POST", "http://provider.local/processes/echo/execution"): post_resp
        },
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        body = r.json()
        # initial response always accepted snapshot
        assert body.get("status") == "accepted"
        assert body.get("jobID") and body["jobID"] != "remote-job-1"
        assert "Location" in r.headers
        # follow-up fetch reveals remote-derived running status
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "running"


def test_no_statusinfo_no_location_returns_failed():
    """Provider returns non-statusInfo body and no Location header.

    Initial response: accepted snapshot (contract). Persisted status becomes failed.
    """
    post_resp = {"status": 201, "headers": {}, "body": "not-a-status"}
    http_client = MultiFakeHttpClient(
        get_responses={},
        post_responses={
            ("POST", "http://provider.local/processes/echo/execution"): post_resp
        },
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body.get("status") == "accepted"
        assert "Location" in r.headers
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "failed"


def test_remote_provider_error_or_timeout():
    """Upstream POST raises exception; server still creates local job.

    Initial response: accepted snapshot; persisted status becomes failed.
    """
    http_client = MultiFakeHttpClient(
        post_responses={
            ("POST", "http://provider.local/processes/echo/execution"): RuntimeError(
                "timeout"
            )
        }
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        assert r.json().get("status") == "accepted"
        assert "Location" in r.headers
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "failed"


def test_always_create_local_job():
    """Provider returns successful terminal statusInfo.

    Initial response: accepted snapshot; persisted status becomes successful (after immediate verification).
    """
    post_resp = {
        "status": 200,
        "headers": {},
        "body": {"jobID": "remote-job-2", "status": "successful", "type": "process"},
    }
    http_client = MultiFakeHttpClient(
        post_responses={
            ("POST", "http://provider.local/processes/echo/execution"): post_resp
        }
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        assert r.json().get("status") == "accepted"
        assert "Location" in r.headers
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "successful"


def test_relative_location_header_resolution():
    """Provider returns relative Location; server resolves, polls and rewrites jobID.

    Initial response: accepted snapshot; persisted status becomes running.
    """
    post_resp = {"status": 202, "headers": {"Location": "/jobs/rel-1"}, "body": None}
    job_status = {"jobID": "rel-1", "status": "running", "type": "process"}
    http_client = MultiFakeHttpClient(
        get_responses={"http://provider.local/jobs/rel-1": job_status},
        post_responses={
            ("POST", "http://provider.local/processes/echo/execution"): post_resp
        },
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"inputs": {}},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body.get("status") == "accepted"
        assert body.get("jobID") and body["jobID"] != "rel-1"
        assert "Location" in r.headers
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "running"


def test_execute_endpoint_forwards_201():
    """Provider returns statusInfo with relative Location.

    Initial response: accepted snapshot; persisted status becomes accepted (remote also accepted).
    """
    exec_url = "http://provider.local/processes/echo/execution"
    fake_response = {
        "status": 201,
        "headers": {"Location": "/jobs/1"},
        "body": {"jobID": "1", "status": "accepted", "type": "process"},
    }
    http_client = MultiFakeHttpClient(
        post_responses={("POST", exec_url): fake_response}
    )
    app = make_app_with_factories(http_client)
    with TestClient(app) as client:
        r = client.post(
            "/v1.0/processes/infra:echo/execution",
            json={"x": 1},
            headers={"Prefer": "respond-async"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body.get("status") == "accepted"  # initial snapshot
        assert body.get("jobID") and body["jobID"] != "1"
        job_id = r.headers["Location"].split("/")[-1]
        jr = client.get(f"/v1.0/jobs/{job_id}")
        assert jr.status_code == 200
        assert jr.json().get("status") == "accepted"
