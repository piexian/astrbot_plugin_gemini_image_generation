from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.scheduler import JobResult
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
