import asyncio
from types import SimpleNamespace

import pytest

from ump.api.models.process import Process
from ump.api.models.providers_config import ProcessConfig
from ump.errors import OGCProcessException


class _FakeProviderAuth:
    headers = {}
    auth = None


class _FakeAuthStrategy:
    def get_auth(self):
        return _FakeProviderAuth()


class _FakeResponse:
    def raise_for_status(self):
        return None


class _FakeJob:
    def __init__(self):
        self.job_id = "job-1"
        self.remote_job_id = "remote-1"
        self.status = None
        self.update_calls = 0

    def update(self):
        self.update_calls += 1


def _make_process_config(policy: str, result_storage: str) -> ProcessConfig:
    return ProcessConfig.model_validate(
        {
            "result-storage": result_storage,
            "anonymous-access": True,
            "transmission-mode-policy": policy,
        }
    )


def _make_process_instance() -> Process:
    p = Process.__new__(Process)
    process_any = p
    setattr(process_any, "provider_prefix", "modelserver")
    setattr(process_any, "process_id", "hello-geo-world")
    setattr(process_any, "process_id_with_prefix", "modelserver:hello-geo-world")
    setattr(process_any, "outputs", {"result": {}})
    setattr(process_any, "version", "1.0.0")
    setattr(process_any, "title", "hello-geo-world")
    return p


def test_start_process_execution_rewrites_forward_mode_and_keeps_delivery_mode(
    monkeypatch,
):
    process = _make_process_instance()

    process_config = _make_process_config(
        policy="emulate-ref-only",
        result_storage="geoserver",
    )
    provider = SimpleNamespace(
        server_url="http://modelserver:5005/",
        timeout=5,
        authentication=SimpleNamespace(type="NoAuth"),
        processes={"hello-geo-world": process_config},
    )

    import ump.api.models.process as process_module

    monkeypatch.setattr(
        process_module.providers,
        "get_providers",
        lambda: {"modelserver": provider},
    )
    monkeypatch.setattr(
        process_module.remote_auth,
        "get_auth_strategy",
        lambda _auth: _FakeAuthStrategy(),
    )

    captured = {}

    async def _fake_submit_remote_job(
        _session,
        _url,
        request_body,
        _auth,
        _headers,
        max_submit_seconds,
    ):
        captured["submitted_body"] = request_body
        captured["submit_timeout"] = max_submit_seconds
        return _FakeResponse()

    async def _fake_fetch_response_content(_response):
        return ("ok", "")

    async def _fake_extract_remote_job_id(_response):
        return "remote-1"

    async def _fake_create_local_job_instance(
        _remote_job_id,
        _name,
        _request_body,
        _user,
        transmission_mode,
    ):
        captured["delivered_mode"] = transmission_mode
        return _FakeJob()

    async def _fake_fetch_remote_job_status(
        _session,
        _url,
        _remote_job_id,
        _auth,
        _headers,
    ):
        return {"status": "accepted"}

    monkeypatch.setattr(process, "check_for_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process, "_submit_remote_job", _fake_submit_remote_job)
    monkeypatch.setattr(
        process_module,
        "fetch_response_content",
        _fake_fetch_response_content,
    )
    monkeypatch.setattr(process, "_extract_remote_job_id", _fake_extract_remote_job_id)
    monkeypatch.setattr(
        process,
        "_create_local_job_instance",
        _fake_create_local_job_instance,
    )
    monkeypatch.setattr(
        process,
        "_fetch_remote_job_status",
        _fake_fetch_remote_job_status,
    )

    request_body = {
        "inputs": {"name": "Me"},
        "outputs": {
            "result": {"transmissionMode": "reference"},
        },
    }

    job = asyncio.run(process.start_process_execution(request_body, user="u1"))

    assert job.job_id == "job-1"
    assert (
        captured["submitted_body"]["outputs"]["result"]["transmissionMode"] == "value"
    )
    assert captured["delivered_mode"] == "reference"


def test_start_process_execution_rejects_mixed_output_modes(monkeypatch):
    process = _make_process_instance()

    process_config = _make_process_config(
        policy="pass-through",
        result_storage="remote",
    )
    provider = SimpleNamespace(
        server_url="http://modelserver:5005/",
        timeout=5,
        authentication=SimpleNamespace(type="NoAuth"),
        processes={"hello-geo-world": process_config},
    )

    import ump.api.models.process as process_module

    monkeypatch.setattr(
        process_module.providers,
        "get_providers",
        lambda: {"modelserver": provider},
    )

    request_body = {
        "inputs": {"name": "Me"},
        "outputs": {
            "a": {"transmissionMode": "value"},
            "b": {"transmissionMode": "reference"},
        },
    }

    with pytest.raises(OGCProcessException) as exc:
        asyncio.run(process.start_process_execution(request_body, user="u1"))

    assert exc.value.response.status == 400
    assert "same transmissionMode" in exc.value.response.detail
