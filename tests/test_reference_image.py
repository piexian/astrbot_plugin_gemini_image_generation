from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_reference_image(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[1]

    fake_pil = types.ModuleType("PIL")
    fake_pil.__path__ = []
    fake_image = types.ModuleType("PIL.Image")

    def fail_open(*args, **kwargs):
        raise AssertionError("supported reference image bytes should not be re-encoded")

    fake_image.open = fail_open
    fake_pil.Image = fake_image
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image)

    module_name = "real_reference_image"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "tl" / "reference_image.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_reference_image_bytes_skip_pillow_reencode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_reference_image(monkeypatch)
    raw_png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"

    mime_type, encoded = module.coerce_reference_image_bytes(
        "image/png",
        raw_png,
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png


def test_reference_image_bytes_use_detected_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_reference_image(monkeypatch)
    raw_png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"

    mime_type, encoded = module.coerce_reference_image_bytes(
        "image/jpeg",
        raw_png,
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png


@pytest.mark.asyncio
async def test_normalize_reference_image_input_coerces_cached_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)
    url = "https://cdn.example/image.gif"
    raw_png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    (tmp_path / f"{cache_key}.gif").write_bytes(raw_png)

    mime_type, encoded = await module.normalize_reference_image_input(
        url,
        image_cache_dir=tmp_path,
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png


@pytest.mark.asyncio
async def test_normalize_reference_image_input_decodes_file_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)
    raw_png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    image_path = tmp_path / "image with space.png"
    image_path.write_bytes(raw_png)

    mime_type, encoded = await module.normalize_reference_image_input(
        image_path.as_uri(),
        image_cache_dir=tmp_path,
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png


@pytest.mark.asyncio
async def test_normalize_reference_image_input_uses_supplied_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)
    raw_png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"

    class _Response:
        status = 200
        reason = "OK"
        headers = {"Content-Type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def read(self) -> bytes:
            return raw_png

    class _Session:
        closed = False

        def __init__(self) -> None:
            self.kwargs = None

        def get(self, *args, **kwargs):
            self.kwargs = kwargs
            return _Response()

    session = _Session()

    mime_type, encoded = await module.normalize_reference_image_input(
        "https://cdn.example/image.png",
        image_cache_dir=tmp_path,
        session=session,
        proxy="http://proxy.local:8080",
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png
    assert session.kwargs["proxy"] == "http://proxy.local:8080"


@pytest.mark.asyncio
async def test_normalize_reference_image_input_logs_http_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)

    class _Logger:
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def debug(self, *args, **kwargs) -> None:
            return None

        def warning(self, message: str, *args, **kwargs) -> None:
            self.warnings.append(str(message))

    class _Response:
        status = 404
        reason = "Not Found"
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        closed = False

        def get(self, *args, **kwargs):
            return _Response()

    logger = _Logger()
    monkeypatch.setattr(module, "logger", logger)

    mime_type, encoded = await module.normalize_reference_image_input(
        "https://cdn.example/missing.png",
        image_cache_dir=tmp_path,
        session=_Session(),
    )

    assert mime_type is None
    assert encoded is None
    assert any("HTTP 404 Not Found" in message for message in logger.warnings)
