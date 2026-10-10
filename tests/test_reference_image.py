from __future__ import annotations

import base64
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


@pytest.mark.asyncio
async def test_normalize_reference_image_input_base64_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)

    mime_type, encoded = await module.normalize_reference_image_input(
        "not-a-valid-base64-string!!!",
        image_cache_dir=tmp_path,
    )

    assert mime_type is None
    assert encoded is None


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
async def test_normalize_reference_image_input_decodes_bare_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)
    raw_png = b"\x89PNG\r\n\x1a\n" + b"bare-path-bytes"
    image_path = tmp_path / "bare image.png"
    image_path.write_bytes(raw_png)

    mime_type, encoded = await module.normalize_reference_image_input(
        str(image_path),
        image_cache_dir=tmp_path,
    )

    assert mime_type == "image/png"
    assert base64.b64decode(encoded) == raw_png


@pytest.mark.asyncio
async def test_normalize_reference_image_input_missing_bare_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)

    result = await module.normalize_reference_image_input(
        str(tmp_path / "missing.png"),
        image_cache_dir=tmp_path,
    )

    assert result == (None, None)


@pytest.mark.asyncio
async def test_normalize_reference_image_input_overlong_path_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_reference_image(monkeypatch)

    result = await module.normalize_reference_image_input(
        "/" + "x" * 5000,
        image_cache_dir=tmp_path,
    )

    assert result == (None, None)
