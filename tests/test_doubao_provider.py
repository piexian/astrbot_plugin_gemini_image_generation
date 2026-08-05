from __future__ import annotations

import pytest

from tl.api.doubao import DoubaoProvider
from tl.api_types import ApiRequestConfig
from tl.provider_hooks import (
    DOUBAO_SEQUENTIAL_IMAGES_MAX,
    DOUBAO_SEQUENTIAL_IMAGES_MIN,
    is_doubao_seedream_5_pro,
    normalize_doubao_endpoint_mode,
    normalize_doubao_output_format,
    normalize_doubao_settings,
)


@pytest.mark.asyncio
async def test_doubao_payload_defaults_to_current_seedream_model() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(model="", prompt="draw", api_type="doubao"),
        doubao_settings={},
    )

    assert payload["model"] == "doubao-seedream-5-0-260128"
    assert payload["output_format"] == "jpeg"


@pytest.mark.asyncio
async def test_doubao_seedream_5_pro_omits_group_generation() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5-0-pro-260628",
            prompt="draw",
            api_type="doubao",
        ),
        doubao_settings={
            "endpoint_id": "doubao-seedream-5-0-pro-260628",
            "sequential_image_generation": "auto",
            "sequential_max_images": 12,
        },
    )

    assert payload["output_format"] == "jpeg"
    assert "sequential_image_generation" not in payload
    assert "sequential_image_generation_options" not in payload


@pytest.mark.asyncio
async def test_doubao_seedream_5_pro_limits_reference_images_to_ten() -> None:
    references = [f"https://example.com/reference-{index}.png" for index in range(11)]
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5.0-pro",
            prompt="edit",
            api_type="doubao",
            reference_images=references,
            image_input_mode="auto",
        ),
        doubao_settings={"endpoint_id": "doubao-seedream-5.0-pro"},
    )

    assert isinstance(payload["image"], list)


@pytest.mark.asyncio
async def test_doubao_pro_inference_endpoint_uses_declared_capability() -> None:
    references = [
        f"https://example.com/pro-reference-{index}.png" for index in range(11)
    ]
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="",
            prompt="edit",
            api_type="doubao",
            reference_images=references,
            image_input_mode="auto",
        ),
        doubao_settings={
            "endpoint_id": "ep-20260628-seedream-pro",
            "model_capability": "seedream_5_pro",
            "sequential_image_generation": "auto",
        },
    )

    assert payload["model"] == "ep-20260628-seedream-pro"
    assert "sequential_image_generation" not in payload
    assert isinstance(payload["image"], list)
    assert len(payload["image"]) == 10


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("doubao-seedream-5-0-pro-260628", True),
        ("doubao-seedream-5.0-pro", True),
        ("doubao-seedream-5.0", False),
        ("doubao-seedream-5-0", False),
        ("doubao-seedream-5.0-lite", False),
    ],
)
def test_doubao_pro_detection_requires_explicit_marker(
    model: str, expected: bool
) -> None:
    assert is_doubao_seedream_5_pro(model) is expected


@pytest.mark.asyncio
async def test_doubao_parse_base64_uses_configured_output_format(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_save_base64_image(data: str, extension: str) -> str:
        captured["data"] = data
        captured["extension"] = extension
        return "/tmp/generated.jpeg"

    monkeypatch.setattr(
        "tl.api.doubao.save_base64_image",
        fake_save_base64_image,
    )
    image_urls, image_paths, _, _ = await DoubaoProvider().parse_response(
        client=object(),
        response_data={"data": [{"b64_json": "BASE64DATA"}]},
        session=None,
        request_config=ApiRequestConfig(
            model="doubao-seedream-5.0-pro",
            prompt="draw",
            api_type="doubao",
            provider_settings={"output_format": "jpeg"},
        ),
    )

    assert captured == {"data": "BASE64DATA", "extension": "jpeg"}
    assert image_urls == ["/tmp/generated.jpeg"]
    assert image_paths == ["/tmp/generated.jpeg"]


@pytest.mark.asyncio
async def test_doubao_default_endpoint_mode_uses_official_path() -> None:
    request = await DoubaoProvider().build_request(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5-0-260128",
            prompt="draw",
            api_type="doubao",
            api_key="official-key",
            provider_settings={
                "api_base": "https://ark.cn-beijing.volces.com",
            },
        ),
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    )


@pytest.mark.asyncio
async def test_doubao_agent_plan_endpoint_mode_uses_plan_path() -> None:
    request = await DoubaoProvider().build_request(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5.0-lite",
            prompt="draw",
            api_type="doubao",
            api_key="agent-plan-key",
            provider_settings={
                "api_base": "https://ark.cn-beijing.volces.com",
                "endpoint_mode": "agent_plan",
            },
        ),
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
    )


@pytest.mark.asyncio
async def test_doubao_plan_mode_reuses_existing_full_api_base() -> None:
    request = await DoubaoProvider().build_request(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5.0-lite",
            prompt="draw",
            api_type="doubao",
            api_key="agent-plan-key",
            provider_settings={
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "endpoint_mode": "plan",
            },
        ),
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
    )


@pytest.mark.asyncio
async def test_doubao_custom_size_payload_uses_official_size_field() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="",
            prompt="draw",
            api_type="doubao",
            resolution="4K",
            seed=123,
        ),
        doubao_settings={
            "endpoint_id": "doubao-seedream-5-0-lite",
            "size": "3K",
            "size_mode": "custom",
            "custom_size": "2304×1728",
            "watermark": False,
        },
    )

    assert payload["model"] == "doubao-seedream-5-0-lite"
    assert payload["size"] == "2304x1728"
    assert "seed" not in payload
    assert "size_mode" not in payload
    assert "custom_size" not in payload
    assert "default_size" not in payload
    assert "resolution" not in payload
    assert "aspect_ratio" not in payload


@pytest.mark.asyncio
async def test_doubao_preset_size_payload_uses_size_setting() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5-0-lite",
            prompt="draw",
            api_type="doubao",
            resolution="4K",
        ),
        doubao_settings={
            "size": "3K",
            "size_mode": "preset",
            "sequential_image_generation": "auto",
            "sequential_max_images": 1,
        },
    )

    assert payload["size"] == "3K"
    assert payload["sequential_image_generation_options"] == {"max_images": 1}


def test_doubao_normalizer_accepts_official_min_sequential_images() -> None:
    settings = {"sequential_max_images": "1"}

    normalize_doubao_settings(settings)

    assert DOUBAO_SEQUENTIAL_IMAGES_MIN == 1
    assert settings["sequential_max_images"] == 1


def test_doubao_normalizer_endpoint_mode_defaults_to_official() -> None:
    settings: dict = {}
    normalize_doubao_settings(settings)

    assert settings["endpoint_mode"] == "official"
    assert settings["output_format"] == "jpeg"
    assert settings["model_capability"] == "auto"


def test_doubao_normalizer_output_format_aliases_jpg() -> None:
    settings = {"output_format": "JPG"}
    normalize_doubao_settings(settings)

    assert settings["output_format"] == "jpeg"


def test_doubao_shared_normalizers_canonicalize_aliases() -> None:
    assert normalize_doubao_endpoint_mode("Plan") == "agent_plan"
    assert normalize_doubao_output_format("JPG") == "jpeg"


def test_doubao_normalizer_invalid_output_format_falls_back() -> None:
    settings = {"output_format": "webp"}
    normalize_doubao_settings(settings)
    assert settings["output_format"] == "jpeg"


def test_doubao_normalizer_accepts_pro_model_capability_alias() -> None:
    settings = {"model_capability": "Pro"}
    normalize_doubao_settings(settings)

    assert settings["model_capability"] == "seedream_5_pro"


def test_doubao_normalizer_invalid_model_capability_falls_back() -> None:
    settings = {"model_capability": "unknown"}
    normalize_doubao_settings(settings)

    assert settings["model_capability"] == "auto"


def test_doubao_normalizer_accepts_agent_plan_alias() -> None:
    settings = {"endpoint_mode": "Plan"}
    normalize_doubao_settings(settings)

    assert settings["endpoint_mode"] == "agent_plan"


def test_doubao_normalizer_invalid_endpoint_mode_falls_back() -> None:
    settings = {"endpoint_mode": "unsupported"}
    normalize_doubao_settings(settings)

    assert settings["endpoint_mode"] == "official"


def test_doubao_normalizer_rejects_zero_sequential_images() -> None:
    settings = {"sequential_max_images": "0"}

    with pytest.raises(ValueError) as exc_info:
        normalize_doubao_settings(settings)

    message = str(exc_info.value)
    assert "sequential_max_images" in message
    assert "必须在" in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MIN) in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MAX) in message


def test_doubao_normalizer_rejects_too_many_sequential_images() -> None:
    settings = {"sequential_max_images": str(DOUBAO_SEQUENTIAL_IMAGES_MAX + 1)}

    with pytest.raises(ValueError) as exc_info:
        normalize_doubao_settings(settings)

    message = str(exc_info.value)
    assert "sequential_max_images" in message
    assert "必须在" in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MIN) in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MAX) in message


def test_doubao_normalizer_rejects_non_numeric_sequential_images() -> None:
    settings = {"sequential_max_images": "not-a-number"}

    with pytest.raises(ValueError) as exc_info:
        normalize_doubao_settings(settings)

    message = str(exc_info.value)
    assert "sequential_max_images" in message
    assert "配置无效" in message
