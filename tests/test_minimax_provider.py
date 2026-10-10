"""tests for tl/api/minimax.py — 官方限制对齐与参考图格式归一化"""

from __future__ import annotations

import pytest

from tl.api.minimax import MiniMaxProvider
from tl.api_types import ApiRequestConfig


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "minimax",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "image-01"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


class _FakeClient:
    async def _normalize_reference_image_input(self, image_input, image_input_mode):  # noqa: ANN001
        return "image/png", "QUJD"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None


@pytest.mark.asyncio
async def test_prompt_over_1500_raises_non_retryable() -> None:
    provider = MiniMaxProvider()
    config = _make_config(prompt="x" * 1501)
    with pytest.raises(Exception) as exc_info:
        await provider.build_request(client=_FakeClient(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False


@pytest.mark.asyncio
async def test_subject_reference_truncated_to_nine() -> None:
    provider = MiniMaxProvider()
    config = _make_config(
        reference_images=[f"https://example.com/{i}.png" for i in range(11)]
    )
    references = await provider._build_subject_reference(
        client=_FakeClient(), config=config, settings={}
    )
    assert len(references) == 9
    assert references[0]["type"] == "character"


@pytest.mark.asyncio
async def test_parse_response_reports_safety_block_count() -> None:
    provider = MiniMaxProvider()
    with pytest.raises(Exception) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={
                "data": {},
                "metadata": {"success_count": 0, "failed_count": 2},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            },
            session=None,
        )
    assert "内容安全" in str(exc_info.value)
