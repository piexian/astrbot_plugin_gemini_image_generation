"""tests for tl/api/minimax.py — 官方限制对齐与参考图格式归一化"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image as PILImage

from tl.api.minimax import MiniMaxProvider
from tl.api.reference_values import normalize_image_mime
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


def test_normalize_image_mime_passthrough_for_jpeg_png() -> None:
    assert normalize_image_mime("QUJD", "image/jpeg", error_label="minimax") == (
        "QUJD",
        "image/jpeg",
    )
    assert normalize_image_mime("QUJD", "image/png", error_label="minimax") == (
        "QUJD",
        "image/png",
    )


def test_normalize_image_mime_transcodes_gif_first_frame_to_png() -> None:
    buf = io.BytesIO()
    PILImage.new("P", (4, 4), color=1).save(buf, format="GIF", save_all=True)
    gif_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    out_b64, out_mime = normalize_image_mime(
        gif_b64, "image/gif", error_label="minimax"
    )
    assert out_mime == "image/png"
    with PILImage.open(io.BytesIO(base64.b64decode(out_b64))) as img:
        assert img.format == "PNG"
        assert img.size == (4, 4)


def test_normalize_image_mime_rejects_oversize() -> None:
    big = base64.b64encode(b"\x00" * (10 * 1024 * 1024 + 1)).decode("ascii")
    with pytest.raises(Exception) as exc_info:
        normalize_image_mime(big, "image/gif", error_label="minimax")
    assert getattr(exc_info.value, "retryable", True) is False


def test_normalize_image_mime_rejects_oversize_after_transcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源字节未超限但转码后 PNG 超限时同样拒绝（高压缩比源场景）。"""
    import tl.api.reference_values as reference_values

    buf = io.BytesIO()
    PILImage.new("P", (4, 4), color=1).save(buf, format="GIF", save_all=True)
    gif_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    # 阈值设为源大小：源校验恰好通过，转码产物（更大）触发转码后校验
    monkeypatch.setattr(
        reference_values, "_MAX_REFERENCE_IMAGE_BYTES", len(buf.getvalue())
    )
    with pytest.raises(Exception) as exc_info:
        normalize_image_mime(gif_b64, "image/gif", error_label="minimax")
    assert getattr(exc_info.value, "retryable", True) is False
    assert "转码后" in str(exc_info.value)


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


@pytest.mark.asyncio
async def test_image_count_clamped_to_nine() -> None:
    provider = MiniMaxProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(provider_settings={"model": "image-01", "n": 15}),
    )
    assert request.payload["n"] == 9


def test_style_assembly_for_live_and_ignored_for_image01() -> None:
    provider = MiniMaxProvider()
    settings = {"style_type": "漫画", "style_weight": 0.5}

    style = provider._resolve_style(settings, "image-01-live")
    assert style == {"style_type": "漫画", "style_weight": 0.5}

    assert provider._resolve_style(settings, "image-01") is None

    # 权重非法/留空 → 省略字段，交服务端默认 0.8
    style = provider._resolve_style(
        {"style_type": "元气", "style_weight": 1.5}, "image-01-live"
    )
    assert style == {"style_type": "元气"}

    # legacy settings.style dict 兼容
    legacy = {"style": {"style_type": "水彩"}}
    assert provider._resolve_style(legacy, "image-01-live") == {"style_type": "水彩"}
