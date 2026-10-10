from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.errors import (
    CoreError,
    InvalidRequestError,
    NoSupportedProviderError,
    ProviderRouteError,
)
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.router import ProviderCandidate, ProviderRouter


class UpstreamError(Exception):
    """模拟可重试的上游 5xx 错误。"""

    def __init__(self, message: str = "upstream 5xx", status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = True


class ScriptedProvider:
    """记录 contract 调用顺序、按脚本决定失败行为的 provider。"""

    def __init__(
        self,
        name: str = "p",
        *,
        supported: bool = True,
        fail_times: int = 0,
        error: Exception | None = None,
        supports_error: Exception | None = None,
        artifacts: tuple[str, ...] = ("a-1",),
    ) -> None:
        self.name = name
        self.supported = supported
        self.fail_times = fail_times
        self.error = error
        self.supports_error = supports_error
        self.artifacts = artifacts
        self.calls: list[str] = []
        self.build_count = 0
        self.send_count = 0
        self.bodies: list[dict] = []
        self.seen_prompts: list[str] = []

    async def supports(self, request):
        self.calls.append("supports")
        if self.supports_error is not None:
            raise self.supports_error
        return self.supported

    async def normalize(self, request):
        self.calls.append("normalize")
        self.seen_prompts.append(request.prompt)
        return request

    async def build_request(self, request, attempt):
        self.calls.append("build_request")
        self.build_count += 1
        return (
            "https://provider.test/v1",
            {"x-test": self.name},
            lambda n=attempt.number: {"n": n},
        )

    async def send(self, request, attempt, *, url, headers, body_factory):
        self.calls.append("send")
        self.send_count += 1
        body = body_factory()
        self.bodies.append(body)
        if self.send_count <= self.fail_times:
            raise self.error or UpstreamError()
        return {"sent": body}

    async def parse_response(self, response, *, request, attempt):
        self.calls.append("parse_response")
        return GenerationResult(artifacts=self.artifacts)


def make_request(**overrides) -> GenerationRequest:
    values: dict = {
        "prompt": "draw a cat",
        "source": "test",
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=5),
    }
    values.update(overrides)
    return GenerationRequest(**values)


@pytest.mark.asyncio
async def test_contract_order_per_candidate():
    first = ScriptedProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert first.calls == [
        "supports",
        "normalize",
        "build_request",
        "send",
        "parse_response",
    ]
    assert second.calls == ["supports"]
    assert result.artifacts == ("a-1",)
    assert result.provider == "first"
    assert result.candidate_id == "c1"


@pytest.mark.asyncio
async def test_retry_rebuilds_body_per_attempt():
    provider = ScriptedProvider("p", fail_times=1)
    router = ProviderRouter([provider], backoff=0.0)
    result = await router.execute(make_request())
    assert provider.build_count == 2
    assert provider.bodies == [{"n": 1}, {"n": 2}]
    assert result.attempt_count == 2


@pytest.mark.asyncio
async def test_retry_truncated_by_deadline_does_not_fall_back():
    class SlowFailProvider(ScriptedProvider):
        async def send(self, request, attempt, *, url, headers, body_factory):
            self.calls.append("send")
            self.send_count += 1
            await asyncio.sleep(0.02)
            raise UpstreamError()

    first = SlowFailProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")],
        backoff=0.005,
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.05)
    with pytest.raises(ProviderRouteError) as exc_info:
        await router.execute(make_request(), deadline_at=deadline)
    assert exc_info.value.code == "deadline_exceeded"
    assert exc_info.value.retryable is True
    assert len(exc_info.value.details["candidates"]) == 1
    assert second.send_count == 0


@pytest.mark.asyncio
async def test_send_truncated_by_remaining_deadline():
    class HangingProvider(ScriptedProvider):
        async def send(self, request, attempt, *, url, headers, body_factory):
            self.calls.append("send")
            self.send_count += 1
            await asyncio.sleep(5)

    provider = HangingProvider("p")
    router = ProviderRouter([provider], backoff=0.0)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.1)
    with pytest.raises(ProviderRouteError) as exc_info:
        await router.execute(make_request(), deadline_at=deadline)
    assert exc_info.value.code == "deadline_exceeded"
    assert provider.send_count == 1


@pytest.mark.asyncio
async def test_unsupported_candidate_is_skipped():
    first = ScriptedProvider("first", supported=False)
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert first.calls == ["supports"]
    assert second.send_count == 1
    assert result.provider == "second"
    assert result.candidate_id == "c2"


@pytest.mark.asyncio
async def test_non_retryable_error_falls_back_immediately():
    first = ScriptedProvider("first", fail_times=1, error=ValueError("bad prompt"))
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert first.send_count == 1
    assert second.send_count == 1
    assert result.provider == "second"
    assert result.attempt_count == 2


@pytest.mark.asyncio
async def test_candidate_id_selects_exclusively():
    first = ScriptedProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request(candidate_id="c2"))
    assert first.calls == []
    assert result.candidate_id == "c2"

    with pytest.raises(NoSupportedProviderError):
        await router.execute(make_request(candidate_id="missing"))


@pytest.mark.asyncio
async def test_provider_name_preference_reorders():
    first = ScriptedProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request(provider="second"))
    assert first.send_count == 0
    assert result.provider == "second"


@pytest.mark.asyncio
async def test_normalize_not_chained_across_candidates():
    class MutatingProvider(ScriptedProvider):
        async def normalize(self, request):
            self.calls.append("normalize")
            self.seen_prompts.append(request.prompt)
            return replace(request, prompt=request.prompt + "!")

    first = MutatingProvider("first", fail_times=1, error=ValueError("no"))
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert first.seen_prompts == ["draw a cat"]
    assert second.seen_prompts == ["draw a cat"]
    assert result.provider == "second"


@pytest.mark.asyncio
async def test_supports_failure_skips_candidate():
    first = ScriptedProvider("first", supports_error=RuntimeError("boom"))
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert result.provider == "second"


@pytest.mark.asyncio
async def test_all_candidates_fail_aggregates_outcomes():
    first = ScriptedProvider("first", fail_times=1, error=ValueError("bad"))
    second = ScriptedProvider(
        "second",
        fail_times=1,
        error=CoreError("denied", code="permission_denied", retryable=False),
    )
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    with pytest.raises(ProviderRouteError) as exc_info:
        await router.execute(make_request())
    error = exc_info.value
    assert error.code == "provider_route_failed"
    candidates = error.details["candidates"]
    assert [item["status"] for item in candidates] == ["failed", "failed"]
    assert candidates[0]["attempts"] == 1
    assert candidates[1]["attempts"] == 1
    assert candidates[1]["error_code"] == "permission_denied"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_candidate_error_messages_are_redacted():
    provider = ScriptedProvider(
        "p", fail_times=1, error=RuntimeError("api_key=sk-secret-123 expired")
    )
    router = ProviderRouter([provider])
    with pytest.raises(ProviderRouteError) as exc_info:
        await router.execute(make_request())
    message = exc_info.value.details["candidates"][0]["error_message"]
    assert "sk-secret-123" not in message


@pytest.mark.asyncio
async def test_invalid_build_request_output_falls_back():
    class BadBuildProvider(ScriptedProvider):
        async def build_request(self, request, attempt):
            self.calls.append("build_request")
            self.build_count += 1
            return "https://provider.test", {}

    first = BadBuildProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")]
    )
    result = await router.execute(make_request())
    assert first.build_count == 1
    assert first.send_count == 0
    assert second.send_count == 1
    assert result.provider == "second"


@pytest.mark.asyncio
async def test_attempt_timeout_falls_back_to_next_candidate():
    class TimeoutProvider(ScriptedProvider):
        async def send(self, request, attempt, *, url, headers, body_factory):
            self.calls.append("send")
            self.send_count += 1
            raise asyncio.TimeoutError("provider timeout")

    first = TimeoutProvider("first")
    second = ScriptedProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")],
        max_attempts=1,
    )
    result = await router.execute(make_request())
    assert first.send_count == 1
    assert result.provider == "second"


@pytest.mark.asyncio
async def test_sync_supports_and_normalize_are_supported():
    class SyncProvider(ScriptedProvider):
        def supports(self, request):
            self.calls.append("supports")
            return True

        def normalize(self, request):
            self.calls.append("normalize")
            return request

    provider = SyncProvider("sync")
    router = ProviderRouter([provider])
    result = await router.execute(make_request())
    assert provider.calls == [
        "supports",
        "normalize",
        "build_request",
        "send",
        "parse_response",
    ]
    assert result.has_output


@pytest.mark.asyncio
async def test_naive_deadline_rejected():
    router = ProviderRouter([ScriptedProvider("p")])
    with pytest.raises(InvalidRequestError):
        await router.execute(make_request(), deadline_at=datetime(2020, 1, 1))


def test_empty_candidates_rejected():
    with pytest.raises(InvalidRequestError):
        ProviderRouter([])
