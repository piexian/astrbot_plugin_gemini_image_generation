from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def _load_tl_utils(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[1]
    module_name = "tl._qq_official_avatar_tl_utils"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "tl" / "tl_utils.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


class _Response:
    status = 200
    reason = "OK"
    headers = {"Content-Type": "image/jpeg"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self) -> bytes:
        return b"\xff\xd8\xff" + b"avatar" * 200


class _Session:
    def __init__(self) -> None:
        self.url: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, **kwargs):
        self.url = url
        return _Response()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform_name",
    ["qq_official", "qq_official_webhook"],
)
async def test_download_qq_official_avatar_uses_appid_and_openid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform_name: str,
) -> None:
    module = _load_tl_utils(monkeypatch)
    session = _Session()
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: session)

    event = SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=None, sender=None),
        bot=SimpleNamespace(platform=SimpleNamespace(appid="1234567890")),
        get_platform_name=lambda: platform_name,
    )
    openid = "openid_abcdefghijklmnopqrstuvwxyz"

    result = await module.download_qq_avatar(
        openid,
        "official-avatar",
        images_dir=tmp_path,
        event=event,
    )

    assert session.url == (f"https://q.qlogo.cn/qqapp/1234567890/{openid}/100")
    assert result is not None
    assert base64.b64decode(result.split(",", 1)[1]).startswith(b"\xff\xd8\xff")


def test_qq_official_avatar_url_ignores_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tl_utils(monkeypatch)
    event = SimpleNamespace(
        bot=SimpleNamespace(platform=SimpleNamespace(appid="1234567890")),
        get_platform_name=lambda: "aiocqhttp",
    )

    assert module._build_qq_official_avatar_url(event, "123456789") is None


def test_metadata_only_declares_tested_qq_official_transport() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))

    assert "qq_official" in metadata["support_platforms"]
    assert "qq_official_webhook" not in metadata["support_platforms"]
