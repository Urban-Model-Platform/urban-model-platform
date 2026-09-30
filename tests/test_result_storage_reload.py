"""Active ldproxy reload trigger (REF-F5, 2026-09-14).

Fakes stand in for the two network boundaries the trigger crosses:
``socket.getaddrinfo`` (headless-Service DNS, one address per ldproxy pod) and
``urllib.request.urlopen`` (the POST to each pod's reload sidecar). Storage
runs against a temp directory, as in test_result_storage_v6.
"""

from __future__ import annotations

import socket
import urllib.error

import pytest

from tests.test_result_storage_v6 import JOB_ID, _make_storage, _payload
from ump.adapters.result_storage import ldproxy_result_storage as module

RELOAD_URL = "http://ldproxy-reload.ns.svc.cluster.local:7082"


class _FakeNetwork:
    def __init__(self, ips: list[str], down: frozenset[str] = frozenset()) -> None:
        self.ips = ips
        self.down = down
        self.lookups = 0
        self.posted: list[str] = []

    def getaddrinfo(self, host, port, proto=0):
        self.lookups += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port)) for ip in self.ips]

    def urlopen(self, request, timeout=None):
        self.posted.append(request.full_url)
        if any(ip in request.full_url for ip in self.down):
            raise urllib.error.URLError("connection refused")
        return _Response()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def network(monkeypatch):
    fake = _FakeNetwork(["10.0.0.1", "10.0.0.2", "10.0.0.1"])
    monkeypatch.setattr(module.socket, "getaddrinfo", fake.getaddrinfo)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake.urlopen)
    return fake


class TestReloadTrigger:
    @pytest.mark.asyncio
    async def test_posts_to_every_resolved_pod_once(self, tmp_path, network):
        storage = _make_storage(tmp_path, reload_url=RELOAD_URL)

        await storage.store(JOB_ID, [_payload("voronoi")])

        assert sorted(network.posted) == [
            "http://10.0.0.1:7082/",
            "http://10.0.0.2:7082/",
        ]

    @pytest.mark.asyncio
    async def test_unset_reload_url_sends_nothing(self, tmp_path, network):
        storage = _make_storage(tmp_path)

        await storage.store(JOB_ID, [_payload("voronoi")])

        assert network.posted == []

    @pytest.mark.asyncio
    async def test_unreachable_pods_do_not_fail_the_store(self, tmp_path, network):
        network.down = frozenset({"10.0.0.1", "10.0.0.2"})
        storage = _make_storage(tmp_path, reload_url=RELOAD_URL)

        refs = await storage.store(JOB_ID, [_payload("voronoi")])

        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_dns_failure_does_not_fail_the_store(self, tmp_path, monkeypatch):
        def fail(*args, **kwargs):
            raise socket.gaierror("name not known")

        monkeypatch.setattr(module.socket, "getaddrinfo", fail)
        storage = _make_storage(tmp_path, reload_url=RELOAD_URL)

        refs = await storage.store(JOB_ID, [_payload("voronoi")])

        assert len(refs) == 1


class TestReloadDnsCache:
    @pytest.mark.parametrize(
        ("elapsed", "expected_lookups"),
        [(29.9, 1), (30.0, 2)],
        ids=["within-ttl-reuses", "after-ttl-re-resolves"],
    )
    @pytest.mark.asyncio
    async def test_resolution_is_cached_for_ttl(
        self, tmp_path, network, elapsed, expected_lookups
    ):
        clock = _Clock()
        storage = _make_storage(
            tmp_path, reload_url=RELOAD_URL, reload_dns_ttl=30.0, clock=clock
        )
        await storage.store("job-1", [_payload("voronoi")])
        clock.now = elapsed

        await storage.store("job-2", [_payload("voronoi")])

        assert network.lookups == expected_lookups

    @pytest.mark.asyncio
    async def test_failed_pod_forces_re_resolution(self, tmp_path, network):
        network.down = frozenset({"10.0.0.2"})
        storage = _make_storage(tmp_path, reload_url=RELOAD_URL, clock=_Clock())
        await storage.store("job-1", [_payload("voronoi")])

        await storage.store("job-2", [_payload("voronoi")])

        assert network.lookups == 2
