from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from tl.core.errors import (
    RemoteFetchBodyTooLargeError,
    RemoteFetchError,
    RemoteFetchRejectedError,
    RemoteFetchTimeoutError,
    RemoteFetchTooManyRedirectsError,
)
from tl.core.remote_fetch import RemoteFetchService


def _make_png(width: int, height: int) -> bytes:
    """生成超过 PROBE_BYTES(4KB) 的合法 PNG。"""

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (9, 80, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


# 超过探测阈值的合法 PNG：探测后应继续读完而不是拒绝。
_PNG_BYTES = _make_png(900, 900)


def _service(**kwargs) -> RemoteFetchService:
    return RemoteFetchService(**kwargs)


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

    async def junk(request: web.Request) -> web.Response:
        # 超过探测阈值的非图片内容，用于验证提前拦截。
        return web.Response(body=b"<html>" * 4096, content_type="text/html")

    async def png_large(request: web.Request) -> web.Response:
        # 超过探测阈值的合法 PNG，用于确认探测不会误杀。
        return web.Response(body=_PNG_BYTES, content_type="image/png")

    app.router.add_get("/img.png", ok)
    app.router.add_get("/big.bin", big)
    app.router.add_get("/to-file", to_file)
    app.router.add_get("/loop", loop)
    app.router.add_get("/redirect-ok", redir_ok)
    app.router.add_get("/slow", slow)
    app.router.add_get("/missing.png", missing)
    app.router.add_get("/junk.html", junk)
    app.router.add_get("/png-large.png", png_large)
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
    service = RemoteFetchService()

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
async def test_redirect_hops_share_deadline(monkeypatch) -> None:
    calls = []

    class Response:
        def __init__(self, location=None):
            self.status = 302 if location else 200
            self.headers = {"Location": location} if location else {}
            self.content = self

        async def __aenter__(self):
            # 每跳网络耗时；deadline 由循环在下一跳开始前判定。
            await asyncio.sleep(0.03)
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

    monkeypatch.setattr("tl.core.remote_fetch.aiohttp.ClientSession", Session)

    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.04)
    with pytest.raises(RemoteFetchTimeoutError):
        await RemoteFetchService().fetch(
            "https://example.invalid/start.png", deadline_at=deadline
        )
    assert calls == [
        "https://example.invalid/start.png",
        "https://example.invalid/hop-2.png",
    ]


@pytest.mark.asyncio
async def test_probe_rejects_non_image_before_full_download() -> None:
    """探测在读完响应体之前就拒绝非图片，且只拿到文件头前缀。"""

    server = await _start(_app())
    seen: list[bytes] = []

    def probe(data: bytes) -> None:
        seen.append(data)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RemoteFetchRejectedError("不是受支持的图片格式")

    try:
        with pytest.raises(RemoteFetchRejectedError, match="不是受支持的图片格式"):
            await _service().fetch(str(server.make_url("/junk.html")), probe=probe)
    finally:
        await server.close()
    # 服务端共 24KB，探测只应看到 4KB 前缀，说明主体没有被读完。
    assert len(seen) == 1
    assert len(seen[0]) == 4 * 1024
    assert seen[0].startswith(b"<html>")


@pytest.mark.asyncio
async def test_probe_allows_valid_image_to_complete() -> None:
    """探测不应误杀合法图片：通过探测后完整读取。"""

    server = await _start(_app())
    seen: list[bytes] = []

    def probe(data: bytes) -> None:
        seen.append(data)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RemoteFetchRejectedError("不是受支持的图片格式")

    try:
        data = await _service().fetch(
            str(server.make_url("/png-large.png")), probe=probe
        )
    finally:
        await server.close()
    assert data == _PNG_BYTES
    assert len(seen) == 1
    assert len(seen[0]) == 4 * 1024
