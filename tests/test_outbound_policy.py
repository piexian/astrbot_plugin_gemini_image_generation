"""WebUI 出站地址、模型目录与重定向安全边界。"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest

from tests.test_web_studio_service import _config, _png
from tl.core.errors import PathPolicyError
from tl.generation_tracker import GenerationTracker
from tl.model_catalog import (
    ModelCatalogError,
    ModelCatalogService,
    make_catalog_request,
)
from tl.outbound_policy import check_outbound_url
from tl.web_studio_service import WebStudioService


def resolver_for(*addresses):
    values = tuple(ipaddress.ip_address(value) for value in addresses)
    return lambda host: values


@pytest.mark.asyncio
async def test_public_outbound_url_is_allowed():
    url = "https://images.example.com/result.png?token=secret"
    assert await check_outbound_url(url, resolver=resolver_for("93.184.216.34")) == url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address", ["169.254.1.2", "169.254.169.254", "fd00:ec2::254", "100.100.100.200"]
)
async def test_link_local_and_metadata_are_always_rejected(address):
    with pytest.raises(PathPolicyError, match="link-local|元数据"):
        await check_outbound_url(
            "http://127.0.0.1:8080/image",
            saved_api_bases=["http://127.0.0.1:8080"],
            resolver=resolver_for(address),
        )


@pytest.mark.asyncio
async def test_private_url_requires_matching_saved_origin():
    url = "http://127.0.0.1:8080/v1/images"
    with pytest.raises(PathPolicyError, match="先保存到供应商配置"):
        await check_outbound_url(url, resolver=resolver_for("127.0.0.1"))
    assert (
        await check_outbound_url(
            url,
            saved_api_bases=["http://127.0.0.1:8080"],
            resolver=resolver_for("127.0.0.1"),
        )
        == url
    )


@pytest.mark.asyncio
async def test_dns_private_result_is_rejected_even_for_public_hostname():
    with pytest.raises(PathPolicyError, match="先保存到供应商配置"):
        await check_outbound_url(
            "https://public-name.example/image",
            resolver=resolver_for("93.184.216.34", "10.0.0.8"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["file:///tmp/a.png", "https://user:pass@example.com/a"]
)
async def test_outbound_rejects_non_http_and_userinfo(url):
    with pytest.raises(PathPolicyError):
        await check_outbound_url(url, resolver=resolver_for("93.184.216.34"))


@pytest.mark.asyncio
async def test_model_catalog_rejects_url_before_network():
    async def reject(url):
        raise PathPolicyError("目标被拒绝")

    service = ModelCatalogService(url_checker=reject)
    request = make_catalog_request("openai", "https://example.com/v1", "key")
    try:
        with pytest.raises(ModelCatalogError) as caught:
            await service.fetch(request)
        assert caught.value.status_code == 400
        assert caught.value.reason == "url_rejected"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_archive_redirect_to_private_address_is_not_followed(
    tmp_path, monkeypatch
):
    import tl.web_studio_service as module

    checked = []
    requested = []

    async def check(url, **kwargs):
        checked.append(url)
        if url.endswith("private.png"):
            raise PathPolicyError("内网目标被拒绝")

    monkeypatch.setattr(module, "check_outbound_url", check)
    content = _png(tmp_path / "input.png").read_bytes()

    class Response:
        status = 302
        headers = {"Location": "http://127.0.0.1/private.png"}
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
        with pytest.raises(PathPolicyError, match="内网目标"):
            await service._download_remote_image("https://example.com/start.png")
        assert checked == [
            "https://example.com/start.png",
            "http://127.0.0.1/private.png",
        ]
        assert requested == ["https://example.com/start.png"]
        assert content
    finally:
        await tracker.close()
