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
