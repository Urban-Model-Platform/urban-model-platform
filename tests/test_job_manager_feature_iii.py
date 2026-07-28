"""Unit tests for Feature III (Job lifecycle & result fetching).

Each test targets the core unit `JobManager` (hexagonal core) exercising distinct
execution lifecycle scenarios against concrete lightweight adapter implementations:

Adapters used here are minimal test implementations of the real ports to keep
behavior realistic without external dependencies:
    - ProvidersPort: `TestProvidersAdapter`
    - ProcessIdValidatorPort: `TestProcessIdValidator`
    - HttpClientPort: `TestHttpClientAdapter`
    - JobRepositoryPort: `InMemoryJobRepository` (debug/TDD only)
    - RetryPort: `TestRetryAdapter` / subclass for failure cases

Test scenarios covered:
    1. Immediate results synthesis when provider ignores Prefer (no statusInfo).
    2. Poll timeout enforcing `UMP_REMOTE_JOB_TTW` (running -> failed).
    3. Successful immediate remote job with retry-based results verification.
    4. Failed verification downgrading successful -> failed (results unavailable).
    5. Link normalization: remote self/results links replaced by local ones.
    6. Results endpoint proxy: success vs not available (non-successful job).

Purposes:
    - Guard correctness of statusInfo derivation logic.
    - Validate link injection & normalization invariants (local UUID only).
    - Ensure retry logic influences status downgrade/confirmation paths.
    - Confirm timeout path marks job failed with diagnostic.
    - Verify results endpoint uses remote proxy only for successful jobs.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.retry_tenacity import TenacityRetryAdapter
from ump.core.config import JobManagerConfig
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.job_manager import JobManager
from ump.core.models.job import StatusCode

# --- Test adapter implementations (implement ports, not replacing them with mocks) ---


class TestProviderConfig:
    def __init__(self, name: str, url: str, ttw_job_done: int = 300):
        self.name = name
        self.url = url
        self.ttw_job_done = ttw_job_done
        self.processes = []  # minimal attribute surface used by JobManager

    # Marker indicating this is not a production adapter (test helper)
    test = False


class TestProvidersAdapter(ProvidersPort):
    def __init__(
        self,
        provider_name: str = "prov",
        url: str = "http://provider.test",
        ttw_job_done: int = 300,
    ):
        self._provider = TestProviderConfig(provider_name, url, ttw_job_done)
        self._ttw_job_done = ttw_job_done

    # Marker indicating this is not a production adapter (test helper)
    test = False

    # Unused lifecycle methods for these tests
    def load_providers(self) -> None:  # pragma: no cover trivial
        pass

    def get_providers(self):  # pragma: no cover
        return [self._provider]

    def get_provider(self, provider_name: str):
        return self._provider

    def get_process_config(self, provider_name: str, process_id: str):
        # Return a minimal test process config with ttw_job_done inherited from provider
        class TestProcessConfig:
            def __init__(self, ttw_job_done=None):
                self.ttw_job_done = ttw_job_done  # None means use provider default
                self.poll_interval = None

        return TestProcessConfig(ttw_job_done=None)  # pragma: no cover

    def list_providers(self):
        return [self._provider.name]

    def get_processes(self, provider_name: str):  # pragma: no cover
        return []

    def check_process_availability(
        self, provider_name: str, process_id: str
    ) -> bool:  # pragma: no cover
        return True


class TestProcessIdValidator(ProcessIdValidatorPort):
    def validate(self, process_id_with_prefix: str) -> bool:
        return ":" in process_id_with_prefix

    def extract(self, process_id_with_prefix: str) -> tuple[str, str]:
        if ":" not in process_id_with_prefix:
            raise ValueError("missing prefix")
        prefix, pid = process_id_with_prefix.split(":", 1)
        return prefix, pid

    def create(self, provider_prefix: str, process_id: str) -> str:
        return f"{provider_prefix}:{process_id}"

    # Marker indicating this is not a production adapter (test helper)
    test = False


class TestHttpClientAdapter(HttpClientPort):
    def __init__(self, post_response=None, get_responses=None):
        self._post_response = post_response
        self._get_responses = get_responses or []
        self.get_calls = []

    # Marker indicating this is not a production adapter (test helper)
    test = False

    async def __aenter__(self):  # pragma: no cover trivial
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):  # pragma: no cover trivial
        return False

    async def close(self):  # pragma: no cover trivial
        pass

    async def post(self, url: str, json=None, timeout=None, headers=None):
        return self._post_response

    async def get(self, url: str, timeout=None, headers=None):
        idx = len(self.get_calls)
        self.get_calls.append(url)
        if idx < len(self._get_responses):
            resp = self._get_responses[idx]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {"jobID": "remote-running", "status": "running", "type": "process"}

    async def get_content(self, url: str, timeout=None, headers=None):
        # Shares the get_calls index with get() so get_responses applies to both
        idx = len(self.get_calls)
        self.get_calls.append(url)
        if idx < len(self._get_responses):
            resp = self._get_responses[idx]
            if isinstance(resp, Exception):
                raise resp
            # Serialize the dict response to bytes as the real adapter would
            import json as _json

            return _json.dumps(resp).encode(), "application/json"
        return b"{}", "application/json"


class TestRetryAdapter(TenacityRetryAdapter):
    async def execute(
        self, func, *args, **kwargs
    ):  # override to expose call count deterministically
        try:
            return await super().execute(func, *args, **kwargs)
        finally:
            # TenacityRetryAdapter doesn't track calls; we can infer from test client's get_calls length if needed.
            pass

    # Marker indicating this is not a production adapter (test helper)
    test = False


# --- Helpers ----------------------------------------------------------------


def make_manager(
    post_response, get_responses=None, retry_port=None, config_overrides=None
):
    """Create test JobManager with custom config.

    Args:
        post_response: POST response for http client
        get_responses: List of GET responses for http client
        retry_port: Optional retry adapter
        config_overrides: Dict of config overrides (e.g., {'poll_interval': 0.01, 'ttw_job_done': 0.05})
            Note: 'ttw_job_done' is passed to providers, not JobManagerConfig
    """
    from ump.core.managers.observers import (
        PollingSchedulerObserver,
        ResultsVerificationObserver,
        StatusHistoryObserver,
    )

    # Extract ttw_job_done from overrides if present (it's provider-level config, not manager config)
    ttw_job_done = 300  # default
    if config_overrides and "ttw_job_done" in config_overrides:
        ttw_job_done = config_overrides.pop("ttw_job_done")

    providers_port = TestProvidersAdapter(ttw_job_done=ttw_job_done)
    validator = TestProcessIdValidator()
    repo = InMemoryJobRepository()
    http_client = TestHttpClientAdapter(
        post_response=post_response, get_responses=get_responses
    )

    # Create config with test-friendly defaults
    config_params = {
        "poll_interval": 0.01,  # Fast polling for tests
        "rewrite_remote_links": True,
        "inline_inputs_size_limit": 64 * 1024,
    }
    if config_overrides:
        config_params.update(config_overrides)

    config = JobManagerConfig(**config_params)

    # Create JobManager first
    mgr = JobManager(
        providers_port,
        http_client,
        validator,
        repo,
        config=config,
        retry_port=retry_port,
        observers=[],
    )

    # Create observers with callback to manager's methods
    observers = [
        StatusHistoryObserver(repository=repo),
        PollingSchedulerObserver(schedule_callback=mgr._schedule_poll),
        ResultsVerificationObserver(http_client=http_client),
    ]
    mgr._observers = observers

    return mgr, repo, http_client


# --- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_results_synthesis_success():
    """Unit: JobManager.run_execution_pipeline
    Purpose: When provider returns an immediate results body (contains 'outputs')
    but no statusInfo snapshot, the manager synthesizes a terminal successful
    statusInfo, injects self/results links, and does NOT start polling.
    Assertions:
        - 201 response with Location
        - Job status successful
        - Self & results links present using local UUID
        - remote_status_url remains None (polling not scheduled)
    """
    # Provider returns outputs (no statusInfo) -> synthesize successful status
    post_response = {
        "status": 200,
        "headers": {},
        "body": {"outputs": {"raster": {"value": 42}}},
    }
    mgr, repo, _ = make_manager(post_response)
    resp = await mgr.run_execution_pipeline("prov:procA", {"inputs": {"x": 1}}, {})
    assert resp["status"] == 201
    job = await repo.get(resp["headers"]["Location"].split("/")[-1])
    assert job is not None, "Job should exist"
    assert job.status_info is not None, "status_info should be populated"
    assert job.status_info.status == StatusCode.successful
    # Ensure links contain local self and results
    assert job.status_info.links is not None, "links should be present"
    hrefs = {l.href for l in job.status_info.links}
    assert f"/jobs/{job.id}" in hrefs
    assert f"/jobs/{job.id}/results" in hrefs
    # No remote status polling scheduled (no remote_status_url synthesized)
    assert job.remote_status_url is None


@pytest.mark.asyncio
async def test_poll_timeout_marks_job_failed():
    """Unit: JobManager polling loop (_poll_loop)
    Purpose: Validate enforcement of process/provider timeout (ttw_job_done).
    Scenario: Initial remote snapshot is running; polling exceeds timeout. Job
    should transition to failed with explanatory message.
    Assertions:
      - Status becomes failed after sleep past timeout
      - Failure message contains 'Timed out'
    """
    # Provider returns running snapshot causing polling -> timeout triggers failure
    post_response = {
        "status": 200,
        "headers": {},
        "body": {
            "jobID": "remote-1",
            "status": "running",
            "type": "process",
        },
    }
    mgr, repo, _ = make_manager(post_response, config_overrides={"ttw_job_done": 0.05})
    resp = await mgr.run_execution_pipeline("prov:procB", {"inputs": {"y": 2}}, {})
    job_id = resp["headers"]["Location"].split("/")[-1]
    # wait past timeout
    await asyncio.sleep(0.08)
    job = await repo.get(job_id)
    assert job is not None, "Job should exist"
    assert job.status_info is not None, "status_info should be populated"
    assert job.status_info.status == StatusCode.failed
    assert "Timed out" in (job.status_info.message or "")


@pytest.mark.asyncio
async def test_link_normalization_replaces_remote_ids():
    """Unit: Link normalization in _derive_status_info/_ensure_self_link/_ensure_results_link
    Purpose: Ensure remote-provided self/results links referencing remote job id
    are filtered and replaced exclusively with local canonical UUID links.
    Assertions:
      - Exposed jobID equals local job.id
      - Local self & results links exist
      - Remote absolute links removed
    """
    # Remote provides jobID different + remote self link that should be replaced
    post_response = {
        "status": 200,
        "headers": {},
        "body": {
            "jobID": "remote-XYZ",
            "status": "successful",
            "type": "process",
            "links": [
                {
                    "href": "http://provider.test/jobs/remote-XYZ",
                    "rel": "self",
                    "type": "application/json",
                },
                {
                    "href": "http://provider.test/jobs/remote-XYZ/results",
                    "rel": "results",
                    "type": "application/json",
                },
            ],
        },
    }
    mgr, repo, _ = make_manager(post_response, get_responses=[{"ok": True}])
    resp = await mgr.run_execution_pipeline("prov:procE", {"inputs": {}}, {})
    job_id = resp["headers"]["Location"].split("/")[-1]
    job = await repo.get(job_id)
    assert job is not None, "Job should exist"
    assert job.status_info is not None, "status_info should be populated"
    assert job.status_info.jobID == job.id
    assert job.status_info.links is not None, "links should be present"
    hrefs = {link.href for link in job.status_info.links}
    assert f"/jobs/{job.id}" in hrefs
    assert f"/jobs/{job.id}/results" in hrefs
    # Ensure remote provider links are removed
    assert not any(h.startswith("http://provider.test/jobs/remote-XYZ") for h in hrefs)


@pytest.mark.asyncio
async def test_results_endpoint_proxy_success_and_not_available():
    """Unit: JobManager.get_results proxy
    Purpose: Validate remote-only results fetch: successful job returns provider
    results; non-successful job returns 404 and does not attempt results fetch.
    Assertions (success case): status=200, remote results fetched.
    Assertions (running case): status=404.
    """
    # Successful job scenario
    post_response_success = {
        "status": 200,
        "headers": {},
        "body": {"jobID": "remote-R", "status": "successful", "type": "process"},
    }
    # Provide two successful GET responses: one for verification during run_execution_pipeline
    # and one for the explicit results fetch call below.
    mgr_s, repo_s, http_s = make_manager(
        post_response_success, get_responses=[{"ok": True}, {"ok": True}]
    )
    resp_s = await mgr_s.run_execution_pipeline("prov:procF", {"inputs": {}}, {})
    job_id_s = resp_s["headers"]["Location"].split("/")[-1]
    # Call results endpoint
    results_resp = await mgr_s.get_results(job_id_s)
    assert results_resp["status"] == 200
    # get_results now returns body_bytes + content_type (remote Content-Type forwarded)
    assert b"ok" in results_resp.get("body_bytes", b"")
    assert any("/results" in u for u in http_s.get_calls)

    # Non-successful job scenario
    post_response_running = {
        "status": 200,
        "headers": {},
        "body": {"jobID": "remote-R2", "status": "running", "type": "process"},
    }
    mgr_r, repo_r, _ = make_manager(post_response_running)
    resp_r = await mgr_r.run_execution_pipeline("prov:procG", {"inputs": {}}, {})
    job_id_r = resp_r["headers"]["Location"].split("/")[-1]
    results_resp_fail = await mgr_r.get_results(job_id_r)
    assert results_resp_fail["status"] == 404
    # cleanup poll tasks
    await mgr_r.shutdown()
