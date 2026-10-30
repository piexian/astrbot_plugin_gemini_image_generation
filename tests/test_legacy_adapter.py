from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tl.api_types import APIError
from tl.core.dispatch import dispatch_provider
from tl.core.errors import LegacyProviderError
from tl.core.legacy_adapter import LegacyClientAdapter
from tl.core.models import Job
from tl.core.requests import GenerationRequest, ProviderAttempt
from tl.core.states import JobState


class FakeClient:
    """鸭子类型 legacy client：捕获调用入参并返回固定 4 元组。"""

    def __init__(self, response: tuple):
        self.response = response
        self.calls: list[tuple[object, dict]] = []

    async def generate_image(
        self,
        config,
        *,
        max_retries=3,
        total_timeout=120,
        per_retry_timeout=None,
        max_total_time=None,
    ):
        self.calls.append(
            {
                "config": config,
                "max_retries": max_retries,
                "total_timeout": total_timeout,
                "per_retry_timeout": per_retry_timeout,
                "max_total_time": max_total_time,
            }
        )
        return self.response


def make_request(**overrides) -> GenerationRequest:
    values = {"prompt": "draw a cat", "source": "test"}
    values.update(overrides)
    return GenerationRequest(**values)


def make_job(request: GenerationRequest) -> Job:
    return Job(job_id="job-adapter", request=request, state=JobState.RUNNING)


@pytest.mark.asyncio
async def test_contract_via_dispatch_merges_urls_and_paths():
    urls = ["https://img.test/b.png", "https://img.test/a.png"]
    paths = ["/tmp/out/a.png", "https://img.test/a.png"]
    client = FakeClient((urls, paths, "好的", None))
    adapter = LegacyClientAdapter(
        client, provider="gemini", model="gemini-x", candidate_id="cand-1"
    )
    result = await dispatch_provider(adapter, make_job(make_request()))
    # urls+paths 去重排序。
    assert result.artifacts == (
        "/tmp/out/a.png",
        "https://img.test/a.png",
        "https://img.test/b.png",
    )
    assert result.text == "好的"
    assert result.provider == "gemini"
    assert result.model == "gemini-x"
    assert result.candidate_id == "cand-1"
    assert result.attempt_count == 1
    assert result.partial is False
    assert client.calls[0]["max_retries"] == 3


@pytest.mark.asyncio
async def test_thought_signature_still_succeeds():
    """legacy 4 元组的第 4 项是思维签名：非空不判失败，结果照常成功。"""

    client = FakeClient((["u"], [], "好的", "signature-1"))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    result = await dispatch_provider(adapter, make_job(make_request()))
    assert result.artifacts == ("u",)
    assert result.text == "好的"


@pytest.mark.asyncio
async def test_empty_output_raises_non_retryable_legacy_error():
    """空产出（无图且文本为空白）报 provider_empty_result，不重试。"""

    client = FakeClient(([], [], None, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    with pytest.raises(LegacyProviderError) as exc_info:
        await dispatch_provider(adapter, make_job(make_request()))
    assert exc_info.value.retryable is False
    assert exc_info.value.code == "provider_empty_result"
    # 不可重试错误不应在 dispatch 层重复调用 legacy client。
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_text_only_result_succeeds_without_require_images():
    """未声明 require_images（命令快捷路径）：只有文本仍按成功返回。"""

    client = FakeClient(([], [], "只有文本", None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")

    for metadata in ({}, {"require_images": False}):
        result = await dispatch_provider(
            adapter, make_job(make_request(metadata=metadata))
        )
        assert result.artifacts == ()
        assert result.text == "只有文本"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["只有文本", None])
async def test_require_images_rejects_results_without_any_image(text):
    """require_images（生成核心路径）：没有图片就失败，哪怕带了文本；不重试。"""

    client = FakeClient(([], [], text, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    request = make_request(metadata={"require_images": True})

    with pytest.raises(LegacyProviderError) as exc_info:
        await dispatch_provider(adapter, make_job(request))

    assert exc_info.value.code == "provider_no_image"
    assert exc_info.value.retryable is False
    assert exc_info.value.message == "供应商未返回图片"
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        (["https://img.test/a.png"], [], None, None),
        ([], ["/tmp/out/a.png"], "带文本", None),
    ],
)
async def test_require_images_accepts_results_with_an_image(response):
    client = FakeClient(response)
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    request = make_request(metadata={"require_images": True})

    result = await dispatch_provider(adapter, make_job(request))

    assert result.artifacts == tuple(sorted({*response[0], *response[1]}))


class _FailingClient:
    """按预设异常失败的 legacy client，统计调用次数。"""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def generate_image(self, config, **kwargs):
        self.calls += 1
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_error", "expected_code"),
    [
        (APIError("上游超时", None, "timeout"), "provider_timeout"),
        (APIError("已取消", None, "cancelled"), "provider_timeout"),
        (APIError("网络异常", None, "network"), "provider_network"),
        (
            APIError("工作区不可用", None, "workspace_unavailable"),
            "provider_workspace_unavailable",
        ),
        (APIError("限流", 429, None), "provider_http_429"),
        (APIError("未知失败"), "legacy_provider_failed"),
    ],
)
async def test_api_error_maps_to_non_retryable_legacy_error(api_error, expected_code):
    """旧客户端 APIError 按类型/状态码映射 code，转不可重试并保留原因链。"""

    client = _FailingClient(api_error)
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    with pytest.raises(LegacyProviderError) as exc_info:
        await dispatch_provider(adapter, make_job(make_request()))
    assert exc_info.value.code == expected_code
    assert exc_info.value.retryable is False
    assert exc_info.value.__cause__ is api_error
    # legacy client 内部已重试过，Router/dispatch 不再重复调用。
    assert client.calls == 1


@pytest.mark.asyncio
async def test_deadline_converted_to_total_timeout():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    request = make_request()

    future = datetime.now(timezone.utc) + timedelta(seconds=59.9)
    await adapter.send(
        request,
        ProviderAttempt(number=1, deadline_at=future),
        url="",
        headers={},
        body_factory=lambda: None,
    )
    assert client.calls[-1]["total_timeout"] == 59

    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await adapter.send(
        request,
        ProviderAttempt(number=1, deadline_at=past),
        url="",
        headers={},
        body_factory=lambda: None,
    )
    assert client.calls[-1]["total_timeout"] >= 1

    await adapter.send(
        request,
        ProviderAttempt(number=1, deadline_at=None),
        url="",
        headers={},
        body_factory=lambda: None,
    )
    assert client.calls[-1]["total_timeout"] == 120


@pytest.mark.asyncio
async def test_config_mapping_and_fresh_instances_per_factory_call():
    client = FakeClient((["u"], [], None, None))
    settings = {"api_type": "openai"}
    adapter = LegacyClientAdapter(
        client,
        provider="gemini",
        model="gemini-x",
        candidate_id="cand-9",
        proxy="http://proxy.test:7890",
        provider_settings=settings,
    )
    request = make_request(
        image_count=2,
        resolution="2K",
        aspect_ratio="16:9",
        negative_prompt="blurry",
        quality="high",
        watermark=False,
        reference_images=("/tmp/a.png", "/tmp/b.png"),
    )
    url, headers, body_factory = await adapter.build_request(
        request, ProviderAttempt(number=1)
    )
    assert url == ""
    assert headers == {}
    first = body_factory()
    second = body_factory()
    assert first is not second

    await adapter.send(
        request,
        ProviderAttempt(number=1),
        url=url,
        headers=headers,
        body_factory=body_factory,
    )
    config = client.calls[-1]["config"]
    assert config.model == "gemini-x"
    assert config.prompt == "draw a cat"
    assert config.reference_images == ["/tmp/a.png", "/tmp/b.png"]
    assert config.candidate_id == "cand-9"
    assert config.candidate_id == "cand-9"
    # requested_* 只来自入口请求（此处未指定 → 全量轮询），适配器标注不外泄。
    assert config.requested_provider is None
    assert config.requested_model is None
    assert config.resolution == "2K"
    assert config.aspect_ratio == "16:9"
    assert config.negative_prompt == "blurry"
    assert config.quality == "high"
    assert config.watermark is False
    assert config.proxy == "http://proxy.test:7890"
    assert config.provider_settings == {"api_type": "openai"}
    assert config.image_count == 2


@pytest.mark.asyncio
async def test_optional_fields_default_to_none_without_settings():
    client = FakeClient(([], [], "text-only", None))
    adapter = LegacyClientAdapter(client, provider="openai", model="gpt-image")
    result = await dispatch_provider(adapter, make_job(make_request()))
    config = client.calls[-1]["config"]
    assert config.proxy is None
    assert config.provider_settings is None
    assert config.reference_images == []
    # 空 urls/paths 仍返回 text-only 结果。
    assert result.artifacts == ()
    assert result.text == "text-only"


@pytest.mark.asyncio
async def test_send_returns_config_pair_and_parse_prefers_writeback():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(
        client, provider="gemini", model="gemini-x", candidate_id="cand-1"
    )
    request = make_request()
    attempt = ProviderAttempt(number=1)
    url, headers, body_factory = await adapter.build_request(request, attempt)
    response = await adapter.send(
        request,
        attempt,
        url=url,
        headers=headers,
        body_factory=body_factory,
    )
    # send 返回 (legacy 4 元组, config) 二元组。
    assert isinstance(response, tuple)
    legacy_tuple, config = response
    assert legacy_tuple == (["u"], [], None, None)

    # client 在 config 上回写 successful_* 时，parse_response 标注采用回写值。
    config.successful_provider = "openai"
    config.successful_model = "gpt-image-2"
    config.successful_candidate_id = "cand-real"
    result = await adapter.parse_response(response, request=request, attempt=attempt)
    assert result.provider == "openai"
    assert result.model == "gpt-image-2"
    assert result.candidate_id == "cand-real"

    # 未回写时回退适配器自身标注。
    adapter2 = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    url2, headers2, body_factory2 = await adapter2.build_request(request, attempt)
    response2 = await adapter2.send(
        request,
        attempt,
        url=url2,
        headers=headers2,
        body_factory=body_factory2,
    )
    result2 = await adapter2.parse_response(response2, request=request, attempt=attempt)
    assert result2.provider == "gemini"
    assert result2.model == "gemini-x"
    assert result2.candidate_id is None


@pytest.mark.asyncio
async def test_parse_response_collects_legacy_stats_writeback():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    request = make_request()
    attempt = ProviderAttempt(number=1)
    url, headers, body_factory = await adapter.build_request(request, attempt)
    response = await adapter.send(
        request, attempt, url=url, headers=headers, body_factory=body_factory
    )
    # client 在 config 副本上回写 legacy 统计字段，parse_response 经 stats 带出。
    _, config = response
    config.retry_count = 2
    config.retry_note = "上游 429 后重试成功"
    config.token_usage = {"prompt_tokens": 10, "total_tokens": 30}
    config.successful_model_alias = "别名"
    result = await adapter.parse_response(response, request=request, attempt=attempt)
    assert result.stats == {
        "retry_count": 2,
        "retry_note": "上游 429 后重试成功",
        "token_usage": {"prompt_tokens": 10, "total_tokens": 30},
        "successful_model_alias": "别名",
    }


@pytest.mark.asyncio
async def test_parse_response_omits_default_stats_fields():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    result = await dispatch_provider(adapter, make_job(make_request()))
    # 字段为默认值（0/None）时省略键，无 None 占位。
    assert result.stats == {}


@pytest.mark.asyncio
async def test_routing_pins_and_legacy_knobs_flow_from_request():
    """requested_* 路由与 legacy 专属开关只从请求透传，适配器哨兵不得进 config。"""
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="legacy", model="auto")
    request = make_request(
        provider="doubao",
        model="doubao-seedream-4",
        metadata={"suppress_resolution": True, "enable_smart_retry": False},
    )
    attempt = ProviderAttempt(number=1)
    url, headers, body_factory = await adapter.build_request(request, attempt)
    await adapter.send(
        request, attempt, url=url, headers=headers, body_factory=body_factory
    )
    config = client.calls[-1]["config"]
    assert config.requested_provider == "doubao"
    assert config.requested_model == "doubao-seedream-4"
    assert config.model == "doubao-seedream-4"
    assert config.suppress_resolution is True
    assert config.enable_smart_retry is False

    # 无固定时 requested_* 为 None（全量轮询），legacy 开关回落默认值。
    plain = make_request()
    url2, headers2, factory2 = await adapter.build_request(plain, attempt)
    await adapter.send(
        plain, attempt, url=url2, headers=headers2, body_factory=factory2
    )
    config2 = client.calls[-1]["config"]
    assert config2.requested_provider is None
    assert config2.requested_model is None
    assert config2.model == "auto"
    assert config2.suppress_resolution is False
    assert config2.enable_smart_retry is True


@pytest.mark.asyncio
async def test_retry_limits_and_timeout_are_transmitted():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    request = make_request(metadata={"max_attempts_per_key": 7, "total_timeout": 9})
    attempt = ProviderAttempt(number=1)
    url, headers, factory = await adapter.build_request(request, attempt)
    await adapter.send(request, attempt, url=url, headers=headers, body_factory=factory)

    assert client.calls[-1]["max_retries"] == 7
    assert client.calls[-1]["total_timeout"] == 9
    assert client.calls[-1]["per_retry_timeout"] == 9


@pytest.mark.asyncio
async def test_fallback_candidate_gets_its_own_route_values():
    client = FakeClient((["u"], [], None, None))
    adapter = LegacyClientAdapter(client, provider="beta", model="beta-x")
    request = make_request(provider="alpha", model="alpha-x")
    attempt = ProviderAttempt(
        number=1,
        context={
            "candidate_provider": "beta",
            "candidate_model": "beta-x",
            "candidate_preferred": False,
        },
    )
    _, _, factory = await adapter.build_request(request, attempt)
    config = factory()
    assert config.requested_provider == "beta"
    assert config.requested_model == "beta-x"
    assert config.model == "beta-x"
