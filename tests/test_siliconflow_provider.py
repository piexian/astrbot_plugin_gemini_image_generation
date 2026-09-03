"""tests for tl/api/siliconflow.py — 同步单端点 provider 的构建/解析/门控"""

from __future__ import annotations

from typing import Any

import pytest

from tl.api.siliconflow import (
    SiliconFlowProvider,
    _model_family,
    _resolve_image_size,
)
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_capability
from tl.provider_hooks import siliconflow_edit_capability
from tl.provider_metadata import get_provider_spec

_DATA_URI = "data:image/png;base64,QUJD"


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "画一只猫",
        "api_type": "siliconflow",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "Qwen/Qwen-Image"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


class _FakeClient:
    def __init__(self, *, download_path: str | None = "/tmp/a.png"):
        self._download_path = download_path
        self.download_calls: list[tuple[str, dict[str, Any]]] = []

    def _request_http_proxy(self, request_config) -> str | None:  # noqa: ANN001
        return getattr(request_config, "proxy", None)

    async def _download_image(self, image_url, session, **kwargs):  # noqa: ANN001, ANN003
        self.download_calls.append((image_url, kwargs))
        if self._download_path is None:
            raise RuntimeError("download boom")
        return None, self._download_path


# ---------------------------------------------------------------------------
# 模型族分发
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("Qwen/Qwen-Image-Edit-2509", "qwen-image-edit-2509"),
        ("qwen/qwen-image-edit-2509", "qwen-image-edit-2509"),
        ("Qwen/Qwen-Image-Edit", "qwen-image-edit"),
        ("Qwen/Qwen-Image", "qwen-image"),
        ("Kwai-Kolors/Kolors", "kolors"),
        ("unknown/model", "unknown"),
    ],
)
def test_model_family_dispatch(model: str, expected: str) -> None:
    # edit 系列是 qwen-image 的超集子串，2509 必须先于 edit 命中
    assert _model_family(model) == expected


# ---------------------------------------------------------------------------
# build_request：各族 payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen_image_payload_with_preset_size() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(), config=_make_config(aspect_ratio="16:9")
    )
    assert request.url == "https://api.siliconflow.cn/v1/images/generations"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.payload["model"] == "Qwen/Qwen-Image"
    assert request.payload["image_size"] == "1664x928"
    # 非 Kolors 族不发送 Kolors 专属参数
    for key in ("batch_size", "guidance_scale", "num_inference_steps", "seed"):
        assert key not in request.payload


@pytest.mark.asyncio
async def test_kolors_full_params() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={
                "model": "Kwai-Kolors/Kolors",
                "batch_size": 4,
                "guidance_scale": 8.5,
                "num_inference_steps": 30,
                "seed": 42,
                "negative_prompt": "blurry",
            },
            aspect_ratio="3:4",
        ),
    )
    assert request.payload["model"] == "Kwai-Kolors/Kolors"
    assert request.payload["image_size"] == "768x1024"
    assert request.payload["batch_size"] == 4
    assert request.payload["guidance_scale"] == 8.5
    assert request.payload["num_inference_steps"] == 30
    assert request.payload["seed"] == 42
    assert request.payload["negative_prompt"] == "blurry"


@pytest.mark.asyncio
async def test_optional_params_clamped_or_omitted() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={
                "model": "Kwai-Kolors/Kolors",
                "batch_size": 99,
                "guidance_scale": 99,
                "num_inference_steps": 0,
                "seed": 12345678901,
            },
        ),
    )
    assert request.payload["batch_size"] == 4
    assert request.payload["guidance_scale"] == 20.0
    # 0 = 不传
    assert "num_inference_steps" not in request.payload
    assert request.payload["seed"] == 9999999999


@pytest.mark.asyncio
async def test_edit_model_omits_image_size() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(provider_settings={"model": "Qwen/Qwen-Image-Edit"}),
    )
    assert "image_size" not in request.payload


@pytest.mark.asyncio
async def test_suppress_resolution_omits_image_size() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(suppress_resolution=True),
    )
    assert "image_size" not in request.payload


@pytest.mark.asyncio
async def test_api_base_normalization() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(api_base="https://relay.example.com/v1/"),
    )
    assert request.url == "https://relay.example.com/v1/images/generations"


# ---------------------------------------------------------------------------
# 编辑门控与参考图键位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2509_three_reference_keys() -> None:
    provider = SiliconFlowProvider()
    refs = [_DATA_URI, _DATA_URI, _DATA_URI]
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={"model": "Qwen/Qwen-Image-Edit-2509"},
            reference_images=refs,
        ),
    )
    assert request.payload["image"] == _DATA_URI
    assert request.payload["image2"] == _DATA_URI
    assert request.payload["image3"] == _DATA_URI
    assert "image_size" not in request.payload


@pytest.mark.asyncio
async def test_classic_edit_truncates_to_single_reference() -> None:
    provider = SiliconFlowProvider()
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={"model": "Qwen/Qwen-Image-Edit"},
            reference_images=[_DATA_URI, _DATA_URI],
        ),
    )
    assert request.payload["image"] == _DATA_URI
    assert "image2" not in request.payload
    assert "image3" not in request.payload


@pytest.mark.asyncio
async def test_2509_reference_cap_clamped_to_three() -> None:
    provider = SiliconFlowProvider()
    refs = [_DATA_URI] * 5
    request = await provider.build_request(
        client=object(),
        config=_make_config(
            provider_settings={
                "model": "Qwen/Qwen-Image-Edit-2509",
                "max_reference_images": 99,
            },
            reference_images=refs,
        ),
    )
    assert "image3" in request.payload
    assert "image4" not in request.payload


@pytest.mark.asyncio
async def test_non_edit_model_with_reference_raises_non_retryable() -> None:
    provider = SiliconFlowProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=object(),
            config=_make_config(reference_images=[_DATA_URI]),
        )
    assert exc_info.value.error_type == "invalid_reference_image"
    assert getattr(exc_info.value, "retryable", True) is False


def test_siliconflow_edit_capability_gates_by_model() -> None:
    assert siliconflow_edit_capability({"model": "Qwen/Qwen-Image-Edit-2509"})
    assert siliconflow_edit_capability({"model": "Qwen/Qwen-Image-Edit"})
    assert not siliconflow_edit_capability({"model": "Qwen/Qwen-Image"})
    assert not siliconflow_edit_capability({})


# ---------------------------------------------------------------------------
# 尺寸映射：预设命中 / 未命中计算 / 钳制
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "aspect_ratio", "model", "expected"),
    [
        ("1K", "1:1", "Kwai-Kolors/Kolors", "1024x1024"),
        ("1K", "3:4", "Kwai-Kolors/Kolors", "768x1024"),
        ("2K", "3:4", "Kwai-Kolors/Kolors", "960x1280"),
        ("1K", "9:16", "Kwai-Kolors/Kolors", "720x1280"),
        ("1K", "1:2", "Kwai-Kolors/Kolors", "720x1440"),
        ("1K", "1:1", "Qwen/Qwen-Image", "1328x1328"),
        ("1K", "16:9", "Qwen/Qwen-Image", "1664x928"),
        ("2K", "16:9", "Qwen/Qwen-Image", "1664x928"),
    ],
)
def test_preset_size_mapping(
    resolution: str, aspect_ratio: str, model: str, expected: str
) -> None:
    assert (
        _resolve_image_size(
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            family=_model_family(model),
        )
        == expected
    )


def test_unmatched_ratio_computed_and_clamped() -> None:
    # Kolors 21:9 无预设：1K 预算本地计算，宽触顶 1440 后按比例重算高
    size = _resolve_image_size(resolution="1K", aspect_ratio="21:9", family="kolors")
    width, height = (int(part) for part in size.split("x"))
    assert width == 1440 and 512 <= height < 1440
    assert abs(width / height - 21 / 9) < 0.02


def test_decimal_ratio_computed() -> None:
    # 全局比例并集含 19.5:9 等小数比例，不得静默回退 1:1
    size = _resolve_image_size(
        resolution="1K", aspect_ratio="19.5:9", family="qwen-image"
    )
    width, height = (int(part) for part in size.split("x"))
    assert width % 8 == 0 and height % 8 == 0
    assert abs(width / height - 19.5 / 9) < 0.02


def test_unknown_family_conservative_bounds() -> None:
    # 未知族按 1K 预算保守档 [512, 1440] 计算
    size = _resolve_image_size(resolution="2K", aspect_ratio="1:1", family="unknown")
    assert size == "1024x1024"


def test_invalid_resolution_falls_back_to_1k() -> None:
    size = _resolve_image_size(resolution="8K", aspect_ratio="4:5", family="unknown")
    width, height = (int(part) for part in size.split("x"))
    assert 512 <= width <= 1440 and 512 <= height <= 1440
    assert abs(width / height - 4 / 5) < 0.02


# ---------------------------------------------------------------------------
# parse_response：错误体解析
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "message_part", "retryable"),
    [
        (
            {"code": 20012, "message": "bad request", "data": ""},
            400,
            "bad request",
            None,
        ),
        ("Invalid token", 401, "Invalid token", None),
        ({}, 500, "HTTP 500", None),
        (
            {"code": 50505, "message": "Model service overloaded."},
            503,
            "过载",
            True,
        ),
    ],
)
async def test_error_body_parsing(
    body: Any,
    status: int,
    message_part: str,
    retryable: bool | None,
) -> None:
    provider = SiliconFlowProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=object(),
            response_data=body,
            session=None,  # type: ignore[arg-type]
            http_status=status,
            is_retry=False,
        )
    assert message_part in exc_info.value.message
    assert exc_info.value.retryable is retryable


# ---------------------------------------------------------------------------
# parse_response：成功路径与 URL 下载
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_images_at_200_raises_no_image() -> None:
    provider = SiliconFlowProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=object(),
            response_data={},
            session=None,  # type: ignore[arg-type]
            http_status=200,
        )
    assert exc_info.value.error_type == "no_image"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_urls_downloaded_immediately_with_candidate_proxy() -> None:
    provider = SiliconFlowProvider()
    client = _FakeClient(download_path="/tmp/a.png")
    urls, paths, text, thought = await provider.parse_response(
        client=client,
        response_data={"images": [{"url": "https://cdn.example.com/a.png"}]},
        session=None,  # type: ignore[arg-type]
        http_status=200,
        request_config=_make_config(proxy="http://127.0.0.1:7890"),
        is_retry=False,
    )
    # URL 仅 1 小时有效：无条件立即下载，urls+paths 均为本地路径
    assert urls == paths == ["/tmp/a.png"]
    image_url, kwargs = client.download_calls[0]
    assert image_url == "https://cdn.example.com/a.png"
    assert kwargs["use_cache"] is False
    # 显式传候选级代理（provider_overrides 优先于全局）
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert text is None
    assert thought is None


@pytest.mark.asyncio
async def test_download_failure_falls_back_to_direct_url() -> None:
    provider = SiliconFlowProvider()
    client = _FakeClient(download_path=None)
    urls, paths, _, _ = await provider.parse_response(
        client=client,
        response_data={"images": [{"url": "https://cdn.example.com/a.png"}]},
        session=None,  # type: ignore[arg-type]
        http_status=200,
        request_config=_make_config(),
    )
    assert urls == ["https://cdn.example.com/a.png"]
    assert paths == []


# ---------------------------------------------------------------------------
# capability profile 与 spec 标志
# ---------------------------------------------------------------------------


class _Candidate:
    def __init__(self, *, model: str, supports_edit: bool = True):
        self.api_type = "siliconflow"
        self.model = model
        self.supports_image_edit = supports_edit
        self.settings = {"model": model}


def test_kolors_capability_batch_and_map() -> None:
    cap = candidate_capability(_Candidate(model="Kwai-Kolors/Kolors"))
    assert cap["native_batch_limit"] == 4
    assert cap["request_setting_map"]["image_count"] == "batch_size"
    assert cap["parameters"]["resolution"]["enum"] == ["1K", "2K"]
    assert "negative_prompt" in cap["parameters"]
    assert cap["request_setting_map"]["negative_prompt"] == "negative_prompt"
    # seed 仅声明不参与运行期注入
    assert cap["request_setting_map"].get("seed") is None


def test_qwen_capability_single_image() -> None:
    cap = candidate_capability(_Candidate(model="Qwen/Qwen-Image"))
    assert cap["native_batch_limit"] == 1
    assert "image_count" not in cap["request_setting_map"]


def test_capability_modes_follow_edit_support() -> None:
    edit_cap = candidate_capability(_Candidate(model="Qwen/Qwen-Image-Edit-2509"))
    assert "image_to_image" in edit_cap["generation_modes"]
    t2i_cap = candidate_capability(
        _Candidate(model="Qwen/Qwen-Image-Edit-2509", supports_edit=False)
    )
    assert "image_to_image" not in t2i_cap["generation_modes"]


def test_siliconflow_spec_flags() -> None:
    spec = get_provider_spec("siliconflow")
    assert spec is not None
    assert spec.provider_path == "tl.api.siliconflow.SiliconFlowProvider"
    assert spec.settings_attr == "siliconflow_settings"
    assert spec.parse_errors_with_provider is True
    # 同步请求无跨阶段状态：不需要重建
    assert spec.rebuild_on_retry is False
    assert spec.max_concurrency == 0
