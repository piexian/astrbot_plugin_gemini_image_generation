from __future__ import annotations

import asyncio
import json

import pytest

from tl import model_catalog as catalog
from tl.model_catalog import (
    ModelCatalogError,
    ModelCatalogService,
    make_catalog_request,
    safe_target,
)

FAKE_KEY = "fake-catalog-key-never-real"
FAKE_QUERY_SECRET = "fake-query-secret"


class FakeResponse:
    def __init__(self, payload=None, *, status=200, body=None, block=None):
        self.status = status
        self.body = json.dumps(payload).encode() if body is None else body
        self.content_length = None
        self.content = self
        self.block = block
        self.started = asyncio.Event()
        self.exited = False

    async def __aenter__(self):
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class FakeHTTP:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.sessions = []

    def session(self, **kwargs):
        session = FakeSession(self, kwargs)
        self.sessions.append(session)
        return session


class FakeSession:
    def __init__(self, http, kwargs):
        self.http = http
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.http.calls.append((url, kwargs))
        if not self.http.responses:
            raise AssertionError("Unexpected fake HTTP request")
        response = self.http.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def http(monkeypatch):
    """No test can accidentally use a real supplier connection."""
    http = FakeHTTP()
    monkeypatch.setattr(catalog.aiohttp, "ClientSession", http.session)
    return http


def request(protocol="openai", base="https://catalog.invalid/gateway"):
    return make_catalog_request(protocol, base, FAKE_KEY)


@pytest.mark.parametrize("key", ["", " ", "fake-key\r\nX-Header: bad", None])
def test_invalid_key_is_not_echoed(key):
    with pytest.raises(ModelCatalogError) as error:
        make_catalog_request("google", "https://catalog.invalid", key)
    assert error.value.status_code == 400
    assert "X-Header" not in str(error.value)


def test_safe_target_does_not_echo_key_in_host():
    req = request(base=f"https://{FAKE_KEY}.catalog.invalid/v1")
    assert safe_target(req) == "自定义目标"


@pytest.mark.asyncio
async def test_filters_known_credentials_and_obvious_echoes(http):
    http.responses = [
        FakeResponse(
            {
                "data": [
                    {"id": value}
                    for value in [
                        "fake-valid-model",
                        FAKE_KEY,
                        f"echo-{FAKE_KEY}",
                        FAKE_QUERY_SECRET,
                        "Bearer fake-other-key",
                        "token=fake-private",
                        '{"api_key":"fake-private"}',
                        "sk-fakeotherkey0123456789",
                        "api_key=fake-other",
                        "https://fake:fake-password@catalog.invalid",
                    ]
                ]
            }
        )
    ]
    service = ModelCatalogService()
    result = await service.fetch(
        request(base=f"https://catalog.invalid/v1?token={FAKE_QUERY_SECRET}")
    )
    assert result["models"] == [{"id": "fake-valid-model", "label": "fake-valid-model"}]
    assert "过滤" in result["warning"]
    assert FAKE_KEY not in json.dumps(result)
    await service.close()


@pytest.mark.asyncio
async def test_http_proxy_auth_not_echoed(http):
    req = make_catalog_request(
        "openai",
        "https://catalog.invalid",
        FAKE_KEY,
        "http://fake-user:fake-proxy-password@localhost:9000",
    )
    http.responses = [RuntimeError(f"{req.url} {req.headers} {req.proxy}")]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(req)
    assert error.value.reason == "connection_error"
    assert "fake-proxy-password" not in repr(error.value)
    assert FAKE_KEY not in str(error.value)
    assert http.calls[0][1]["proxy"] == req.proxy
    assert http.sessions[0].closed
    await service.close()
