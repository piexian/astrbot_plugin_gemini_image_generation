from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tl.api_types import ApiRequestConfig
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.scheduler import JobResult
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.v2_bridge import (
    apply_result_to_config,
    command_request,
    split_artifacts,
    v2_failure_error,
)


def test_split_artifacts_by_prefix():
    urls, paths = split_artifacts(
        [
            "http://a.test/x.png",
            "https://b.test/y.png",
            "/tmp/out/z.png",
            "artifact:abc",
        ]
    )
    assert urls == ["http://a.test/x.png", "https://b.test/y.png"]
    assert paths == ["/tmp/out/z.png", "artifact:abc"]


def test_split_artifacts_empty():
    assert split_artifacts([]) == ([], [])


def test_apply_result_to_config_writes_and_keeps_existing():
    config = SimpleNamespace(
        successful_provider=None,
        successful_model=None,
        successful_candidate_id=None,
    )
    apply_result_to_config(
        config,
        GenerationResult(
            artifacts=(),
            provider="gemini",
            model="gemini-x",
            candidate_id="cand-1",
        ),
    )
    assert config.successful_provider == "gemini"
    assert config.successful_model == "gemini-x"
    assert config.successful_candidate_id == "cand-1"

    # 已有非空值不覆盖；result 缺失字段不写入。
    config2 = SimpleNamespace(
        successful_provider="openai",
        successful_model="gpt-image",
        successful_candidate_id=None,
    )
    apply_result_to_config(
        config2,
        GenerationResult(artifacts=(), provider="gemini", model="gemini-x"),
    )
    assert config2.successful_provider == "openai"
    assert config2.successful_model == "gpt-image"
    assert config2.successful_candidate_id is None


def test_apply_result_to_config_writes_stats_fields():
    config = SimpleNamespace(
        successful_provider=None,
        successful_model=None,
        successful_candidate_id=None,
        retry_count=0,
        retry_note=None,
        token_usage=None,
        successful_model_alias=None,
    )
    apply_result_to_config(
        config,
        GenerationResult(
            artifacts=(),
            stats={
                "retry_count": 2,
                "retry_note": "上游 429 后重试成功",
                "token_usage": {"prompt_tokens": 10, "total_tokens": 30},
                "successful_model_alias": "别名",
            },
        ),
    )
    assert config.retry_count == 2
    assert config.retry_note == "上游 429 后重试成功"
    assert config.token_usage == {"prompt_tokens": 10, "total_tokens": 30}
    assert config.successful_model_alias == "别名"


def test_apply_result_to_config_stats_keep_existing_and_empty_noop():
    existing = SimpleNamespace(
        successful_provider=None,
        successful_model=None,
        successful_candidate_id=None,
        retry_count=1,
        retry_note="入口已写入",
        token_usage={"total_tokens": 5},
        successful_model_alias="入口别名",
    )
    apply_result_to_config(
        existing,
        GenerationResult(
            artifacts=(),
            stats={
                "retry_count": 2,
                "retry_note": "上游 429 后重试成功",
                "token_usage": {"prompt_tokens": 10, "total_tokens": 30},
                "successful_model_alias": "别名",
            },
        ),
    )
    # 已有非空值不被 stats 覆盖。
    assert existing.retry_count == 1
    assert existing.retry_note == "入口已写入"
    assert existing.token_usage == {"total_tokens": 5}
    assert existing.successful_model_alias == "入口别名"

    # stats 为空时 config 不变。
    untouched = SimpleNamespace(
        successful_provider=None,
        successful_model=None,
        successful_candidate_id=None,
        retry_count=0,
        retry_note=None,
        token_usage=None,
        successful_model_alias=None,
    )
    apply_result_to_config(untouched, GenerationResult(artifacts=(), stats={}))
    assert untouched.retry_count == 0
    assert untouched.retry_note is None
    assert untouched.token_usage is None
    assert untouched.successful_model_alias is None


def test_v2_failure_error_contains_code_and_message():
    job_result = JobResult(
        job_id="job-1",
        state=JobState.FAILED,
        result=None,
        error_code="provider_failed",
        error_message="boom",
    )
    error = v2_failure_error(job_result)
    assert isinstance(error, RuntimeError)
    assert "provider_failed" in str(error)
    assert "boom" in str(error)


def test_command_request_fields_and_deadline():
    before = datetime.now(timezone.utc)
    request = command_request(
        prompt="draw a cat",
        resolution="2K",
        aspect_ratio="16:9",
        requested_provider="gemini",
        requested_model="gemini-x",
        reference_images=["/tmp/a.png"],
        deadline_seconds=240,
    )
    after = datetime.now(timezone.utc)
    assert isinstance(request, GenerationRequest)
    assert request.source == "command"
    assert request.provider == "gemini"
    assert request.model == "gemini-x"
    assert request.resolution == "2K"
    assert request.aspect_ratio == "16:9"
    assert request.reference_images == ("/tmp/a.png",)
    assert request.requester == {}
    assert request.deadline_at.tzinfo is timezone.utc
    assert (
        before + timedelta(seconds=240)
        <= request.deadline_at
        <= after + timedelta(seconds=240)
    )


def test_command_request_accepts_requester():
    request = command_request(
        prompt="draw a cat",
        resolution=None,
        aspect_ratio=None,
        requested_provider=None,
        requested_model=None,
        reference_images=[],
        deadline_seconds=30,
        requester={"user_id": "u1", "group_id": "g1", "unknown_field": "x"},
    )
    # requester 归一化：只保留白名单字段；不传时保持空对象。
    assert request.requester == {"user_id": "u1", "group_id": "g1"}


class _StatsWritingClient:
    """鸭子类型 legacy client：在收到的 config 副本上回写 legacy 统计字段。"""

    async def generate_image(
        self,
        config,
        *,
        max_retries=3,
        total_timeout=120,
        per_retry_timeout=None,
        max_total_time=None,
    ):
        config.retry_count = 2
        config.retry_note = "上游 429 后重试成功"
        config.token_usage = {"prompt_tokens": 10, "total_tokens": 30}
        config.successful_model_alias = "别名"
        return (["https://img.test/generated.png"], [], "ok", None)


@pytest.mark.asyncio
async def test_stats_writeback_end_to_end_via_stack(tmp_path):
    """v2 栈端到端：stats 随 GenerationResult 流经 submit/wait 回写入口 config。"""

    service = build_generation_stack(
        _StatsWritingClient(),
        [{"provider": "gemini", "model": "gemini-x", "candidate_id": "cand-1"}],
        tmp_path / "store",
        (),
    )
    try:
        request = GenerationRequest(prompt="draw", source="command")
        job_result = await service.wait((await service.submit(request)).job_id)
        assert job_result.state is JobState.SUCCEEDED
        assert job_result.result is not None

        config = ApiRequestConfig(model="gemini-x", prompt="draw")
        apply_result_to_config(config, job_result.result)
        assert config.successful_provider == "gemini"
        assert config.retry_count == 2
        assert config.retry_note == "上游 429 后重试成功"
        assert config.token_usage == {"prompt_tokens": 10, "total_tokens": 30}
        assert config.successful_model_alias == "别名"
    finally:
        await service.close()
