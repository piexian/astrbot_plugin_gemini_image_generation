from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tl.api.openai_images import OpenAIImagesProvider, read_images_response
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_reference_limit, openai_images_capability


class Response:
    status = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, events, *, chunk_size=7):
        self.raw = "".join(
            f"event: {event['type']}\r\nid: 1\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
            for event in events
        ).encode()
        self.chunk_size = chunk_size
        self.content = self

    async def iter_any(self):
        for pos in range(0, len(self.raw), self.chunk_size):
            yield self.raw[pos : pos + self.chunk_size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def config(**overrides):
    return ApiRequestConfig(
        model=overrides.pop("model", "gpt-image-2.5-flare"),
        prompt="draw",
        api_type="openai_images",
        api_key="test",
        **overrides,
    )


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.mark.asyncio
async def test_native_stream_without_completion_is_not_retried():
    with pytest.raises(APIError) as error:
        await read_images_response(
            response=Response(
                [{"type": "image_edit.partial_image", "b64_json": "preview"}]
            )
        )
    assert error.value.error_type == "outcome_unknown"


def test_native_batch_and_reference_limits():
    candidate = SimpleNamespace(
        api_type="openai_images",
        model="gpt-image-2.5-flare",
        supports_image_edit=True,
        settings={"n": 10, "max_reference_images": 99},
    )
    assert openai_images_capability(candidate)["native_batch_limit"] == 10
    assert candidate_reference_limit(candidate) == 16
    candidate.settings["stream"] = True
    assert openai_images_capability(candidate)["native_batch_limit"] == 1

    # output_format 全系声明；input_fidelity 仅 1.5/mini 按模型声明，其余模型工作台隐藏
    flare = openai_images_capability(candidate)
    assert flare["parameters"]["output_format"]["enum"] == [
        "png",
        "jpeg",
        "webp",
        "gif",
        "svg",
    ]
    assert "input_fidelity" not in flare["parameters"]
    assert flare["unsupported_settings"] == {"input_fidelity"}
    one_five = SimpleNamespace(
        api_type="openai_images",
        model="gpt-image-1.5",
        supports_image_edit=True,
        settings={},
    )
    mini = SimpleNamespace(
        api_type="openai_images",
        model="gpt-image-1-mini",
        supports_image_edit=True,
        settings={},
    )
    one_five_enum = openai_images_capability(one_five)["parameters"]["input_fidelity"][
        "enum"
    ]
    mini_enum = openai_images_capability(mini)["parameters"]["input_fidelity"]["enum"]
    assert one_five_enum == ["low", "high"]
    assert mini_enum == ["low"]


@pytest.mark.asyncio
async def test_invalid_stream_batch_is_rejected():
    with pytest.raises(APIError):
        await OpenAIImagesProvider().build_request(
            client=object(), config=config(provider_settings={"n": 2, "stream": True})
        )
    # gif/svg 输出与流式、预览、批量互斥，请求构建期拒绝
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(),
            config=config(provider_settings={"output_format": "gif", "stream": True}),
        )
    assert error.value.error_type == "invalid_parameter"
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(),
            config=config(
                provider_settings={"output_format": "svg", "partial_images": 2}
            ),
        )
    assert error.value.error_type == "invalid_parameter"
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(),
            config=config(provider_settings={"output_format": "svg", "n": 2}),
        )
    assert error.value.error_type == "invalid_parameter"
    # complexity 与 GIF 帧参数非法值拒绝
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(), config=config(provider_settings={"complexity": "ultra"})
        )
    assert error.value.error_type == "invalid_parameter"
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(),
            config=config(provider_settings={"output_format": "gif", "frame_count": 1}),
        )
    assert error.value.error_type == "invalid_parameter"
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider().build_request(
            client=object(),
            config=config(
                provider_settings={"output_format": "gif", "frame_duration_ms": 10}
            ),
        )
    assert error.value.error_type == "invalid_parameter"
    # generations 载荷携带新键，非 gif 时帧参数与 svg_model 静默忽略
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            provider_settings={
                "complexity": "high",
                "output_format": "gif",
                "frame_count": 12,
                "frame_duration_ms": 300,
            }
        ),
    )
    assert request.payload["complexity"] == "high"
    assert request.payload["output_format"] == "gif"
    assert request.payload["frame_count"] == 12
    assert request.payload["frame_duration_ms"] == 300
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            provider_settings={
                "output_format": "png",
                "frame_count": 12,
                "frame_duration_ms": 300,
                "svg_model": "o3",
            }
        ),
    )
    assert "frame_count" not in request.payload
    assert "frame_duration_ms" not in request.payload
    assert "svg" not in request.payload
    # svg 输出携带作者模型对象
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            provider_settings={
                "output_format": "svg",
                "svg_model": "gpt-image-2.5-flare",
            }
        ),
    )
    assert request.payload["svg"] == {"model": "gpt-image-2.5-flare"}
    # edits：svg 对象以 JSON 字符串进表单
    form = await OpenAIImagesProvider()._prepare_edits_payload(
        client=object(),
        config=config(reference_images=[f"data:image/png;base64,{PNG_B64}"]),
        settings={"output_format": "svg", "svg_model": "o3"},
    )
    fields = {field[0]["name"]: field[2] for field in form["_form_data"]._fields}
    assert fields["svg"] == '{"model": "o3"}'
    # edits：input_fidelity 按模型门控
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider()._prepare_edits_payload(
            client=object(),
            config=config(reference_images=[f"data:image/png;base64,{PNG_B64}"]),
            settings={"input_fidelity": "low"},
        )
    assert error.value.error_type == "invalid_parameter"
    with pytest.raises(APIError) as error:
        await OpenAIImagesProvider()._prepare_edits_payload(
            client=object(),
            config=config(
                model="gpt-image-1-mini",
                reference_images=[f"data:image/png;base64,{PNG_B64}"],
            ),
            settings={"input_fidelity": "high"},
        )
    assert error.value.error_type == "invalid_parameter"
    form = await OpenAIImagesProvider()._prepare_edits_payload(
        client=object(),
        config=config(
            model="gpt-image-1.5",
            reference_images=[f"data:image/png;base64,{PNG_B64}"],
        ),
        settings={"input_fidelity": "high"},
    )
    fields = {field[0]["name"]: field[2] for field in form["_form_data"]._fields}
    assert fields["input_fidelity"] == "high"
