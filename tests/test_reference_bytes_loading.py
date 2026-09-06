"""load_reference_bytes：data URI/本地路径/URL 的共享参考图解析回归。

复现 job-143ae2afb961：Studio 把画廊/上传参考图解析为本地绝对路径后，
openai_images 与 stepfun 的 edits 只认 base64，直接报「无法解码参考图为二进制数据」。
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from tl.api.openai_images import OpenAIImagesProvider
from tl.api.reference_values import load_reference_bytes
from tl.api.stepfun import StepfunProvider
from tl.api_types import APIError, ApiRequestConfig

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


class FakeClient:
    """模仿 GeminiAPIClient._normalize_reference_image_input 的输入约定。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def _normalize_reference_image_input(
        self, image_input, image_input_mode="force_base64", request_proxy=None
    ):
        self.calls.append((str(image_input), image_input_mode, request_proxy))
        text = str(image_input)
        if text.startswith("file://"):
            path = Path(unquote(urlparse(text).path))
            return "image/png", base64.b64encode(path.read_bytes()).decode()
        if text.startswith(("http://", "https://")):
            return "image/png", PNG_B64
        return None, None


def _config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "gpt-image-2",
        "prompt": "改成一位黑发碧眼的魔女",
        "api_type": "openai_images",
        "api_key": "fake-key",
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


@pytest.fixture
def png_file(tmp_path) -> Path:
    path = tmp_path / "gallery-ref.png"
    path.write_bytes(PNG_BYTES)
    return path


@pytest.mark.asyncio
async def test_data_uri_and_bare_base64_fast_path_needs_no_client() -> None:
    config = _config()
    assert (
        await load_reference_bytes(
            object(), config, f"data:image/png;base64,{PNG_B64}", log_prefix="[t]"
        )
        == PNG_BYTES
    )
    assert (
        await load_reference_bytes(object(), config, PNG_B64, log_prefix="[t]")
        == PNG_BYTES
    )


@pytest.mark.asyncio
async def test_local_path_resolves_via_shared_normalizer(png_file) -> None:
    """Studio 场景：参考图是本地绝对路径，必须转 file:// 后走共享归一化。"""
    client = FakeClient()
    data = await load_reference_bytes(
        client, _config(), str(png_file), log_prefix="[openai_images]"
    )
    assert data == PNG_BYTES
    assert client.calls[0][0].startswith("file://")
    assert client.calls[0][1] == "force_base64"
    assert client.calls[0][2] is None


@pytest.mark.asyncio
async def test_remote_url_passes_candidate_proxy() -> None:
    client = FakeClient()
    data = await load_reference_bytes(
        client,
        _config(proxy="socks5://fake:1080"),
        "https://cdn.example/ref.png",
        log_prefix="[t]",
    )
    assert data == PNG_BYTES
    assert client.calls[0][0] == "https://cdn.example/ref.png"
    assert client.calls[0][2] == "socks5://fake:1080"


@pytest.mark.asyncio
async def test_undecodable_input_without_normalizer_returns_none() -> None:
    """旧语义保留：无法解析时返回 None，由调用方抛不可重试错误。"""
    assert (
        await load_reference_bytes(
            object(), _config(), "not-an-image", log_prefix="[t]"
        )
        is None
    )
    assert await load_reference_bytes(object(), _config(), "", log_prefix="[t]") is None


@pytest.mark.asyncio
async def test_openai_images_edits_accepts_local_paths(png_file) -> None:
    provider = OpenAIImagesProvider()
    payload = await provider._prepare_edits_payload(
        client=FakeClient(),
        config=_config(reference_images=[str(png_file)]),
        settings={},
    )
    assert payload["_multipart"] is True
    values = [field[2] for field in payload["_form_data"]._fields]
    assert PNG_BYTES in values


@pytest.mark.asyncio
async def test_openai_images_edits_multi_refs_and_garbage_skipped(png_file) -> None:
    provider = OpenAIImagesProvider()
    payload = await provider._prepare_edits_payload(
        client=FakeClient(),
        config=_config(
            reference_images=[f"data:image/png;base64,{PNG_B64}", str(png_file)]
        ),
        settings={},
    )
    images = [field for field in payload["_form_data"]._fields if field[2] == PNG_BYTES]
    assert len(images) == 2
    # 解析失败的附加参考图跳过而不中断
    payload = await provider._prepare_edits_payload(
        client=object(),
        config=_config(reference_images=[f"data:image/png;base64,{PNG_B64}", "broken"]),
        settings={},
    )
    images = [field for field in payload["_form_data"]._fields if field[2] == PNG_BYTES]
    assert len(images) == 1


@pytest.mark.asyncio
async def test_openai_images_edits_undecodable_first_ref_keeps_error(png_file) -> None:
    provider = OpenAIImagesProvider()
    with pytest.raises(APIError) as error:
        await provider._prepare_edits_payload(
            client=object(),
            config=_config(reference_images=["not-an-image"]),
            settings={},
        )
    assert "无法解码参考图" in str(error.value)
    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_stepfun_edits_build_request_accepts_local_path(png_file) -> None:
    provider = StepfunProvider()
    request = await provider.build_request(
        client=FakeClient(),
        config=_config(
            api_type="stepfun",
            model="step-image-edit-2",
            reference_images=[str(png_file)],
            provider_settings={"model": "step-image-edit-2"},
        ),
    )
    assert request.url.endswith("/v1/images/edits")
    payload = request.payload
    assert payload["_multipart"] is True
    values = [field[2] for field in payload["_form_data"]._fields]
    assert PNG_BYTES in values


@pytest.mark.asyncio
async def test_bare_base64_alphabet_path_prefers_file_over_decode(
    tmp_path, monkeypatch
) -> None:
    """无扩展名、恰好是合法 base64 的相对路径必须按文件读取，而不是误解码。"""
    (tmp_path / "abcd").write_bytes(PNG_BYTES)
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    data = await load_reference_bytes(client, _config(), "abcd", log_prefix="[t]")
    assert data == PNG_BYTES
    assert client.calls[0][0].endswith("/abcd")
    assert client.calls[0][0].startswith("file://")
