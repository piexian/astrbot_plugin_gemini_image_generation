from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tl.core.errors import (
    RemoteFetchBodyTooLargeError,
    RemoteFetchError,
    RemoteFetchRejectedError,
    RemoteFetchTimeoutError,
    RemoteFetchTooManyRedirectsError,
)
from tl.core.path_policy import PathPolicy
from tl.core.remote_fetch import RemoteFetchService


def _policy() -> PathPolicy:
    # loopback 测试服务器在私网段，策略必须放行。
    return PathPolicy((), allow_private_networks=True)


def _service(**kwargs) -> RemoteFetchService:
    return RemoteFetchService(_policy(), **kwargs)


def _app() -> web.Application:
    app = web.Application()

    async def ok(request: web.Request) -> web.Response:
        return web.Response(body=b"ref-bytes", content_type="image/png")

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 32)

    async def to_file(request: web.Request) -> web.Response:
        raise web.HTTPFound("file:///etc/passwd")

    async def loop(request: web.Request) -> web.Response:
        raise web.HTTPFound("/loop")

    async def redir_ok(request: web.Request) -> web.Response:
        raise web.HTTPFound("/img.png")

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.Response(body=b"late")

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    app.router.add_get("/img.png", ok)
    app.router.add_get("/big.bin", big)
    app.router.add_get("/to-file", to_file)
    app.router.add_get("/loop", loop)
    app.router.add_get("/redirect-ok", redir_ok)
    app.router.add_get("/slow", slow)
    app.router.add_get("/missing.png", missing)
    return app


async def _start(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_fetch_loopback_ok() -> None:
    server = await _start(_app())
    try:
        service = _service()
        data = await service.fetch(str(server.make_url("/img.png")))
        assert data == b"ref-bytes"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_fetch_redirect_within_policy() -> None:
    server = await _start(_app())
    try:
        data = await _service().fetch(str(server.make_url("/redirect-ok")))
        assert data == b"ref-bytes"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_body_over_limit_rejected() -> None:
    server = await _start(_app())
    try:
        service = _service(max_body_bytes=8)
        with pytest.raises(RemoteFetchBodyTooLargeError):
            await service.fetch(str(server.make_url("/big.bin")))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_redirect_to_forbidden_target_rejected() -> None:
    server = await _start(_app())
    try:
        with pytest.raises(RemoteFetchRejectedError):
            await _service().fetch(str(server.make_url("/to-file")))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_redirect_hops_exhausted() -> None:
    server = await _start(_app())
    try:
        with pytest.raises(RemoteFetchTooManyRedirectsError):
            await _service().fetch(str(server.make_url("/loop")))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_deadline_in_past_rejected() -> None:
    server = await _start(_app())
    try:
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(RemoteFetchTimeoutError):
            await _service().fetch(
                str(server.make_url("/img.png")), deadline_at=deadline
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_deadline_truncates_slow_response() -> None:
    server = await _start(_app())
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
        with pytest.raises(RemoteFetchTimeoutError):
            await _service().fetch(str(server.make_url("/slow")), deadline_at=deadline)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_naive_deadline_rejected() -> None:
    server = await _start(_app())
    try:
        with pytest.raises(ValueError):
            await _service().fetch(
                str(server.make_url("/img.png")),
                deadline_at=datetime.now(),
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_http_error_status() -> None:
    server = await _start(_app())
    try:
        with pytest.raises(RemoteFetchError) as excinfo:
            await _service().fetch(str(server.make_url("/missing.png")))
        assert "404" in str(excinfo.value)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_sensitive_headers(monkeypatch) -> None:
    calls = []

    class Response:
        def __init__(self, status, location=None):
            self.status = status
            self.headers = {"Location": location} if location else {}
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_chunked(self, _size):
            yield b"ok"

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            calls.append((url, kwargs["headers"]))
            if len(calls) == 1:
                return Response(302, "https://other.invalid/image.png")
            return Response(200)

    monkeypatch.setattr("tl.core.remote_fetch.aiohttp.ClientSession", Session)

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr("tl.core.remote_fetch.asyncio.to_thread", fake_to_thread)
    policy = type("Policy", (), {"validate_url": lambda self, url: url})()
    service = RemoteFetchService(policy)

    assert (
        await service.fetch(
            "https://first.invalid/image.png",
            headers={"Authorization": "Bearer secret", "Accept": "image/*"},
        )
        == b"ok"
    )
    assert calls[0][1]["Authorization"] == "Bearer secret"
    assert "Authorization" not in calls[1][1]
    assert calls[1][1]["Accept"] == "image/*"


@pytest.mark.asyncio
async def test_dns_validation_is_offloaded(monkeypatch) -> None:
    class Response:
        status = 200
        headers = {}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_chunked(self, _size):
            yield b"ok"

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            return Response()

    monkeypatch.setattr("tl.core.remote_fetch.aiohttp.ClientSession", Session)

    class Policy:
        def validate_url(self, url):
            return url

    offloaded = []

    async def fake_to_thread(func, *args):
        offloaded.append(func)
        return func(*args)

    monkeypatch.setattr("tl.core.remote_fetch.asyncio.to_thread", fake_to_thread)
    await RemoteFetchService(Policy()).fetch("https://example.invalid/image.png")
    assert len(offloaded) == 1
    assert offloaded[0].__name__ == "validate_url"


@pytest.mark.asyncio
async def test_redirect_hops_share_deadline_for_dns_validation(monkeypatch) -> None:
    calls = []

    class Response:
        def __init__(self, location=None):
            self.status = 302 if location else 200
            self.headers = {"Location": location} if location else {}
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_chunked(self, _size):
            yield b"ok"

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            calls.append(url)
            return Response(
                f"https://example.invalid/hop-{len(calls) + 1}.png"
                if len(calls) < 3
                else None
            )

    class Policy:
        def validate_url(self, url):
            return url

    async def slow_to_thread(func, *args):
        await asyncio.sleep(0.015)
        return func(*args)

    monkeypatch.setattr("tl.core.remote_fetch.aiohttp.ClientSession", Session)
    monkeypatch.setattr("tl.core.remote_fetch.asyncio.to_thread", slow_to_thread)

    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.04)
    with pytest.raises(RemoteFetchTimeoutError):
        await RemoteFetchService(Policy()).fetch(
            "https://example.invalid/start.png", deadline_at=deadline
        )
    assert calls == [
        "https://example.invalid/start.png",
        "https://example.invalid/hop-2.png",
    ]
