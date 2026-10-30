"""命令入口在 v2 模式下的准入：额度只由调度器扣、被拒不先发进度提示。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_limit_admission import _Event
from tests.test_rate_limiter import config as limiter_config
from tests.test_rate_limiter import count
from tests.test_v2_admission import DecoratedClient, provider_failure
from tests.test_v2_command_paths import (
    _FakeV2Service,
    _make_generator,
    _make_plugin,
    _png_data_uri,
    _wire_sender,
)
from tl.api_types import APIError
from tl.core.errors import QueueFullError, QuotaExceededError, ServiceClosedError
from tl.core.requests import GenerationRequest
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_errors import api_failure_message
from tl.generation_scheduler import GenerationScheduler
from tl.image_generator import ImageGenerator
from tl.rate_limiter import RateLimiter
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR
from tl.v2_admission import LegacyFifoLimiter, RateLimiterQuota

UMO = "platform:FriendMessage:user-1"

# (命令方法, 参数, 是否启用表情包切割)：覆盖 _quick_generate_image 与 ImageGenerator 两条 v2 入口。
COMMANDS = [
    ("generate_image", ("draw",), False),
    ("modify_image", ("change",), False),
    ("quick_avatar", ("draw",), False),
    ("quick_sticker", ("draw",), False),
    ("quick_sticker", ("draw",), True),
    ("change_style", ("ink",), False),
]
COMMAND_IDS = [f"{name}-split" if split else name for name, _, split in COMMANDS]
PROGRESS_MARKERS = ("生成中", "开始转换风格")


class _AdmissionService(_FakeV2Service):
    """记录提交的请求，便于核对准入元数据。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests: list[GenerationRequest] = []

    async def submit(self, request, *, admission=None):
        self.requests.append(request)
        return await super().submit(request, admission=admission)


def _make_v2_plugin(service):
    plugin = _make_plugin()
    plugin.cfg.scheduler_v2 = True
    plugin._v2_service_for = lambda umo: service
    plugin.image_generator = ImageGenerator(
        context=None,
        api_client=plugin.api_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    _wire_sender(plugin)
    return plugin


async def _run(plugin, name, args, split, event=None):
    plugin.cfg.enable_sticker_split = split
    event = event or _Event()
    event.message_str = ""
    outputs = [result async for result in getattr(plugin, name)(event, *args)]
    return event, outputs


def _has_progress(outputs):
    return any(marker in str(item) for item in outputs for marker in PROGRESS_MARKERS)


# ===== 入口不再扣额度，准入上下文交给调度器 =====


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
async def test_v2_commands_leave_quota_to_the_scheduler(name, args, split):
    service = _AdmissionService(state=JobState.FAILED)
    plugin = _make_v2_plugin(service)

    event, outputs = await _run(plugin, name, args, split)

    submits = [call for call in service.calls if call[0] == "submit"]
    assert len(submits) == 1
    # 额度由 submit 准入扣：入口既不 check_and_consume，也不直接 acquire。
    assert plugin.rate_limiter.calls == []
    assert submits[0][2] == {"event": event}
    (request,) = service.requests
    assert request.metadata["quota"] == {"cost": 1}
    assert request.requester["umo"] == event.unified_msg_origin
    # 提交成功后才发进度提示，随后进入等待。
    assert _has_progress(outputs)
    assert any(call[0] == "wait" for call in service.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
async def test_v2_quota_denial_replies_without_any_progress_prompt(name, args, split):
    denial = QuotaExceededError(
        "chat exhausted", details={"scope": "session", "silent": False}
    )
    service = _AdmissionService(submit_error=denial)
    plugin = _make_v2_plugin(service)

    _, outputs = await _run(plugin, name, args, split)

    assert outputs[-1] == "chat exhausted"
    assert not _has_progress(outputs)
    # 被拒的请求没有 Job：既不等待也不取消。
    assert not [c for c in service.calls if c[0] in {"wait", "cancel"}]
    assert plugin.rate_limiter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
async def test_v2_group_denial_is_silent(name, args, split):
    async def outputs_for(error):
        plugin = _make_v2_plugin(_AdmissionService(submit_error=error))
        return (await _run(plugin, name, args, split))[1]

    denied = await outputs_for(
        QuotaExceededError("chat exhausted", details={"silent": False})
    )
    silent = await outputs_for(QuotaExceededError("", details={"silent": True}))

    # 静默拒绝只比普通拒绝少最后那条回复，且没有进度提示。
    assert denied == [*silent, "chat exhausted"]
    assert "" not in silent
    assert not _has_progress(silent)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
@pytest.mark.parametrize(
    ("error_cls", "error_type", "message"),
    [
        (QueueFullError, "queue_full", "生成队列已满，请稍后重试"),
        (ServiceClosedError, "service_closed", "生成服务正在关闭"),
    ],
)
async def test_v2_queue_rejection_matches_legacy_wording(
    name, args, split, error_cls, error_type, message
):
    # 旧路径：被 @scheduled_generation 装饰的客户端在调用时抛出 APIError。
    legacy = _make_plugin()
    legacy._v2_service_for = lambda umo: None
    legacy.api_client.generate_image = AsyncMock(
        side_effect=APIError(message, None, error_type, retryable=False)
    )
    legacy.image_generator = ImageGenerator(
        context=None,
        api_client=legacy.api_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    _wire_sender(legacy)
    _, legacy_outputs = await _run(legacy, name, args, split)

    service = _AdmissionService(submit_error=error_cls(message))
    _, v2_outputs = await _run(_make_v2_plugin(service), name, args, split)

    assert legacy_outputs[-1] == v2_outputs[-1]
    assert message in v2_outputs[-1]
    # 旧路径先发"生成中"再排队失败；v2 在提交时就被拒，不再发。
    assert _has_progress(legacy_outputs)
    assert not _has_progress(v2_outputs)


@pytest.mark.asyncio
async def test_route_check_without_quota_never_touches_limiter():
    plugin = _make_plugin()
    event = _Event()

    allowed = await plugin._check_command_generation_limit(
        event, has_reference_images=True, consume_quota=False
    )
    assert allowed == (True, None)
    assert plugin.rate_limiter.calls == []

    # 默认仍由入口扣一次，旧路径行为不变。
    assert await plugin._check_command_generation_limit(event) == (True, None)
    assert plugin.rate_limiter.calls == [(event.unified_msg_origin, 1)]

    # API 客户端未初始化时两种模式同样拒绝，且不再扣额度。
    plugin.rate_limiter.calls.clear()
    plugin.api_client = None
    for consume in (True, False):
        allowed, message = await plugin._check_command_generation_limit(
            event, consume_quota=consume
        )
        assert not allowed and "API 客户端未初始化" in message
    assert plugin.rate_limiter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
@pytest.mark.parametrize("abort", ["close", "cancel"])
async def test_v2_abort_at_progress_prompt_cancels_the_submitted_job(
    name, args, split, abort
):
    service = _AdmissionService()
    plugin = _make_v2_plugin(service)
    plugin.cfg.enable_sticker_split = split
    event = _Event()
    event.message_str = ""

    generator = getattr(plugin, name)(event, *args)
    async for item in generator:
        if _has_progress([item]):
            break
    if abort == "close":
        await generator.aclose()
    else:
        with pytest.raises(asyncio.CancelledError):
            await generator.athrow(asyncio.CancelledError())
    # 外层命令生成器不会同步关闭它转发的内层生成器；内层由事件循环的
    # 异步生成器终结钩子收尾，需要等它被调度。
    for _ in range(100):
        if ("cancel", "job-v2-1") in service.calls:
            break
        await asyncio.sleep(0.01)

    # 准入先于进度提示后，Job 在发出提示时已存在：此处被中止也要取消生成。
    assert ("cancel", "job-v2-1") in service.calls
    assert not [call for call in service.calls if call[0] == "wait"]


# ===== 真实栈：额度恰好扣一次且由调度器扣 =====


def _real_stack_plugin(tmp_path, client, limiter, fifo):
    service = build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (REFERENCE_IMAGE_CACHE_DIR,),
        quota=RateLimiterQuota(lambda: limiter),
        limiter=LegacyFifoLimiter(lambda: fifo),
    )
    plugin = _make_plugin()
    plugin.cfg.scheduler_v2 = True
    plugin.rate_limiter = limiter
    plugin.generation_scheduler = fifo
    plugin.api_client = SimpleNamespace(generate_image=client.generate_image)
    plugin._v2_service_for = lambda umo: service
    plugin.image_handler.fetch_images_from_event = AsyncMock(
        return_value=([_png_data_uri()], [])
    )
    plugin.image_generator = ImageGenerator(
        context=None,
        api_client=plugin.api_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    _wire_sender(plugin)
    return plugin, service


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
async def test_real_stack_charges_once_and_denies_the_next_command(
    tmp_path, name, args, split
):
    limiter = RateLimiter(limiter_config(session_max=1))
    consumed_by_entry = []
    original = limiter.check_and_consume

    async def spy(event, *, cost=1):
        consumed_by_entry.append(event)
        return await original(event, cost=cost)

    limiter.check_and_consume = spy
    fifo = GenerationScheduler(concurrency=2, queue_size=5)
    client = DecoratedClient(fifo, error=provider_failure())
    plugin, service = _real_stack_plugin(tmp_path, client, limiter, fifo)
    event = _Event()
    event.unified_msg_origin = UMO
    try:
        _, first = await _run(plugin, name, args, split, event)
        # 额度被调度器恰好扣一次；客户端直接使用 submit 时预留的队列票据。
        assert count(limiter, UMO) == 1
        assert client.snapshots == [(1, 0)]
        assert fifo.active == 0
        assert _has_progress(first)

        _, second = await _run(plugin, name, args, split, event)
        assert "已达上限" in second[-1]
        assert not _has_progress(second)
        assert count(limiter, UMO) == 1
        assert client.snapshots == [(1, 0)]
        assert consumed_by_entry == []
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "split"), COMMANDS, ids=COMMAND_IDS)
async def test_real_stack_queue_full_refunds_quota_and_matches_legacy_wording(
    tmp_path, name, args, split
):
    limiter = RateLimiter(limiter_config(session_max=2))
    fifo = GenerationScheduler(concurrency=1, queue_size=0)
    held = fifo.reserve()
    client = DecoratedClient(fifo)
    plugin, service = _real_stack_plugin(tmp_path, client, limiter, fifo)
    event = _Event()
    event.unified_msg_origin = UMO
    try:
        _, outputs = await _run(plugin, name, args, split, event)
        assert "生成队列已满，请稍后重试" in outputs[-1]
        assert not _has_progress(outputs)
        # 队列被拒后额度已退，桶回到提交之前；客户端没有被调用。
        assert not limiter._rate_limit_buckets
        assert client.snapshots == []
    finally:
        held.release()
        await service.close()


@pytest.mark.asyncio
async def test_real_stack_group_denial_is_silent_and_charges_nothing(tmp_path):
    cfg = limiter_config(session_max=2)
    cfg.group_limit_mode = "whitelist"
    cfg.group_limit_list = {"10002"}
    limiter = RateLimiter(cfg)
    # 命令起始的 allows_group 先放行，准入阶段 acquire 内的复查才拒绝
    # （模拟名单在两次检查之间被热更新）。
    start_check = limiter.allows_group
    calls = []

    def flipping(event):
        calls.append(event)
        return len(calls) == 1 or start_check(event)

    limiter.allows_group = flipping
    fifo = GenerationScheduler(concurrency=1, queue_size=1)
    client = DecoratedClient(fifo)
    plugin, service = _real_stack_plugin(tmp_path, client, limiter, fifo)
    event = _Event()
    event.unified_msg_origin = UMO
    event.group_id = "10001"
    try:
        _, outputs = await _run(plugin, "generate_image", ("draw",), False, event)
        assert not _has_progress(outputs)
        assert outputs == ["🎨 开始生成图像..."]
        assert not limiter._rate_limit_buckets
        assert client.snapshots == [] and fifo.active == 0
    finally:
        await service.close()


# ===== _ensure_v2_service 装配 =====


@pytest.mark.asyncio
async def test_ensure_v2_service_wires_quota_and_fifo_limiter(tmp_path):
    plugin = _make_plugin()
    _wire_sender(plugin)
    plugin._plugin_data_dir = str(tmp_path)
    plugin._v2_service = None
    started, release = asyncio.Event(), asyncio.Event()

    async def generate_image(config, **kwargs):
        started.set()
        await release.wait()
        return (["https://img.test/out.png"], [], "ok", None)

    plugin.api_client = SimpleNamespace(generate_image=generate_image)
    service = plugin._ensure_v2_service()
    try:
        assert service is not None and plugin._ensure_v2_service() is service
        # 装配时只记录取值函数：之后才赋值的限流器与队列同样生效。
        limiter = RateLimiter(limiter_config(session_max=1))
        fifo = GenerationScheduler(concurrency=1, queue_size=1)
        plugin.rate_limiter = limiter
        plugin.generation_scheduler = fifo
        request = GenerationRequest(
            prompt="draw", source="command", requester={"umo": UMO}
        )
        handle = await service.submit(request)
        await started.wait()
        assert count(limiter, UMO) == 1
        # 客户端未装饰，预留的票据由 Job 持有到结束。
        assert fifo.active == 1
        with pytest.raises(QuotaExceededError):
            await service.submit(request)
        release.set()
        result = await service.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        assert fifo.active == 0
    finally:
        release.set()
        await service.close()


# ===== ImageGenerator 两阶段 API =====


_SESSION_VAR: ContextVar[str | None] = ContextVar("submit_v2_binding", default=None)


@pytest.mark.asyncio
async def test_submit_v2_passes_admission_quota_and_session_umo():
    service = _AdmissionService()
    generator = _make_generator()
    event = SimpleNamespace(unified_msg_origin=UMO)

    submission = await generator.submit_v2(
        event=event,
        v2_service=service,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        admission={"event": event},
        quota={"cost": 2},
    )

    assert submission.handle.job_id == "job-v2-1"
    assert (submission.message, submission.silent) == (None, False)
    (request,) = service.requests
    assert request.metadata["quota"] == {"cost": 2}
    assert request.requester["umo"] == UMO
    assert service.calls[-1][2] == {"event": event}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quota", "expected"),
    [(None, "absent"), (False, False), ({"scope": "global"}, {"scope": "global"})],
)
async def test_submit_v2_quota_metadata_is_caller_controlled(quota, expected):
    service = _AdmissionService()
    await _make_generator().submit_v2(
        event=SimpleNamespace(unified_msg_origin=UMO),
        v2_service=service,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        quota=quota,
    )
    metadata = service.requests[0].metadata
    if expected == "absent":
        assert "quota" not in metadata
    else:
        assert metadata["quota"] == expected


@pytest.mark.asyncio
async def test_submit_v2_rejections_return_displayable_results():
    generator = _make_generator()
    event = SimpleNamespace(unified_msg_origin=UMO)

    async def submit_with(error):
        return await generator.submit_v2(
            event=event,
            v2_service=_AdmissionService(submit_error=error),
            prompt="draw",
            reference_images=[],
            avatar_reference=[],
        )

    denied = await submit_with(
        QuotaExceededError("额度不足", details={"silent": False})
    )
    assert (denied.handle, denied.message, denied.silent) == (None, "额度不足", False)

    silent = await submit_with(QuotaExceededError("", details={"silent": True}))
    assert (silent.handle, silent.silent) == (None, True)

    for error_cls, error_type, message in (
        (QueueFullError, "queue_full", "生成队列已满，请稍后重试"),
        (ServiceClosedError, "service_closed", "生成服务正在关闭"),
    ):
        rejected = await submit_with(error_cls(message))
        assert rejected.handle is None and not rejected.silent
        assert rejected.message == api_failure_message(
            message=message,
            status_code=None,
            error_type=error_type,
            total_timeout=generator.total_timeout,
        )

    crashed = await submit_with(RuntimeError("boom"))
    assert (crashed.handle, crashed.message) == (None, "❌ 生成图像时发生错误: boom")


@pytest.mark.asyncio
async def test_submit_v2_without_api_client_matches_legacy_message():
    generator = ImageGenerator(context=None, api_client=None)
    event = SimpleNamespace(unified_msg_origin=UMO)
    legacy_ok, legacy_message = await generator.generate_image_core(
        event, prompt="draw", reference_images=[], avatar_reference=[]
    )
    service = _AdmissionService()
    submission = await generator.submit_v2(
        event=event,
        v2_service=service,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
    )
    assert legacy_ok is False
    assert submission.handle is None and submission.message == legacy_message
    assert service.requests == []


@pytest.mark.asyncio
async def test_generate_image_core_v2_denials_become_failure_tuples():
    generator = _make_generator()
    event = SimpleNamespace(unified_msg_origin=UMO)
    common = {"prompt": "draw", "reference_images": [], "avatar_reference": []}

    denied = _AdmissionService(
        submit_error=QuotaExceededError("额度不足", details={"silent": False})
    )
    assert await generator.generate_image_core(event, v2_service=denied, **common) == (
        False,
        "额度不足",
    )
    silent = _AdmissionService(
        submit_error=QuotaExceededError("", details={"silent": True})
    )
    assert await generator.generate_image_core(event, v2_service=silent, **common) == (
        False,
        "",
    )


@pytest.mark.asyncio
async def test_generate_image_core_v2_forwards_admission_and_quota():
    service = _AdmissionService(state=JobState.FAILED)
    generator = _make_generator()
    event = SimpleNamespace(unified_msg_origin=UMO)

    await generator.generate_image_core(
        event,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        v2_service=service,
        v2_admission={"event": event},
        v2_quota=False,
    )

    assert service.calls[1] == ("submit", "draw", {"event": event})
    assert service.requests[0].metadata["quota"] is False


@pytest.mark.asyncio
async def test_submit_v2_runs_inside_session_binding_and_wait_runs_outside(
    monkeypatch,
):
    async def bind(event, context):
        return _SESSION_VAR.set("bound")

    monkeypatch.setattr("tl.image_generator.bind_session_image_dir", bind)
    monkeypatch.setattr(
        "tl.image_generator.reset_session_image_dir",
        lambda token: _SESSION_VAR.reset(token),
    )
    seen = {}

    class Service(_AdmissionService):
        async def submit(self, request, *, admission=None):
            seen["submit"] = _SESSION_VAR.get()
            # runtime task 在 submit 内创建，继承此刻的 contextvar。
            seen["task"] = await asyncio.create_task(self._read())
            return await super().submit(request, admission=admission)

        async def _read(self):
            return _SESSION_VAR.get()

        async def wait(self, job_id, timeout=None):
            seen["wait"] = _SESSION_VAR.get()
            return await super().wait(job_id, timeout=timeout)

    service = Service(state=JobState.FAILED)
    generator = _make_generator()
    event = SimpleNamespace(unified_msg_origin=UMO)
    submission = await generator.submit_v2(
        event=event,
        v2_service=service,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
    )
    assert _SESSION_VAR.get() is None
    await generator.wait_v2(v2_service=service, handle=submission.handle)

    assert seen == {"submit": "bound", "task": "bound", "wait": None}
