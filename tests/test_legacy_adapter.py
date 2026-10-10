from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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

    async def generate_image(self, config, *, max_retries=3, total_timeout=120):
        self.calls.append(
            {
                "config": config,
                "max_retries": max_retries,
                "total_timeout": total_timeout,
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
async def test_error_response_raises_non_retryable_legacy_error():
    client = FakeClient(([], [], None, "配额耗尽"))
    adapter = LegacyClientAdapter(client, provider="gemini", model="gemini-x")
    with pytest.raises(LegacyProviderError) as exc_info:
        await dispatch_provider(adapter, make_job(make_request()))
    assert exc_info.value.retryable is False
    assert exc_info.value.code == "legacy_provider_failed"
    # 不可重试错误不应在 dispatch 层重复调用 legacy client。
    assert len(client.calls) == 1


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
    assert config.requested_provider == "gemini"
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
