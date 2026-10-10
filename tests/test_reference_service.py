from __future__ import annotations

import base64
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image as PILImage

import tl.core.reference_service as reference_service_module
from tl.core.errors import (
    PathPolicyError,
    ReferenceResolveError,
    ReferenceTimeoutError,
    ReferenceTooLargeError,
)
from tl.core.path_policy import PathPolicy
from tl.core.reference_service import ReferenceService
from tl.core.remote_fetch import RemoteFetchService


def _png_bytes(size: tuple[int, int] = (8, 8), color: tuple[int, ...] = (255, 0, 0)):
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _policy(root: Path) -> PathPolicy:
    # loopback 测试服务器在私网段，策略必须放行。
    return PathPolicy((root,), allow_private_networks=True)


def _service(root: Path, cache_dir: Path, **kwargs) -> ReferenceService:
    policy = _policy(root)
    return ReferenceService(policy, RemoteFetchService(policy), cache_dir, **kwargs)


async def _collect(service: ReferenceService, refs, **kwargs):
    return [artifact async for artifact in service.resolve(tuple(refs), **kwargs)]


def _app(state: dict) -> web.Application:
    app = web.Application()

    async def img(request: web.Request) -> web.Response:
        state["hits"] += 1
        state["referers"].append(request.headers.get("Referer", ""))
        return web.Response(body=_PNG_BYTES, content_type="image/png")

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    app.router.add_get("/img.png", img)
    app.router.add_get("/missing.png", missing)
    return app


async def _start(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


_PNG_BYTES = _png_bytes()


@pytest.mark.asyncio
async def test_local_path_resolves(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    payload = _png_bytes(color=(1, 2, 3))
    ref = root / "ref.png"
    ref.write_bytes(payload)
    cache_dir = tmp_path / "cache"
    service = _service(root, cache_dir)

    artifacts = await _collect(service, (str(ref),))

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.kind == "reference"
    assert artifact.artifact_id.startswith("ref-")
    assert artifact.mime_type == "image/png"
    assert artifact.size_bytes == len(payload)
    assert artifact.metadata["source"] == ref.resolve().as_uri()
    stored = Path(artifact.location)
    assert stored.parent == cache_dir
    assert stored.is_file()
    assert stored.read_bytes() == payload


@pytest.mark.asyncio
async def test_file_uri_equivalent_to_bare_path(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes(color=(4, 5, 6)))
    cache_dir = tmp_path / "cache"
    service = _service(root, cache_dir)

    from_bare = (await _collect(service, (str(ref),)))[0]
    from_uri = (await _collect(service, (ref.as_uri(),)))[0]

    assert from_uri.location == from_bare.location
    assert from_uri.metadata["source"] == from_bare.metadata["source"]


@pytest.mark.asyncio
async def test_path_outside_root_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    service = _service(root, tmp_path / "cache")

    with pytest.raises(PathPolicyError):
        await _collect(service, (str(outside),))


@pytest.mark.asyncio
async def test_url_fetch_qq_headers_and_cache(tmp_path, monkeypatch) -> None:
    state = {"hits": 0, "referers": []}
    server = await _start(_app(state))
    try:
        monkeypatch.setattr(
            reference_service_module, "is_qq_image_host", lambda host: True
        )
        root = tmp_path / "allowed"
        root.mkdir()
        service = _service(root, tmp_path / "cache")
        url = str(server.make_url("/img.png"))

        first = (await _collect(service, (url,)))[0]
        assert state["hits"] == 1
        assert state["referers"] == ["https://qun.qq.com"]
        assert first.location

        second = (await _collect(service, (url,)))[0]
        assert state["hits"] == 1  # 第二次命中缓存，服务器未再收到请求
        assert second.location == first.location
        assert second.metadata["source"] == first.metadata["source"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_data_uri_and_bare_base64(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    payload = _png_bytes(color=(9, 9, 9))
    encoded = base64.b64encode(payload).decode()

    artifacts = await _collect(
        service,
        (f"data:image/png;base64,{encoded}", encoded),
    )

    assert len(artifacts) == 2
    assert artifacts[0].location == artifacts[1].location
    assert artifacts[0].metadata["source"] == "inline"
    assert artifacts[1].metadata["source"] == "inline"
    assert Path(artifacts[0].location).read_bytes() == payload


@pytest.mark.asyncio
async def test_pixel_overflow_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "big.png"
    ref.write_bytes(_png_bytes(size=(4, 4)))
    service = _service(root, tmp_path / "cache", max_pixels=4)

    with pytest.raises(ReferenceTooLargeError):
        await _collect(service, (str(ref),))


@pytest.mark.asyncio
async def test_bytes_overflow_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache", max_reference_bytes=8)
    payload = _png_bytes()
    data_uri = f"data:image/png;base64,{base64.b64encode(payload).decode()}"

    with pytest.raises(ReferenceTooLargeError):
        await _collect(service, (data_uri,))


@pytest.mark.asyncio
async def test_non_image_bytes_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    not_image = base64.b64encode(b"hello world, not an image").decode()

    with pytest.raises(ReferenceResolveError):
        await _collect(service, (f"data:image/png;base64,{not_image}",))
    with pytest.raises(ReferenceResolveError):
        await _collect(service, ("!!!not-base64!!!",))


@pytest.mark.asyncio
async def test_heic_passthrough_without_pixel_check(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    # 仅 ftyp 头的 HEIC：文件头可识别、Pillow 缺 libheif 解不开，应透传。
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8
    heic_b64 = base64.b64encode(heic).decode()

    artifacts = await _collect(service, (f"data:image/heic;base64,{heic_b64}",))

    assert len(artifacts) == 1
    assert artifacts[0].mime_type == "image/heic"
    assert artifacts[0].size_bytes == len(heic)


@pytest.mark.asyncio
async def test_url_fetch_404_raises(tmp_path) -> None:
    state = {"hits": 0, "referers": []}
    server = await _start(_app(state))
    try:
        root = tmp_path / "allowed"
        root.mkdir()
        service = _service(root, tmp_path / "cache")

        with pytest.raises(ReferenceResolveError):
            await _collect(service, (str(server.make_url("/missing.png")),))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_expired_deadline_raises(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes())
    service = _service(root, tmp_path / "cache")
    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(ReferenceTimeoutError):
        await _collect(service, (str(ref),), deadline_at=deadline)


@pytest.mark.asyncio
async def test_naive_deadline_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes())
    service = _service(root, tmp_path / "cache")

    with pytest.raises(ValueError):
        await _collect(service, (str(ref),), deadline_at=datetime.now())


@pytest.mark.asyncio
async def test_multiple_references_in_order(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    red = _png_bytes(size=(2, 2), color=(255, 0, 0))
    green = _png_bytes(size=(3, 3), color=(0, 255, 0))
    blue = _png_bytes(size=(4, 4), color=(0, 0, 255))
    blue_ref = root / "blue.png"
    blue_ref.write_bytes(blue)
    service = _service(root, tmp_path / "cache")
    refs = (
        f"data:image/png;base64,{base64.b64encode(green).decode()}",
        str(blue_ref),
        f"data:image/png;base64,{base64.b64encode(red).decode()}",
    )

    artifacts = await _collect(service, refs)

    assert [Path(a.location).read_bytes() for a in artifacts] == [green, blue, red]
    assert [a.metadata["source"] for a in artifacts] == [
        "inline",
        blue_ref.resolve().as_uri(),
        "inline",
    ]
