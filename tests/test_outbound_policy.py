"""出站 URL 基本校验与归档下载的重定向边界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_web_studio_service import _config
from tl.core.errors import PathPolicyError
from tl.core.path_policy import check_remote_url
from tl.generation_tracker import GenerationTracker
from tl.web_studio_service import WebStudioService


@pytest.mark.parametrize(
    "url",
    [
        "ftp://93.184.216.34/img.png",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///img.png",
        "https://user:pass@example.com/a.png",
    ],
)
def test_outbound_rejects_non_http_and_userinfo(url) -> None:
    with pytest.raises(PathPolicyError):
        check_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/img.png",
        "https://[2606:4700::1111]/img.png",
        "HTTP://93.184.216.34/img.png",
        "https://example.com/img.png?token=secret",
        "http://127.0.0.1:8080/img.png",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_outbound_accepts_http_urls_without_userinfo(url) -> None:
    assert check_remote_url(url) == url


def test_outbound_url_is_stripped() -> None:
    assert check_remote_url("  https://example.com/a.png  ") == (
        "https://example.com/a.png"
    )


@pytest.mark.asyncio
async def test_archive_redirect_to_non_http_is_not_followed(tmp_path, monkeypatch):
    import tl.web_studio_service as module

    requested = []

    class Response:
        status = 302
        headers = {"Location": "file:///etc/passwd"}
        content_length = None
        content = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            requested.append(url)
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(None, tracker, _config(), tmp_path)
    try:
        with pytest.raises(PathPolicyError):
            await service._download_remote_image("https://example.com/start.png")
        assert requested == ["https://example.com/start.png"]
    finally:
        await tracker.close()
