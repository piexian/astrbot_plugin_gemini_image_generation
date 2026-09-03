"""tests for tl/api/modelscope.py — 异步任务制 provider 的构建/轮询/门控"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tl.api.modelscope import ModelScopeProvider, _resolve_size
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_capability
from tl.provider_hooks import modelscope_edit_capability

_DATA_URI = "data:image/png;base64,QUJD"


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "画一只猫",
        "api_type": "modelscope",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "Qwen/Qwen-Image"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


class _FakeClient:
    def __init__(self, *, proxy: bool = False, download_path: str | None = None):
        self._proxy = proxy
        self._download_path = download_path
        self.download_calls: list[str] = []

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return self._proxy

    def _request_http_proxy(self, request_config) -> str | None:  # noqa: ANN001
        return "http://127.0.0.1:7890" if self._proxy else None

    async def _download_image(self, image_url, session, **kwargs):  # noqa: ANN001, ANN003
        self.download_calls.append(image_url)
        if self._download_path is None:
            raise RuntimeError("download boom")
        return None, self._download_path


class _FakePollResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakePollResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """按顺序返回响应体；最后一个响应体会被重复返回。"""

    def __init__(self, bodies: list):
        self._bodies = bodies
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakePollResponse:
        self.calls.append({"url": url, **kwargs})
        item = self._bodies.pop(0) if len(self._bodies) > 1 else self._bodies[0]
        # 元素可为 (http_status, body) 元组，用于模拟非 200 轮询响应
        status, body = item if isinstance(item, tuple) else (200, item)
        return _FakePollResponse(body, status=status)


def _task_body(status: str, **extra: Any) -> str:
    return json.dumps({"task_status": status, **extra})


# ---------------------------------------------------------------------------
# build_request：payload 构建
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2i_payload_and_async_headers() -> None:
    provider = ModelScopeProvider()
    request = await provider.build_request(client=object(), config=_make_config())
    assert request.url == ("https://api-inference.modelscope.cn/v1/images/generations")
    assert request.headers["X-ModelScope-Async-Mode"] == "true"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.payload["model"] == "Qwen/Qwen-Image"
    assert request.payload["prompt"] == "画一只猫"
    assert request.payload["size"] == "1024x1024"
    # 可选参数缺省不传
    for key in ("negative_prompt", "seed", "steps", "guidance", "loras", "image_url"):
        assert key not in request.payload


@pytest.mark.asyncio
async def test_api_base_override_strips_trailing_slash() -> None:
    provider = ModelScopeProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(api_base="https://relay.example.com/"),
    )
    assert request.url == "https://relay.example.com/v1/images/generations"


@pytest.mark.asyncio
async def test_api_base_strips_v1_suffix() -> None:
    # 官方文档示例 base 带 /v1 后缀，照抄不得拼出 /v1/v1
    provider = ModelScopeProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(api_base="https://api-inference.modelscope.cn/v1/"),
    )
    assert request.url == "https://api-inference.modelscope.cn/v1/images/generations"


@pytest.mark.asyncio
async def test_optional_params_sent_and_clamped() -> None:
    provider = ModelScopeProvider()
    config = _make_config(
        provider_settings={
            "model": "Qwen/Qwen-Image",
            "negative_prompt": "低画质",
            "seed": 2**31,
            "steps": 150,
            "guidance": "4.5",
            "loras": "user/my-lora",
        },
    )
    request = await provider.build_request(client=object(), config=config)
    assert request.payload["negative_prompt"] == "低画质"
    assert request.payload["seed"] == 2**31 - 1
    assert request.payload["steps"] == 100
    assert request.payload["guidance"] == 4.5
    assert request.payload["loras"] == "user/my-lora"


@pytest.mark.asyncio
async def test_zero_optional_params_omitted() -> None:
    provider = ModelScopeProvider()
    config = _make_config(
        provider_settings={
            "model": "Qwen/Qwen-Image",
            "seed": 0,
            "steps": 0,
            "guidance": 0,
        },
    )
    request = await provider.build_request(client=object(), config=config)
    for key in ("seed", "steps", "guidance"):
        assert key not in request.payload


@pytest.mark.asyncio
async def test_loras_json_dict_parsed_and_invalid_ignored() -> None:
    provider = ModelScopeProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={
                "model": "Qwen/Qwen-Image",
                "loras": '{"user/a":0.6,"user/b":0.4}',
            }
        ),
    )
    assert request.payload["loras"] == {"user/a": 0.6, "user/b": 0.4}

    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={"model": "Qwen/Qwen-Image", "loras": "{bad json"}
        ),
    )
    assert "loras" not in request.payload


@pytest.mark.asyncio
async def test_suppress_resolution_omits_size() -> None:
    provider = ModelScopeProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(suppress_resolution=True),
    )
    assert "size" not in request.payload


@pytest.mark.asyncio
async def test_prompt_over_2000_fails_fast() -> None:
    provider = ModelScopeProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=object(), config=_make_config(prompt="a" * 2001)
        )
    assert getattr(exc_info.value, "retryable", True) is False


# ---------------------------------------------------------------------------
# 编辑门控与参考图
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_model_with_reference_sends_image_url_list() -> None:
    provider = ModelScopeProvider()
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image-Edit"},
        reference_images=[_DATA_URI],
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert request.payload["image_url"] == [_DATA_URI]


@pytest.mark.asyncio
async def test_non_edit_model_with_reference_raises_non_retryable() -> None:
    provider = ModelScopeProvider()
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image"},
        reference_images=[_DATA_URI],
    )
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(client=_FakeClient(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False


def test_modelscope_edit_capability_gates_by_model() -> None:
    assert modelscope_edit_capability({"model": "Qwen/Qwen-Image-Edit"})
    assert not modelscope_edit_capability({"model": "Qwen/Qwen-Image"})
    assert not modelscope_edit_capability({})


# ---------------------------------------------------------------------------
# 尺寸换算与模型族钳制
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_bounds"),
    [
        ("Qwen/Qwen-Image-Edit", (64, 1664)),
        ("Z-Image-Turbo", (512, 2048)),
        ("black-forest-labs/FLUX.1-schnell", (64, 1024)),
        ("stabilityai/sd3.5-large", (64, 2048)),
        ("stabilityai/sdxl-base", (64, 2048)),
        ("MAILAND/majicflus_v1", (64, 2048)),
        ("totally-new-model", (512, 1024)),
    ],
)
def test_model_family_bounds(model: str, expected_bounds: tuple[int, int]) -> None:
    from tl.api.modelscope import _model_bounds

    assert _model_bounds(model) == expected_bounds


@pytest.mark.asyncio
async def test_size_clamped_per_model_family() -> None:
    # Qwen-Image 上限 1664：2K 16:9 宽触顶后按比例重算
    assert (
        _resolve_size(resolution="2K", aspect_ratio="16:9", model="Qwen/Qwen-Image")
        == "1664x936"
    )
    # Z-Image 下限 512：1K 21:9 高触底后按比例重算
    size = _resolve_size(resolution="1K", aspect_ratio="21:9", model="Z-Image-Turbo")
    width, height = (int(part) for part in size.split("x"))
    assert 512 <= width <= 2048 and 512 <= height <= 2048
    # 未知模型 2K 保守档触顶
    assert (
        _resolve_size(resolution="2K", aspect_ratio="1:1", model="totally-new-model")
        == "1024x1024"
    )


def test_decimal_aspect_ratio_parsed_as_float() -> None:
    # 全局比例并集含 19.5:9 等小数比例，不得因 int 解析失败静默回退 1:1
    size = _resolve_size(
        resolution="1K", aspect_ratio="19.5:9", model="Qwen/Qwen-Image"
    )
    width, height = (int(part) for part in size.split("x"))
    assert abs(width / height - 19.5 / 9) < 0.02


# ---------------------------------------------------------------------------
# parse_response：非 200 / 提交校验 / 轮询
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_200_error_body_parsed_with_framework_retryable() -> None:
    provider = ModelScopeProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=object(),
            response_data={"errors": {"message": "Invalid model"}},
            session=_FakeSession([]),
            http_status=400,
        )
    assert "Invalid model" in exc_info.value.message
    assert exc_info.value.status_code == 400
    # retryable=None 交框架通用判断（429/5xx 重试）
    assert exc_info.value.retryable is None


@pytest.mark.asyncio
async def test_missing_task_id_is_invalid_response() -> None:
    provider = ModelScopeProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=object(),
            response_data={"task_status": "PENDING"},
            session=_FakeSession([]),
            http_status=200,
        )
    assert exc_info.value.error_type == "invalid_response"


@pytest.mark.asyncio
async def test_poll_success_returns_output_urls() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession(
        [
            _task_body("Pending"),
            _task_body("SUCCEED", output_images=["https://img.example/a.png"]),
        ]
    )
    client = _FakeClient()
    config = _make_config(
        provider_settings={
            "model": "Qwen/Qwen-Image",
            "poll_interval": 0.001,
            "poll_timeout": 5,
        }
    )
    urls, paths, text, thought = await provider.parse_response(
        client=client,
        response_data={"task_id": "task-1"},
        session=session,
        http_status=200,
        request_config=config,
    )
    assert (urls, paths, text, thought) == (
        ["https://img.example/a.png"],
        [],
        None,
        None,
    )
    assert session.calls[0]["url"].endswith("/v1/tasks/task-1")
    assert session.calls[0]["headers"]["X-ModelScope-Task-Type"] == "image_generation"


@pytest.mark.asyncio
async def test_poll_failed_raises_non_retryable_with_server_message() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession([_task_body("FAILED", message="NSFW detected")])
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image", "poll_interval": 0.001}
    )
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"task_id": "task-1"},
            session=session,
            http_status=200,
            request_config=config,
        )
    assert "NSFW detected" in exc_info.value.message
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_poll_timeout_raises_retryable_timeout() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession([_task_body("Running")])
    config = _make_config(
        provider_settings={
            "model": "Qwen/Qwen-Image",
            "poll_interval": 0.001,
            "poll_timeout": 0.05,
        }
    )
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"task_id": "task-1"},
            session=session,
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.error_type == "timeout"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_poll_auth_error_fails_fast_non_retryable() -> None:
    # 401/403 空转到超时只会白扣魔粒，须立即失败且不可重试
    provider = ModelScopeProvider()
    session = _FakeSession([(401, json.dumps({"message": "unauthorized"}))])
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image", "poll_interval": 0.001}
    )
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"task_id": "task-1"},
            session=session,
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.error_type == "auth"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_output_images_downloaded_when_proxy_configured() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession(
        [_task_body("SUCCEED", output_images=["https://img.example/a.png"])]
    )
    client = _FakeClient(proxy=True, download_path="/tmp/a.png")
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image", "poll_interval": 0.001}
    )
    urls, paths, _, _ = await provider.parse_response(
        client=client,
        response_data={"task_id": "task-1"},
        session=session,
        http_status=200,
        request_config=config,
    )
    assert client.download_calls == ["https://img.example/a.png"]
    assert urls == ["/tmp/a.png"]
    assert paths == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_download_failure_falls_back_to_direct_url() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession(
        [_task_body("SUCCEED", output_images=["https://img.example/a.png"])]
    )
    client = _FakeClient(proxy=True, download_path=None)
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image", "poll_interval": 0.001}
    )
    urls, paths, _, _ = await provider.parse_response(
        client=client,
        response_data={"task_id": "task-1"},
        session=session,
        http_status=200,
        request_config=config,
    )
    assert urls == ["https://img.example/a.png"]
    assert paths == []


# ---------------------------------------------------------------------------
# capability profile
# ---------------------------------------------------------------------------


class _Candidate:
    def __init__(self, *, model: str, supports_edit: bool = True):
        self.api_type = "modelscope"
        self.model = model
        self.supports_image_edit = supports_edit
        self.settings = {"model": model}


def test_modelscope_capability_profile() -> None:
    cap = candidate_capability(_Candidate(model="Qwen/Qwen-Image-Edit"))
    assert cap["native_batch_limit"] == 1
    assert cap["parameters"]["resolution"]["enum"] == ["1K", "2K"]
    # negative_prompt 参数声明 + settings 映射；seed 仅声明不映射
    assert cap["request_setting_map"] == {"negative_prompt": "negative_prompt"}
    assert "negative_prompt" in cap["parameters"]
    assert "seed" in cap["parameters"]
    assert "seed" not in cap["request_setting_map"]


def test_modelscope_capability_modes_follow_edit_support() -> None:
    edit_cap = candidate_capability(_Candidate(model="Qwen/Qwen-Image-Edit"))
    t2i_cap = candidate_capability(
        _Candidate(model="Qwen/Qwen-Image", supports_edit=False)
    )
    assert "image_to_image" in edit_cap["generation_modes"]
    assert "image_to_image" not in t2i_cap["generation_modes"]


def test_modelscope_spec_flags() -> None:
    from tl.provider_metadata import get_provider_spec

    spec = get_provider_spec("modelscope")
    assert spec is not None
    assert spec.parse_errors_with_provider is True
    # 轮询须与提交同 Key：Key 轮换后重建请求以同步 config.api_key
    assert spec.rebuild_on_retry is True
