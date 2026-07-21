from __future__ import annotations

import pytest

from tl.api.dashscope import DashScopeProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_hooks import normalize_dashscope_settings


class _FakeClient:
    dashscope_settings: dict = {}

    def __init__(self, download_error: bool = False) -> None:
        self.normalized: list[tuple[str, str]] = []
        self.downloaded: list[str] = []
        self.download_error = download_error

    async def _normalize_reference_image_input(
        self, image_input: str, *, image_input_mode: str = "force_base64"
    ) -> tuple[str, str]:
        self.normalized.append((image_input, image_input_mode))
        return "image/png", "BASE64"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None

    async def _download_image(self, url, session, use_cache=False, proxy=None):  # noqa: ANN001
        self.downloaded.append(url)
        if self.download_error:
            raise RuntimeError("boom")
        return url, f"/tmp/dashscope_{len(self.downloaded)}.png"


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "dashscope",
        "api_key": "test-key",
        "resolution": "2K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "wan2.7-image-pro"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


@pytest.mark.asyncio
async def test_dashscope_text_to_image_payload() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(client=_FakeClient(), config=_make_config())

    assert request.url == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.payload["model"] == "wan2.7-image-pro"
    assert request.payload["input"]["messages"][0]["role"] == "user"
    assert request.payload["input"]["messages"][0]["content"] == [
        {"text": "draw a cat"}
    ]
    params = request.payload["parameters"]
    assert params["size"] == "2048*2048"
    assert params["n"] == 1
    assert params["watermark"] is False
    assert params["thinking_mode"] is True
    assert "prompt_extend" not in params
    assert "negative_prompt" not in params
    assert "enable_sequential" not in params


@pytest.mark.asyncio
async def test_dashscope_size_16_9_2k() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(aspect_ratio="16:9")
    )
    assert request.payload["parameters"]["size"] == "2688*1536"


@pytest.mark.asyncio
async def test_dashscope_size_9_16_4k() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(resolution="4K", aspect_ratio="9:16")
    )
    assert request.payload["parameters"]["size"] == "2304*4096"


@pytest.mark.asyncio
async def test_dashscope_size_3_2_computed() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(aspect_ratio="3:2")
    )
    size = request.payload["parameters"]["size"]
    w, h = (int(part) for part in size.split("*"))
    assert w % 16 == 0 and h % 16 == 0
    assert w > h


@pytest.mark.asyncio
async def test_dashscope_custom_size_pixels() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "size_mode": "custom",
                "custom_size": "1024x768",
            }
        ),
    )
    assert request.payload["parameters"]["size"] == "1024*768"


@pytest.mark.asyncio
async def test_dashscope_custom_size_shorthand() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "size_mode": "custom",
                "custom_size": "4k",
            }
        ),
    )
    assert request.payload["parameters"]["size"] == "4K"


@pytest.mark.asyncio
async def test_dashscope_endpoint_mode_token_plan() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "endpoint_mode": "token_plan",
            }
        ),
    )
    assert request.url == (
        "https://token-plan.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )


@pytest.mark.asyncio
async def test_dashscope_explicit_api_base_overrides_endpoint_mode() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "endpoint_mode": "token_plan",
                "api_base": "https://my-proxy.example.com/api/v1",
            }
        ),
    )
    assert request.url == (
        "https://my-proxy.example.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )


@pytest.mark.asyncio
async def test_dashscope_unknown_endpoint_mode_falls_back_to_default() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "endpoint_mode": "bogus",
            }
        ),
    )
    assert request.url.startswith("https://dashscope.aliyuncs.com/")


@pytest.mark.asyncio
async def test_dashscope_suppress_resolution_omits_size() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(suppress_resolution=True)
    )
    assert "size" not in request.payload["parameters"]


@pytest.mark.asyncio
async def test_dashscope_wan27_drops_negative_prompt() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "negative_prompt": "blurry",
            }
        ),
    )
    assert "negative_prompt" not in request.payload["parameters"]


@pytest.mark.asyncio
async def test_dashscope_qwen_keeps_negative_prompt() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "qwen-image-2.0",
                "negative_prompt": "blurry",
            }
        ),
    )
    params = request.payload["parameters"]
    assert params["negative_prompt"] == "blurry"
    assert params["prompt_extend"] is False
    assert "thinking_mode" not in params


@pytest.mark.asyncio
async def test_dashscope_wan27_sequential_mode() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "enable_sequential": True,
                "n": 20,
            }
        ),
    )
    params = request.payload["parameters"]
    assert params["enable_sequential"] is True
    assert params["n"] == 12
    assert "thinking_mode" not in params


@pytest.mark.asyncio
async def test_dashscope_qwen_max_clamps_n_to_1() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(provider_settings={"model": "qwen-image-max", "n": 4}),
    )
    assert request.payload["parameters"]["n"] == 1


@pytest.mark.asyncio
async def test_dashscope_reference_image_converted_to_data_uri() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            reference_images=["/tmp/local_photo.jpg"],
            image_input_mode="force_base64",
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    assert content == [
        {"text": "draw a cat"},
        {"image": "data:image/png;base64,BASE64"},
    ]


@pytest.mark.asyncio
async def test_dashscope_bare_local_path_normalized_as_file_uri(
    tmp_path,  # noqa: ANN001
) -> None:
    """裸本地路径应转 file:// URI 后再交客户端归一化（共享归一化器不认裸路径）。"""
    image_file = tmp_path / "photo.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    provider = DashScopeProvider()
    client = _FakeClient()
    await provider.build_request(
        client=client,
        config=_make_config(
            reference_images=[str(image_file)],
            image_input_mode="force_base64",
        ),
    )
    assert client.normalized == [(image_file.resolve().as_uri(), "force_base64")]


@pytest.mark.asyncio
async def test_dashscope_reference_images_capped_at_nine() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            reference_images=[f"/tmp/photo_{i}.jpg" for i in range(10)],
            image_input_mode="force_base64",
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    image_items = [item for item in content if "image" in item]
    assert len(image_items) == 9


@pytest.mark.asyncio
async def test_dashscope_reference_url_passthrough_when_not_forcing_b64() -> None:
    provider = DashScopeProvider()
    client = _FakeClient()
    request = await provider.build_request(
        client=client,
        config=_make_config(
            reference_images=["https://example.com/a.png"],
            image_input_mode="url",
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    assert content[1] == {"image": "https://example.com/a.png"}
    assert client.normalized == []


@pytest.mark.asyncio
async def test_dashscope_reference_url_normalized_when_forcing_b64() -> None:
    provider = DashScopeProvider()
    client = _FakeClient()
    request = await provider.build_request(
        client=client,
        config=_make_config(
            reference_images=["https://example.com/a.png"],
            image_input_mode="force_base64",
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    assert content[1] == {"image": "data:image/png;base64,BASE64"}
    assert client.normalized == [("https://example.com/a.png", "force_base64")]


@pytest.mark.asyncio
async def test_dashscope_parse_response_accepts_framework_is_retry_kwarg() -> None:
    """parse_errors_with_provider=True 时框架会注入 is_retry（tl/tl_api.py:1270）。"""
    provider = DashScopeProvider()
    client = _FakeClient()
    image_urls, _, _, _ = await provider.parse_response(
        client=client,
        response_data=_success_response(["https://oss.example.com/a.png"]),
        session=None,
        api_base=None,
        http_status=200,
        request_config=None,
        is_retry=False,
    )
    assert image_urls == ["/tmp/dashscope_1.png"]


def _success_response(urls: list[str]) -> dict:
    return {
        "status_code": 200,
        "code": "",
        "message": "",
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [{"image": url} for url in urls],
                    },
                }
            ]
        },
        "usage": {"image_count": len(urls)},
    }


@pytest.mark.asyncio
async def test_dashscope_parse_downloads_all_urls() -> None:
    provider = DashScopeProvider()
    client = _FakeClient()
    image_urls, image_paths, text_content, thought = await provider.parse_response(
        client=client,
        response_data=_success_response(
            ["https://oss.example.com/a.png", "https://oss.example.com/b.png"]
        ),
        session=None,
        http_status=200,
    )
    assert client.downloaded == [
        "https://oss.example.com/a.png",
        "https://oss.example.com/b.png",
    ]
    assert image_urls == image_paths == ["/tmp/dashscope_1.png", "/tmp/dashscope_2.png"]
    assert text_content is None
    assert thought is None


@pytest.mark.asyncio
async def test_dashscope_parse_dedupes_urls() -> None:
    provider = DashScopeProvider()
    client = _FakeClient()
    image_urls, image_paths, _, _ = await provider.parse_response(
        client=client,
        response_data=_success_response(
            ["https://oss.example.com/a.png", "https://oss.example.com/a.png"]
        ),
        session=None,
        http_status=200,
    )
    assert client.downloaded == ["https://oss.example.com/a.png"]
    assert image_urls == ["/tmp/dashscope_1.png"]


@pytest.mark.asyncio
async def test_dashscope_parse_falls_back_to_remote_url_on_download_error() -> None:
    provider = DashScopeProvider()
    client = _FakeClient(download_error=True)
    image_urls, image_paths, _, _ = await provider.parse_response(
        client=client,
        response_data=_success_response(["https://oss.example.com/a.png"]),
        session=None,
        http_status=200,
    )
    assert image_urls == ["https://oss.example.com/a.png"]
    assert image_paths == []


@pytest.mark.asyncio
async def test_dashscope_parse_throttling_is_retryable() -> None:
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"code": "Throttling", "message": "限流"},
            session=None,
            http_status=429,
        )
    assert excinfo.value.retryable is True
    assert excinfo.value.error_code == "Throttling"


@pytest.mark.asyncio
async def test_dashscope_parse_invalid_api_key_defers_to_framework() -> None:
    """认证类错误码 retryable=None，交框架在多 Key 时轮换重试。"""
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"code": "InvalidApiKey", "message": "无效 Key"},
            session=None,
            http_status=401,
        )
    assert excinfo.value.retryable is None
    assert excinfo.value.error_code == "InvalidApiKey"


@pytest.mark.asyncio
async def test_dashscope_parse_unknown_code_defers_to_framework() -> None:
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"code": "SomethingNew", "message": "未知"},
            session=None,
            http_status=504,
        )
    assert excinfo.value.retryable is None


@pytest.mark.asyncio
async def test_dashscope_parse_data_inspection_not_retryable() -> None:
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"code": "DataInspectionFailed", "message": "内容拦截"},
            session=None,
            http_status=400,
        )
    assert excinfo.value.retryable is False
    assert excinfo.value.error_code == "DataInspectionFailed"


@pytest.mark.asyncio
async def test_dashscope_parse_empty_choices_raises_no_image() -> None:
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"output": {"choices": []}},
            session=None,
            http_status=200,
        )
    assert excinfo.value.error_type == "no_image"
    assert excinfo.value.retryable is False


def test_normalize_dashscope_settings_converts_and_clamps() -> None:
    settings: dict = {"custom_size": "1024×768", "size_mode": "custom", "n": "20"}
    normalize_dashscope_settings(settings)
    assert settings["custom_size"] == "1024*768"
    assert settings["n"] == 12
    assert settings["size_mode"] == "custom"


def test_normalize_dashscope_settings_bad_size_mode_falls_back() -> None:
    settings: dict = {"size_mode": "bad"}
    normalize_dashscope_settings(settings)
    assert settings["size_mode"] == "preset"


def test_normalize_dashscope_settings_shorthand_uppercased() -> None:
    settings: dict = {"size_mode": "custom", "custom_size": "2k"}
    normalize_dashscope_settings(settings)
    assert settings["custom_size"] == "2K"


def test_normalize_dashscope_settings_asterisk_default_preserved() -> None:
    settings: dict = {"size_mode": "custom", "custom_size": "2048*2048"}
    normalize_dashscope_settings(settings)
    assert settings["custom_size"] == "2048*2048"


def test_normalize_dashscope_settings_endpoint_mode_defaults() -> None:
    settings: dict = {}
    normalize_dashscope_settings(settings)
    assert settings["endpoint_mode"] == "dashscope"


def test_normalize_dashscope_settings_endpoint_mode_token_plan() -> None:
    settings: dict = {"endpoint_mode": "Token_Plan"}
    normalize_dashscope_settings(settings)
    assert settings["endpoint_mode"] == "token_plan"


def test_normalize_dashscope_settings_bad_endpoint_mode_falls_back() -> None:
    settings: dict = {"endpoint_mode": "bogus"}
    normalize_dashscope_settings(settings)
    assert settings["endpoint_mode"] == "dashscope"


def test_normalize_dashscope_settings_non_numeric_n_falls_back_to_1() -> None:
    settings: dict = {"n": "abc"}
    normalize_dashscope_settings(settings)
    assert settings["n"] == 1


def test_normalize_dashscope_settings_out_of_range_custom_size_preserved() -> None:
    settings: dict = {"size_mode": "custom", "custom_size": "8192x8192"}
    normalize_dashscope_settings(settings)
    assert settings["custom_size"] == "8192x8192"
