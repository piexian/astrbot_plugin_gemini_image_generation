from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image as PILImage

import tl.core.reference_service as reference_service_module
from tl.core.errors import (
    PathPolicyError,
    ReferenceAccessDeniedError,
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


def _gif_bytes(
    size: tuple[int, int] = (8, 8), first_color: tuple[int, ...] = (255, 0, 0)
) -> bytes:
    """两帧 GIF：首帧 first_color、第二帧蓝色，用于断言转码取首帧。"""
    buf = io.BytesIO()
    frames = [
        PILImage.new("RGB", size, first_color if i == 0 else (0, 0, 255))
        for i in range(2)
    ]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _transparent_gif_bytes(
    size: tuple[int, int] = (8, 8), first_color: tuple[int, ...] = (255, 0, 0)
) -> bytes:
    """首帧带透明索引的两帧 GIF，验证转码后保留 alpha。"""
    palette = list(first_color) + [0, 0, 255] + [0, 0, 0] * 254
    frames = []
    for index in (0, 1):
        frame = PILImage.new("P", size, index)
        frame.putpalette(palette)
        frames.append(frame)
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:], transparency=0
    )
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
async def test_local_bytes_overflow_rejected_before_read(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "large.png"
    ref.write_bytes(b"x" * 32)
    service = _service(root, tmp_path / "cache", max_reference_bytes=8)

    with pytest.raises(ReferenceTooLargeError):
        await _collect(service, (str(ref),))


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
async def test_gif_transcodes_to_png_first_frame(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    payload = _gif_bytes()
    ref = root / "ref.gif"
    ref.write_bytes(payload)
    cache_dir = tmp_path / "cache"
    service = _service(root, cache_dir)

    artifacts = await _collect(service, (str(ref),))

    artifact = artifacts[0]
    assert artifact.mime_type == "image/png"
    stored = Path(artifact.location)
    assert stored.suffix == ".png"
    assert artifact.size_bytes == stored.stat().st_size
    with PILImage.open(stored) as img:
        assert img.format == "PNG"
        assert img.mode == "RGBA"
        # 取首帧：红色保留，第二帧蓝色不出现
        assert img.getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.parametrize(
    ("fmt", "color"),
    [("BMP", (0, 255, 0)), ("TIFF", (0, 0, 255))],
)
@pytest.mark.asyncio
async def test_bmp_tiff_transcode_to_png(tmp_path, fmt, color) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color).save(buf, format=fmt)
    ref = root / f"ref.{fmt.lower()}"
    ref.write_bytes(buf.getvalue())
    service = _service(root, tmp_path / "cache")

    artifacts = await _collect(service, (str(ref),))

    assert artifacts[0].mime_type == "image/png"
    with PILImage.open(Path(artifacts[0].location)) as img:
        assert img.format == "PNG"
        assert img.getpixel((0, 0)) == color


@pytest.mark.asyncio
async def test_transparent_gif_keeps_alpha(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "alpha.gif"
    ref.write_bytes(_transparent_gif_bytes())
    service = _service(root, tmp_path / "cache")

    artifacts = await _collect(service, (str(ref),))

    stored = Path(artifacts[0].location)
    with PILImage.open(stored) as img:
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0)) == (255, 0, 0, 0)


@pytest.mark.asyncio
async def test_unknown_magic_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    ico = b"\x00\x00\x01\x00\x01\x00" + b"\x00" * 32
    junk = b"NOTAIMG" + bytes(range(64))

    for payload in (ico, junk):
        data_uri = f"data:image/png;base64,{base64.b64encode(payload).decode()}"
        with pytest.raises(ReferenceResolveError):
            await _collect(service, (data_uri,))


@pytest.mark.asyncio
async def test_oversized_gif_rejected_before_transcode(tmp_path, monkeypatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "big.gif"
    ref.write_bytes(_gif_bytes(size=(8, 8)))
    service = _service(root, tmp_path / "cache")
    service.max_pixels = 16  # 8x8=64 超限
    calls: list[int] = []
    original = reference_service_module.transcode_to_supported_mime

    def _spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(reference_service_module, "transcode_to_supported_mime", _spy)

    with pytest.raises(ReferenceTooLargeError):
        await _collect(service, (str(ref),))
    assert calls == []  # 像素超限在转码前拒绝


@pytest.mark.asyncio
async def test_url_fetch_gif_caches_png(tmp_path) -> None:
    state = {"hits": 0, "referers": []}
    gif_payload = _gif_bytes()

    async def gif(request: web.Request) -> web.Response:
        state["hits"] += 1
        return web.Response(body=gif_payload, content_type="image/gif")

    app = web.Application()
    app.router.add_get("/img.gif", gif)
    server = await _start(app)
    try:
        root = tmp_path / "allowed"
        root.mkdir()
        cache_dir = tmp_path / "cache"
        service = _service(root, cache_dir)
        url = str(server.make_url("/img.gif"))

        first = (await _collect(service, (url,)))[0]
        assert first.mime_type == "image/png"
        assert Path(first.location).read_bytes().startswith(b"\x89PNG")
        # URL 缓存与产物缓存写的都是转码后的 PNG 字节
        cached_files = list(cache_dir.glob("*.png"))
        assert len(cached_files) == 2
        assert all(p.read_bytes().startswith(b"\x89PNG") for p in cached_files)

        second = (await _collect(service, (url,)))[0]
        assert state["hits"] == 1  # 命中 URL 缓存，未二次抓取
        assert second.location == first.location
        assert second.mime_type == "image/png"
    finally:
        await server.close()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
@pytest.mark.asyncio
async def test_passthrough_formats_unchanged(tmp_path, fmt) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), (10, 20, 30)).save(buf, format=fmt)
    payload = buf.getvalue()
    data_uri = f"data:image/{fmt.lower()};base64,{base64.b64encode(payload).decode()}"
    service = _service(root, tmp_path / "cache")

    artifacts = await _collect(service, (data_uri,))

    artifact = artifacts[0]
    assert artifact.mime_type == f"image/{fmt.lower()}"
    assert artifact.size_bytes == len(payload)
    assert Path(artifact.location).read_bytes() == payload


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


# ---- ingest 与 ref:tmp: 句柄 ----


def _handle_pattern():
    return reference_service_module._HANDLE_RE


@pytest.mark.asyncio
async def test_ingest_returns_tmp_handle_and_resolves(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    payload = _png_bytes(color=(11, 22, 33))
    requester = {"user_id": "u1", "umo": "qq:g1"}
    data_uri = f"data:image/png;base64,{base64.b64encode(payload).decode()}"

    handle = await service.ingest(data_uri, requester=requester)

    assert handle.startswith("ref:tmp:")
    assert _handle_pattern().match(handle)
    assert len(handle) <= 2048
    # 原始字节输入同样可 ingest。
    handle_from_bytes = await service.ingest(payload, requester=requester)
    assert handle_from_bytes.startswith("ref:tmp:")

    artifacts = await _collect(service, (handle,), requester=requester)
    artifact = artifacts[0]
    assert artifact.kind == "reference"
    assert artifact.mime_type == "image/png"
    assert artifact.size_bytes == len(payload)
    assert Path(artifact.location).parent == tmp_path / "cache"
    assert Path(artifact.location).read_bytes() == payload
    assert artifact.metadata["source"] == "inline"
    assert artifact.metadata["handle"] == handle

    # 另一个 requester 解析同一句柄被拒绝。
    with pytest.raises(ReferenceAccessDeniedError):
        await _collect(service, (handle,), requester={"user_id": "u2"})


@pytest.mark.asyncio
async def test_ingest_inline_mixed_inputs_keep_order(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    local = root / "local.png"
    local.write_bytes(_png_bytes(color=(1, 1, 1)))
    service = _service(root, tmp_path / "cache")
    url = "https://example.com/a.png"
    data_uri = f"data:image/png;base64,{base64.b64encode(_png_bytes(color=(2, 2, 2))).decode()}"
    bare = base64.b64encode(_png_bytes(color=(3, 3, 3))).decode()

    prepared = await service.ingest_inline(
        [url, str(local), data_uri, bare], requester={"user_id": "u"}
    )

    # 非内联形态原样保留，内联形态换句柄，顺序不变。
    assert prepared[0] == url
    assert prepared[1] == str(local)
    assert prepared[2].startswith("ref:tmp:")
    assert prepared[3].startswith("ref:tmp:")
    assert prepared[2] != prepared[3]
    artifacts = await _collect(service, prepared[2:], requester={"user_id": "u"})
    assert [Path(a.location).read_bytes() for a in artifacts] == [
        _png_bytes(color=(2, 2, 2)),
        _png_bytes(color=(3, 3, 3)),
    ]


@pytest.mark.asyncio
async def test_ingest_failures_leave_no_entries(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache", max_pixels=64)
    not_image = base64.b64encode(b"hello world, not an image").decode()

    with pytest.raises(ReferenceResolveError):
        await service.ingest(f"data:image/png;base64,{not_image}", requester={})
    with pytest.raises(ReferenceResolveError):
        await service.ingest("!!!not-base64!!!", requester={})
    with pytest.raises(ReferenceTooLargeError):
        await service.ingest(_png_bytes(size=(16, 16)), requester={})

    assert service._stores["tmp"]._entries == {}


@pytest.mark.asyncio
async def test_handle_forgery_and_unknown_kind_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    forged = [
        "ref:tmp:../../etc/passwd",
        f"ref:tmp:{'A' * 32}",
        "ref:tmp:abc123",
        "ref:unknown:" + "a" * 32,
        "ref:tmp:" + "a" * 32,  # 合法形态但未登记
    ]
    for ref in forged:
        with pytest.raises(ReferenceResolveError):
            await _collect(service, (ref,), requester={"user_id": "u"})


def test_reference_descriptor_not_confused_with_handle():
    from tl.core.models import Job
    from tl.core.reference_service import classify_reference
    from tl.core.requests import GenerationRequest, reference_descriptor

    handle = f"ref:tmp:{'a' * 32}"
    descriptor = reference_descriptor(handle)
    # 描述符 reference_id 只有一个冒号，不会被句柄正则命中。
    assert descriptor["reference_id"].startswith("ref:")
    assert len(descriptor["reference_id"].split(":")) == 2
    assert classify_reference(handle) == "handle"
    assert classify_reference(descriptor["reference_id"]) == "inline"

    # 含句柄的请求经 to_storage_dict 后：无字节、无本地路径，句柄留在 display。
    import json

    request = GenerationRequest(
        prompt="draw",
        source="command",
        reference_images=(handle,),
        requester={"user_id": "u1"},
    )
    payload = Job.create(request, job_id="job-h").to_storage_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    refs = payload["request"]["reference_images"]
    assert refs[0]["reference_id"].startswith("ref:")
    assert refs[0]["display"] == handle
    assert "base64" not in encoded
    assert "/" not in refs[0]["display"]


@pytest.mark.parametrize(
    "value",
    [
        "dAtA:image/png;base64,iVBORw0KGgo=",
        "iVBORw0KGgo=",
    ],
)
def test_reference_descriptor_never_persists_inline_payload(value):
    from tl.core.requests import reference_descriptor

    descriptor = reference_descriptor(value)

    assert descriptor["reference_id"].startswith("ref:")
    assert "iVBORw0KGgo=" not in json.dumps(descriptor)
    assert "base64," not in json.dumps(descriptor)


def test_classify_reference_is_case_insensitive(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes())

    assert reference_service_module.classify_reference("HtTpS://x") == "url"
    assert (
        reference_service_module.classify_reference(
            ref.as_uri().replace("file:", "FiLe:")
        )
        == "file"
    )
    assert (
        reference_service_module.classify_reference("dAtA:image/png;base64,abc")
        == "inline"
    )


@pytest.mark.asyncio
async def test_ingest_oversized_base64_rejected_before_decode(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache", max_reference_bytes=8)
    payload = base64.b64encode(b"x" * 32).decode()
    original = reference_service_module.base64.b64decode

    def fail_if_decoded(*args, **kwargs):
        raise AssertionError("base64 payload should be rejected before decoding")

    monkeypatch.setattr(reference_service_module.base64, "b64decode", fail_if_decoded)
    with pytest.raises(ReferenceTooLargeError):
        await service.ingest(f"DaTa:ImAgE/pNg;BaSe64,{payload}", requester={})
    monkeypatch.setattr(reference_service_module.base64, "b64decode", original)


@pytest.mark.asyncio
async def test_case_insensitive_file_uri_scheme(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes())
    service = _service(root, tmp_path / "cache")

    artifacts = await _collect(service, (ref.as_uri().replace("file:", "FiLe:"),))
    assert artifacts[0].location


@pytest.mark.asyncio
async def test_case_insensitive_http_uri_scheme(tmp_path, monkeypatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    monkeypatch.setattr(service.policy, "validate_url", lambda url: url)

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(reference_service_module.asyncio, "to_thread", fake_to_thread)

    async def fake_fetch(url, **kwargs):
        return _PNG_BYTES

    monkeypatch.setattr(service.fetcher, "fetch", fake_fetch)
    artifact = await service._load_url("HTTP://example.invalid/image.png", None)
    assert artifact.metadata["source"].endswith("example.invalid/image.png")


@pytest.mark.asyncio
async def test_url_cache_hit_still_validates_policy(tmp_path, monkeypatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    cache_dir = tmp_path / "cache"
    service = _service(root, cache_dir)
    service.policy.allow_private_networks = False

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(reference_service_module.asyncio, "to_thread", fake_to_thread)
    url = "http://127.0.0.1:9/secret.png"
    cache_dir.mkdir()
    cache_key = reference_service_module.hashlib.sha256(url.encode()).hexdigest()
    (cache_dir / f"{cache_key}.png").write_bytes(_PNG_BYTES)

    with pytest.raises(PathPolicyError):
        await service._load_url(url, None)


@pytest.mark.asyncio
async def test_concurrent_url_misses_are_serialized(tmp_path, monkeypatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    service = _service(root, tmp_path / "cache")
    monkeypatch.setattr(service.policy, "validate_url", lambda url: url)

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(reference_service_module.asyncio, "to_thread", fake_to_thread)
    calls = 0

    async def fake_fetch(url, **kwargs):
        nonlocal calls
        calls += 1
        return _PNG_BYTES

    monkeypatch.setattr(service.fetcher, "fetch", fake_fetch)
    url = "https://example.invalid/image.png"
    await asyncio.gather(service._load_url(url, None), service._load_url(url, None))
    assert calls == 1
    assert service._url_locks == {}
    assert service._url_lock_users == {}


# ---- trusted_local：调用方已校验过的本地路径 ----


def _outside_png(tmp_path: Path, name: str = "outside.png", color=(5, 6, 7)):
    """落在 PathPolicy 本地根之外的 PNG，返回 (路径, 字节)。"""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    payload = _png_bytes(color=color)
    path = outside / name
    path.write_bytes(payload)
    return path, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["local", "file"])
async def test_trusted_local_ingests_local_files_as_tmp_handles(tmp_path, shape):
    root = tmp_path / "allowed"
    root.mkdir()
    path, payload = _outside_png(tmp_path)
    service = _service(root, tmp_path / "cache")
    reference = str(path) if shape == "local" else path.resolve().as_uri()
    requester = {"user_id": "u1", "umo": "qq:g1"}

    prepared = await service.ingest_inline(
        [reference], requester=requester, trusted_local=True
    )

    assert len(prepared) == 1
    assert _handle_pattern().match(prepared[0])
    artifacts = await _collect(service, prepared, requester=requester)
    assert Path(artifacts[0].location).parent == tmp_path / "cache"
    assert Path(artifacts[0].location).read_bytes() == payload
    # PathPolicy 的本地根没有被放宽：同一路径直接解析仍被拒绝。
    with pytest.raises(PathPolicyError):
        await _collect(service, (reference,), requester=requester)


@pytest.mark.asyncio
async def test_trusted_local_keeps_order_and_other_shapes(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    first, first_bytes = _outside_png(tmp_path, "first.png", (1, 1, 1))
    second, second_bytes = _outside_png(tmp_path, "second.png", (2, 2, 2))
    service = _service(root, tmp_path / "cache")
    url = "https://example.com/a.png"
    data_uri = f"data:image/png;base64,{base64.b64encode(_png_bytes()).decode()}"
    requester = {"user_id": "u"}

    prepared = await service.ingest_inline(
        [str(first), url, data_uri, second.resolve().as_uri()],
        requester=requester,
        trusted_local=True,
    )

    # URL 原样保留；本地与内联图都换成句柄，顺序不变。
    assert prepared[1] == url
    assert all(prepared[i].startswith("ref:tmp:") for i in (0, 2, 3))
    artifacts = await _collect(
        service, (prepared[0], prepared[2], prepared[3]), requester=requester
    )
    assert Path(artifacts[0].location).read_bytes() == first_bytes
    assert Path(artifacts[2].location).read_bytes() == second_bytes


@pytest.mark.asyncio
async def test_trusted_local_defaults_to_false_and_keeps_local_paths(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    path, _ = _outside_png(tmp_path)
    service = _service(root, tmp_path / "cache")
    references = [str(path), path.resolve().as_uri()]

    prepared = await service.ingest_inline(references, requester={})

    assert prepared == tuple(references)
    assert service._stores["tmp"]._entries == {}


@pytest.mark.asyncio
async def test_trusted_local_rejects_oversized_file_by_stat_before_reading(
    tmp_path, monkeypatch
):
    root = tmp_path / "allowed"
    root.mkdir()
    large = tmp_path / "outside" / "large.png"
    large.parent.mkdir()
    large.write_bytes(b"x" * 32)
    service = _service(root, tmp_path / "cache", max_reference_bytes=8)

    def fail_open(self, *args, **kwargs):
        raise AssertionError("超限文件不应被读取")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(ReferenceTooLargeError):
        await service.ingest_inline(
            [large.resolve().as_uri()], requester={}, trusted_local=True
        )
    assert service._stores["tmp"]._entries == {}


@pytest.mark.asyncio
async def test_trusted_local_read_is_bounded_when_file_grows_after_stat(
    tmp_path, monkeypatch
):
    root = tmp_path / "allowed"
    root.mkdir()
    grown = tmp_path / "outside" / "grown.png"
    grown.parent.mkdir()
    grown.write_bytes(b"x" * 64)
    service = _service(root, tmp_path / "cache", max_reference_bytes=8)
    real_stat = Path.stat
    real_open = Path.open
    read_sizes = []

    def stale_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_size=1)

    class SpyFile:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self._inner.__exit__(*exc_info)

        def read(self, size=-1):
            read_sizes.append(size)
            return self._inner.read(size)

    monkeypatch.setattr(Path, "stat", stale_stat)
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: SpyFile(real_open(self, *a, **k))
    )
    with pytest.raises(ReferenceTooLargeError):
        await service.ingest_inline(
            [grown.resolve().as_uri()], requester={}, trusted_local=True
        )
    # stat 已过期时读取量仍以上限封顶（多读 1 字节用来识别超限）。
    assert read_sizes == [9]


@pytest.mark.asyncio
async def test_trusted_local_missing_file_is_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    missing = tmp_path / "outside" / "missing.png"
    service = _service(root, tmp_path / "cache")

    # file: 形态直接 stat 失败；不存在的裸路径按内联处理，base64 解码失败。
    for reference in (missing.resolve().as_uri(), str(missing)):
        with pytest.raises(ReferenceResolveError):
            await service.ingest_inline([reference], requester={}, trusted_local=True)


@pytest.mark.asyncio
async def test_trusted_local_rejects_directory_and_non_image(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    text_file = outside / "secret.png"
    text_file.write_text("not an image", encoding="utf-8")
    service = _service(root, tmp_path / "cache")

    for reference in (outside.resolve().as_uri(), text_file.resolve().as_uri()):
        with pytest.raises(ReferenceResolveError):
            await service.ingest_inline([reference], requester={}, trusted_local=True)
    assert service._stores["tmp"]._entries == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_reference",
    [
        lambda path: path.as_uri().replace("file:///", "file://evil.example/", 1),
        lambda path: "file:relative.png",
    ],
    ids=["remote-host", "relative"],
)
async def test_trusted_local_rejects_non_local_file_uris(tmp_path, make_reference):
    root = tmp_path / "allowed"
    root.mkdir()
    # 真实存在的合法图片：URI 不是本机绝对路径时不能因为文件可读而放行。
    path, _ = _outside_png(tmp_path)
    service = _service(root, tmp_path / "cache")

    with pytest.raises(ReferenceResolveError):
        await service.ingest_inline(
            [make_reference(path.resolve())], requester={}, trusted_local=True
        )
    assert service._stores["tmp"]._entries == {}


@pytest.mark.asyncio
async def test_trusted_local_file_uri_uses_shared_parser_for_host_and_encoding(
    tmp_path,
):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "参考 图 #1.png"
    payload = _png_bytes(color=(8, 8, 8))
    path.write_bytes(payload)
    service = _service(root, tmp_path / "cache")
    uri = path.resolve().as_uri()
    requester = {"user_id": "u"}
    assert "%" in uri  # 空格、中文与 # 都经百分号编码

    prepared = await service.ingest_inline(
        [uri, uri.replace("file:///", "file://localhost/", 1)],
        requester=requester,
        trusted_local=True,
    )

    artifacts = await _collect(service, prepared, requester=requester)
    assert [Path(a.location).read_bytes() for a in artifacts] == [payload, payload]


@pytest.mark.asyncio
async def test_trusted_local_requires_absolute_local_path(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "rel.png").write_bytes(_png_bytes())
    monkeypatch.chdir(tmp_path)
    service = _service(root, tmp_path / "cache")

    with pytest.raises(ReferenceResolveError, match="绝对路径"):
        await service.ingest_inline(["rel.png"], requester={}, trusted_local=True)
