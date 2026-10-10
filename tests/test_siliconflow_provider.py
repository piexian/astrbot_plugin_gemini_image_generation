"""tests for tl/api/siliconflow.py — 同步单端点 provider 的构建/解析/门控"""

from __future__ import annotations

from typing import Any

import pytest

from tl.api.siliconflow import SiliconFlowProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_hooks import siliconflow_edit_capability

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
    # 与 provider 族判定同源：非 Qwen-Image/Kolors 系即使含 edit 字样也不放行
    assert not siliconflow_edit_capability({"model": "foo/whatever-edit-v2"})
    assert siliconflow_edit_capability({"model": "Kwai-Kolors/Kolors"})


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
