import asyncio

import pytest
from aioresponses import aioresponses

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.core.exceptions import OGCProcessException

"""
Tests for AioHttpClientAdapter behavior.

Each test verifies how the adapter maps various upstream responses and errors
into the domain-level OGCProcessException responses. Expected outcomes:
- Non-JSON responses should be treated as invalid upstream content and map to
    an OGCProcessException with status 502 (Bad Gateway / Invalid Response).
- HTTP error status codes from the provider (e.g. 500) are mapped to
    OGCProcessException with the same status to allow upstream error reporting.
- Network timeouts map to OGCProcessException with status 504 (Gateway Timeout).

These mappings ensure the web adapter can reliably translate provider issues
into appropriate OGC-style error responses for API clients.
"""


@pytest.mark.asyncio
async def test_get_json_response():
    # Happy path: provider returns valid JSON. The adapter should parse the
    # JSON and return a Python dict. Expected: a dict with 'processes': [].
    url = "http://example.test/processes"
    with aioresponses() as m:
        m.get(url, payload={"processes": []}, status=200)

        async with AioHttpClientAdapter() as client:
            data = await client.get(url)
            assert isinstance(data, dict)
            assert data.get("processes") == []


@pytest.mark.asyncio
async def test_get_non_json_response_raises_ogc_exception():
    # Non-JSON response: simulate the provider returning HTML/text instead of
    # valid JSON. The adapter must detect invalid JSON and raise an
    # OGCProcessException with status 502. Rationale: the provider violated the
    # expected API contract (JSON), which is treated as an upstream/bad-gateway
    # condition when surfaced to API clients.
    url = "http://example.test/bad"
    with aioresponses() as m:
        m.get(
            url,
            body="<html>error</html>",
            status=200,
            headers={"Content-Type": "text/html"},
        )

        async with AioHttpClientAdapter() as client:
            with pytest.raises(OGCProcessException) as excinfo:
                await client.get(url)
            assert excinfo.value.response.status == 502


@pytest.mark.asyncio
async def test_post_handles_500():
    # Upstream HTTP error: provider returns 500. The adapter should map this
    # to an OGCProcessException carrying the provider status (500) so the
    # caller can see the upstream error.
    url = "http://example.test/processes/echo/execution"
    with aioresponses() as m:
        m.post(url, status=500, body="Server Error")

        async with AioHttpClientAdapter() as client:
            with pytest.raises(OGCProcessException) as excinfo:
                await client.post(url, json={})
            assert excinfo.value.response.status == 500


@pytest.mark.asyncio
async def test_timeout_maps_to_ogc_timeout():
    # Timeout case: simulate a network/adapter timeout. The adapter should map
    # asyncio.TimeoutError to an OGCProcessException with status 504 (Gateway
    # Timeout) indicating the upstream request timed out.
    url = "http://example.test/slow"
    with aioresponses() as m:
        # simulate timeout by raising asyncio.TimeoutError
        m.get(url, exception=asyncio.TimeoutError())

        async with AioHttpClientAdapter() as client:
            with pytest.raises(OGCProcessException) as excinfo:
                await client.get(url)
            assert excinfo.value.response.status == 504


@pytest.mark.asyncio
async def test_post_non_json_returns_text():
    # When provider returns non-JSON content for a POST, adapter.post should
    # return a dict with 'body' containing the raw text.
    url = "http://example.test/processes/echo/execution"
    with aioresponses() as m:
        m.post(
            url,
            body="<html>ok</html>",
            status=202,
            headers={"Content-Type": "text/html"},
        )

        async with AioHttpClientAdapter() as client:
            resp = await client.post(url, json={})
            assert isinstance(resp, dict)
            assert resp.get("status") == 202
            assert resp.get("body") == "<html>ok</html>"


def test_build_client_timeout_scales_sock_read():
    # A caller-provided timeout is the total budget for the whole request. The
    # per-read (sock_read) budget must scale up to match so a slow-but-still-
    # progressing remote body (e.g. a large GeoJSON serialised on the fly) is
    # not cut off by the small default sock_read, which would surface as a
    # premature 504. sock_connect stays at the fast default.
    client = AioHttpClientAdapter()
    ct = client._build_client_timeout(120.0)
    assert ct.total == 120.0
    assert ct.sock_read == 120.0
    assert ct.sock_connect == client._default_sock_connect


def test_build_client_timeout_never_shrinks_sock_read():
    # A tiny explicit timeout must not shrink sock_read below the adapter
    # default; the read budget is max(timeout, default).
    client = AioHttpClientAdapter()
    ct = client._build_client_timeout(2.0)
    assert ct.total == 2.0
    assert ct.sock_read == client._default_sock_read


def test_build_client_timeout_none_returns_default():
    # No explicit timeout falls back to the pre-built adapter default.
    client = AioHttpClientAdapter()
    ct = client._build_client_timeout(None)
    assert ct is client._default_client_timeout
