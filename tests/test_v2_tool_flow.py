"""LLM 工具单图在 v2 下的入口行为：前台完成、转后台、取消、额度与参考图。"""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from PIL import Image

# 复用现有宿主 stub（mcp / astrbot 桩）、事件假对象与限流器配置。
from tests.test_llm_routing_and_tasks import _context, _Event
from tests.test_rate_limiter import config as limiter_config
from tests.test_rate_limiter import count
from tl import llm_tools
from tl.api_types import APIError
from tl.background_tasks import BackgroundTaskManager
from tl.core.errors import QuotaExceededError
from tl.core.results import GenerationResult
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_errors import IMAGE_FILE_MISSING_MESSAGE, api_failure_message
from tl.image_generator import ImageGenerator
from tl.llm_tools import GeminiImageGenerationTool
from tl.plugin_config import ProviderCandidate
from tl.rate_limiter import RateLimiter
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR
from tl.v2_admission import RateLimiterQuota
from tl.v2_bridge import v2_failure_text

UMO = _Event.unified_msg_origin
FULL_UMO = "platform:FriendMessage:user-1"
URL = "https://img.test/out.png"
QUOTA_DENIED = "该会话已达上限"


def png_bytes(color=(9, 8, 7)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class GatedClient:
    """假的 legacy 客户端：记录入参，可停在闸门上，可抛出预设错误。"""

    def __init__(self, *, urls=(URL,), paths=(), text="ok", gate=None, error=None):
        self.urls = list(urls)
        self.paths = list(paths)
        self.text = text
        self.gate = gate
        self.error = error
        self.configs = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return list(self.urls), list(self.paths), self.text, None


class FakeMcpTypes:
    """CallToolResult 构建只需要这三个类型；宿主环境没有真实 mcp。"""

    class TextContent:
        def __init__(self, type, text):
            self.type = type
            self.text = text

    class ImageContent:
        def __init__(self, type, data, mimeType):
            self.type = type
            self.data = data
            self.mimeType = mimeType

    class CallToolResult:
        def __init__(self, content):
            self.content = content


def texts(call_tool_result) -> str:
    return "\n".join(
        item.text for item in call_tool_result.content if hasattr(item, "text")
    )


class Notifier:
    """替身 notify_llm_background_result / report_background_failure。"""

    def __init__(self):
        self.ok = True
        self.results: list[dict] = []
        self.failures: list[tuple] = []

    async def notify(self, plugin, event, task_result, *, notice, scene):
        self.results.append(task_result)
        return self.ok

    async def report(self, plugin, event, failure_summary, *, scene, task_id=None):
        self.failures.append((failure_summary, task_id))
        return self.ok


class AsyncioView:
    """llm_tools 看到的 asyncio：转发一切，但禁止 create_task，可覆盖个别函数。"""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name == "create_task":
            raise AssertionError("v2 分支不得调用 asyncio.create_task")
        if name in self._overrides:
            return self._overrides[name]
        return getattr(asyncio, name)


async def eventually(check, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("条件在限定时间内未成立")
        await asyncio.sleep(0.01)


def make_plugin(tmp_path, service, client, *, allowed_dirs=(), candidates=()):
    return SimpleNamespace(
        api_client=SimpleNamespace(proxy=None),
        cfg=SimpleNamespace(
            provider_candidates=list(candidates),
            preserve_reference_image_size=False,
            llm_tool_reference_path_mode="whitelist",
            llm_tool_reference_allowed_dirs=[str(path) for path in allowed_dirs],
            llm_tool_timeout_reserve_percent=50,
            batch_max_tasks=5,
            batch_max_images_per_task=4,
            batch_concurrency=2,
        ),
        image_generator=ImageGenerator(
            context=None,
            api_client=client,
            filter_valid_fn=lambda images, source: images or [],
        ),
        message_sender=SimpleNamespace(
            merge_available_images=lambda urls, paths: [*(urls or []), *(paths or [])],
            prepare_text_content=lambda text, refs: text or "",
        ),
        background_task_manager=BackgroundTaskManager(tmp_path / "bg"),
        _check_and_consume_limit=AsyncMock(return_value=(True, None)),
        _fetch_images_from_event=AsyncMock(return_value=([], [])),
        _v2_service_for=Mock(return_value=service),
        get_tool_timeout=lambda event: 10,
    )


@pytest.fixture
def notifier(monkeypatch):
    recorder = Notifier()
    monkeypatch.setattr(
        "tl.background_dispatch.notify_llm_background_result", recorder.notify
    )
    monkeypatch.setattr(
        "tl.background_dispatch.report_background_failure", recorder.report
    )
    monkeypatch.setattr(
        "tl.v2_tool_delivery.report_background_failure", recorder.report
    )
    monkeypatch.setattr(
        "tl.batch_generation.notify_llm_background_result", recorder.notify
    )
    monkeypatch.setattr(
        "tl.batch_generation.report_background_failure", recorder.report
    )
    return recorder


@pytest.fixture
def fake_mcp(monkeypatch):
    monkeypatch.setattr(llm_tools, "mcp", SimpleNamespace(types=FakeMcpTypes))


@pytest_asyncio.fixture
async def make_env(tmp_path, monkeypatch, fake_mcp):
    """真实 v2 栈 + 假客户端的工具环境，并封死 v2 分支不该碰的旧路径机制。"""

    built = []

    async def build(
        client=None,
        *,
        foreground=5,
        service_options=None,
        asyncio_overrides=None,
        **plugin_options,
    ):
        client = client or GatedClient()
        service = build_generation_stack(
            client,
            [{"provider": "legacy", "model": "auto"}],
            tmp_path / "store",
            (REFERENCE_IMAGE_CACHE_DIR,),
            **(service_options or {}),
        )
        plugin = make_plugin(tmp_path, service, client, **plugin_options)
        monkeypatch.setattr(
            llm_tools, "_resolve_foreground_wait_seconds", lambda *_: foreground
        )
        monkeypatch.setattr(
            llm_tools, "asyncio", AsyncioView(**(asyncio_overrides or {}))
        )
        for name in ("invoke_generation_core", "_create_generation_task"):
            monkeypatch.setattr(llm_tools, name, Mock(side_effect=AssertionError(name)))
        monkeypatch.setattr(
            BackgroundTaskManager, "attach", Mock(side_effect=AssertionError("attach"))
        )
        built.append((service, plugin))
        return SimpleNamespace(
            client=client,
            service=service,
            plugin=plugin,
            manager=plugin.background_task_manager,
            tool=GeminiImageGenerationTool(plugin=plugin),
        )

    yield build
    for service, plugin in built:
        await service.close()
        await plugin.background_task_manager.close()
        plugin._check_and_consume_limit.assert_not_awaited()


async def call(env, event=None, **kwargs):
    kwargs.setdefault("prompt", "draw a cat")
    return await env.tool.call(_context(event or _Event()), **kwargs)


async def record_of(env, task_id):
    return await env.manager.get(task_id, UMO)


async def stored_jobs(env):
    return await env.service._scheduler.store.list_jobs()


# ===== 前台按时完成 =====


@pytest.mark.asyncio
async def test_on_time_completion_returns_call_tool_result(make_env, fake_mcp):
    env = await make_env()

    result = await call(env, resolution="2K", aspect_ratio="3:2")

    assert isinstance(result, FakeMcpTypes.CallToolResult)
    assert URL in texts(result) and "ok" in texts(result)
    assert env.manager._records == {}
    (config,) = env.client.configs
    assert (config.prompt, config.resolution, config.aspect_ratio) == (
        "draw a cat",
        "2K",
        "3:2",
    )
    assert config.image_count == 1
    env.plugin._v2_service_for.assert_called_once_with(UMO)
    (job,) = await stored_jobs(env)
    assert job.state is JobState.SUCCEEDED
    assert job.request.source == "llm_tool"
    assert job.request.metadata["require_images"] is True
    assert env.service._scheduler._deliveries == {}


@pytest.mark.asyncio
async def test_foreground_failure_uses_legacy_wording(make_env):
    error = APIError("限流", 429, "rate_limit", retryable=False)
    env = await make_env(GatedClient(error=error))

    result = await call(env)

    assert result == api_failure_message(
        message="限流", status_code=429, error_type=None, total_timeout=120
    )
    assert env.manager._records == {}


@pytest.mark.asyncio
async def test_build_timeout_returns_image_locations_instead_of_background(
    make_env, monkeypatch
):
    real_wait_for = asyncio.wait_for

    def fast_wait_for(awaitable, timeout):
        # 结果构建的 30 秒上限缩成毫秒，其余等待保持原样。
        return real_wait_for(awaitable, 0.05 if timeout == 30 else timeout)

    async def never_built(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(llm_tools, "_build_call_tool_result", never_built)
    env = await make_env(
        GatedClient(urls=[URL], paths=["/tmp/local-out.png"]),
        asyncio_overrides={"wait_for": fast_wait_for},
    )

    result = await call(env)

    assert isinstance(result, str)
    assert "send_message_to_user" in result and "image 组件" in result
    assert f"图片1 (URL): {URL}" in result
    assert "图片2 (本地路径): /tmp/local-out.png" in result
    # Job 已结束，无法再挂投递：不建后台记录。
    assert env.manager._records == {}
    assert env.service._scheduler._deliveries == {}
    (job,) = await stored_jobs(env)
    assert job.state is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_text_only_result_returns_the_legacy_missing_file_text(make_env):
    env = await make_env(GatedClient(urls=[], text="只有文本"))

    result = await call(env)

    # 与旧路径一致：没有图片就是失败，哪怕供应商返回了文本。
    assert result == IMAGE_FILE_MISSING_MESSAGE
    assert env.manager._records == {}
    (job,) = await stored_jobs(env)
    assert job.state is JobState.FAILED
    assert job.error_code == "provider_no_image"


def test_image_location_notice_appends_config_notice():
    text = llm_tools._build_image_location_notice([URL], "配置提示")

    assert text.splitlines()[-1] == "配置提示"
    assert "send_message_to_user" in text


# ===== 转后台与投递 =====


@pytest.mark.asyncio
async def test_timeout_moves_to_background_and_delivers_through_adapter(
    make_env, notifier
):
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0.05)

    result = await call(env)

    payload = json.loads(result)
    assert payload["status"] == "running"
    assert payload["routing_mode"] == "full_polling"
    assert "[图像生成任务已转入后台]" in payload["message"]
    task_id = payload["task_id"]
    record = await record_of(env, task_id)
    assert record["kind"] == "single" and record["status"] == "running"
    assert env.manager._runtime_tasks == {}

    gate.set()

    async def delivered():
        return (await record_of(env, task_id))["status"] == "succeeded"

    await eventually(delivered)
    record = await record_of(env, task_id)
    assert notifier.results == [
        {
            "task_id": task_id,
            "status": "succeeded",
            "image_urls": [URL],
            "image_paths": [],
            "text_content": "ok",
        }
    ]
    assert record["message"] == "图片生成已完成并发送"
    assert record["items"][0]["delivery_success"] is True

    # 记录里的 job_id 就是调度器的 Job，且该 Job 经投递阶段正常结束。
    async def job_succeeded():
        status = await env.service.get_status(record["job_id"])
        return status.state is JobState.SUCCEEDED

    await eventually(job_succeeded)


@pytest.mark.asyncio
async def test_zero_foreground_budget_goes_background_immediately(make_env, notifier):
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0)

    result = await call(env)

    payload = json.loads(result)
    assert "[图像生成任务已启动]" in payload["message"]
    assert "前台等待" not in payload["message"]
    task_id = payload["task_id"]
    gate.set()

    async def delivered():
        return bool(notifier.results)

    await eventually(delivered)
    assert notifier.results[0]["task_id"] == task_id


@pytest.mark.asyncio
async def test_undelivered_background_result_is_recorded_as_partial(make_env, notifier):
    notifier.ok = False
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0)

    task_id = json.loads(await call(env))["task_id"]
    gate.set()

    async def finished():
        record = await record_of(env, task_id)
        return record["status"] != "running"

    await eventually(finished)
    record = await record_of(env, task_id)
    assert record["status"] == "partial_success"

    # 未送达：Job 以 PARTIAL + delivery_failed 结束，结果仍保留。
    async def job_partial():
        status = await env.service.get_status(record["job_id"])
        return status.state is JobState.PARTIAL

    await eventually(job_partial)
    status = await env.service.get_status(record["job_id"])
    assert status.error_code == "delivery_failed"
    assert status.result is not None


@pytest.mark.asyncio
async def test_background_failure_is_reported_and_recorded(make_env, notifier):
    gate = asyncio.Event()
    error = APIError("限流", 429, "rate_limit", retryable=False)
    env = await make_env(GatedClient(gate=gate, error=error), foreground=0)

    task_id = json.loads(await call(env))["task_id"]
    gate.set()

    async def reported():
        return bool(notifier.failures)

    await eventually(reported)
    expected = api_failure_message(
        message="限流", status_code=429, error_type=None, total_timeout=120
    )
    assert notifier.failures == [(expected, task_id)]

    async def failed():
        return (await record_of(env, task_id))["status"] == "failed"

    await eventually(failed)
    record = await record_of(env, task_id)
    assert record["message"] == expected
    assert (await env.service.get_status(record["job_id"])).state is JobState.FAILED


@pytest.mark.asyncio
async def test_background_text_only_result_is_reported_as_failure(make_env, notifier):
    gate = asyncio.Event()
    env = await make_env(GatedClient(urls=[], text="只有文本", gate=gate), foreground=0)

    task_id = json.loads(await call(env))["task_id"]
    gate.set()

    async def failed():
        return (await record_of(env, task_id))["status"] == "failed"

    await eventually(failed)
    assert notifier.failures == [(IMAGE_FILE_MISSING_MESSAGE, task_id)]
    assert notifier.results == []
    assert (await record_of(env, task_id))["message"] == IMAGE_FILE_MISSING_MESSAGE


@pytest.mark.asyncio
async def test_cancelled_background_job_marks_record_interrupted(make_env, notifier):
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0)
    task_id = json.loads(await call(env))["task_id"]
    await asyncio.wait_for(env.client.started.wait(), 2)
    (job,) = await stored_jobs(env)

    await env.service.cancel(job.job_id)

    record = await record_of(env, task_id)
    assert record["status"] == "interrupted"
    assert record["job_id"] == job.job_id
    # 取消不是失败：不唤醒 Agent，也不发失败提示。
    assert notifier.failures == [] and notifier.results == []


@pytest.mark.asyncio
async def test_record_creation_failure_does_not_stall_the_delivery(
    make_env, notifier, monkeypatch
):
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0)

    async def broken_create(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(env.manager, "create", broken_create)

    result = await call(env)

    assert result == "❌ 图片生成过程中出错：disk full"
    gate.set()

    async def delivered():
        return bool(notifier.results)

    # 没有记录可绑定：投递方不必等满绑定超时，结果仍经 Agent 送达（无任务号）。
    await eventually(delivered, timeout=2.0)
    assert notifier.results[0]["task_id"] == ""


# ===== 判定点竞态 =====


class ScriptedService:
    """脚本化的 v2 服务：wait 依次返回或抛出预设结果，并记录调用顺序。"""

    def __init__(self, waits, *, attach=True, submit_error=None):
        self.waits = list(waits)
        self.attach = attach
        self.submit_error = submit_error
        self.calls: list[tuple] = []

    def result_wait_timeout(self, deadline_seconds):
        return deadline_seconds + 50

    async def prepare_references(self, references, *, requester, trusted_local=False):
        return tuple(references)

    async def submit(self, request, *, admission=None, delivery=None):
        if self.submit_error is not None:
            raise self.submit_error
        self.calls.append(("submit", request.metadata.get("quota"), admission))
        return SimpleNamespace(job_id="job-fake-1")

    def attach_delivery(self, job_id, delivery):
        self.calls.append(("attach", job_id))
        return self.attach

    async def wait(self, job_id, timeout=None):
        self.calls.append(("wait", timeout))
        outcome = self.waits.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def cancel(self, job_id):
        self.calls.append(("cancel", job_id))


def finished_job(state=JobState.SUCCEEDED, **fields):
    return SimpleNamespace(
        state=state,
        result=GenerationResult(artifacts=(URL,), text="ok")
        if state is JobState.SUCCEEDED
        else None,
        error_code=fields.get("error_code"),
        error_message=fields.get("error_message"),
    )


@pytest.fixture
def scripted(tmp_path, monkeypatch, fake_mcp):
    """用脚本化服务驱动工具；foreground 由用例通过返回值设置。"""

    built = []

    def build(service, *, foreground=5, **plugin_options):
        monkeypatch.setattr(
            llm_tools, "_resolve_foreground_wait_seconds", lambda *_: foreground
        )
        client = GatedClient()
        plugin = make_plugin(tmp_path, service, client, **plugin_options)
        built.append(plugin)
        return SimpleNamespace(
            service=service,
            plugin=plugin,
            manager=plugin.background_task_manager,
            tool=GeminiImageGenerationTool(plugin=plugin),
        )

    yield build
    for plugin in built:
        plugin._check_and_consume_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_race_after_decision_point_takes_foreground_path(scripted):
    service = ScriptedService([asyncio.TimeoutError(), finished_job()], attach=False)
    env = scripted(service, foreground=0.5)

    result = await call(env)

    assert isinstance(result, FakeMcpTypes.CallToolResult)
    assert [call[0] for call in service.calls] == ["submit", "wait", "attach", "wait"]
    # 判定点已过：拿结果用调度器给出的等待上限，不建后台记录、不取消。
    assert service.calls[3] == ("wait", service.result_wait_timeout(120))
    assert env.manager._records == {}


@pytest.mark.asyncio
async def test_zero_budget_race_takes_foreground_path(scripted):
    service = ScriptedService([finished_job()], attach=False)
    env = scripted(service, foreground=0)

    result = await call(env)

    assert isinstance(result, FakeMcpTypes.CallToolResult)
    assert [call[0] for call in service.calls] == ["submit", "attach", "wait"]
    assert env.manager._records == {}


@pytest.mark.asyncio
async def test_result_wait_timeout_after_failed_attach_cancels_job(scripted):
    service = ScriptedService(
        [asyncio.TimeoutError(), asyncio.TimeoutError()], attach=False
    )
    env = scripted(service, foreground=0.5)

    result = await call(env)

    assert result == api_failure_message(
        message="生成等待超时",
        status_code=None,
        error_type="timeout",
        total_timeout=120,
    )
    assert service.calls[-1] == ("cancel", "job-fake-1")


@pytest.mark.asyncio
async def test_wait_error_cancels_job_and_reports(scripted):
    service = ScriptedService([RuntimeError("store down")])
    env = scripted(service, foreground=0.5)

    result = await call(env)

    assert result == "❌ 图片生成过程中出错：store down"
    assert service.calls[-1] == ("cancel", "job-fake-1")
    assert env.manager._records == {}


@pytest.mark.asyncio
async def test_real_scheduler_race_between_timeout_and_decision_point(
    make_env, notifier, fake_mcp
):
    """超时触发后、工具恢复前 Job 已走完：attach 被拒，按前台处理。"""

    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=0.05)
    real_wait = env.service.wait

    async def racing_wait(job_id, timeout=None):
        if timeout != 0.05:
            return await real_wait(job_id, timeout=timeout)
        try:
            return await real_wait(job_id, timeout=timeout)
        except asyncio.TimeoutError:
            # 工具恢复之前 Job 已走完：此时登记投递必然被拒绝。
            gate.set()
            await real_wait(job_id, timeout=5)
            raise

    env.service.wait = racing_wait

    result = await call(env)

    assert isinstance(result, FakeMcpTypes.CallToolResult)
    assert env.manager._records == {}
    assert notifier.results == []


# ===== 取消 =====


@pytest.mark.asyncio
@pytest.mark.parametrize("for_forum", [False, True])
async def test_cancelling_the_tool_call_cancels_the_job(make_env, for_forum):
    gate = asyncio.Event()
    env = await make_env(GatedClient(gate=gate), foreground=30)

    task = asyncio.create_task(call(env, for_forum=for_forum))
    await asyncio.wait_for(env.client.started.wait(), 2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    (job,) = await stored_jobs(env)
    assert job.state is JobState.CANCELLED
    assert env.client.cancelled
    assert env.manager._records == {}
    assert env.service._scheduler._tasks == {}


# ===== for_forum =====


class LegacyPlugin:
    """旧路径的最小插件：经 ImageGenerator 的 legacy 分支生成。"""

    def __init__(self, tmp_path, client):
        self.generator = ImageGenerator(
            context=None,
            api_client=client,
            filter_valid_fn=lambda images, source: images or [],
        )
        base = make_plugin(tmp_path, None, client)
        self.__dict__.update(base.__dict__)
        self.image_generator = self.generator

    async def _generate_image_core_internal(self, **kwargs):
        return await self.generator.generate_image_core(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "text_only"])
async def test_forum_text_matches_legacy_path(tmp_path, make_env, outcome):
    local = tmp_path / "forum-out.png"
    local.write_bytes(png_bytes())
    error = APIError("限流", 429, "rate_limit", retryable=False)

    def client():
        if outcome == "success":
            return GatedClient(urls=[URL], paths=[str(local)], text="森林里的小狐狸")
        if outcome == "text_only":
            return GatedClient(urls=[], text="只有文本")
        return GatedClient(error=error)

    legacy_plugin = LegacyPlugin(tmp_path / "legacy", client())
    legacy_plugin._v2_service_for = lambda umo: None
    legacy = await GeminiImageGenerationTool(plugin=legacy_plugin).call(
        _context(_Event()), prompt="draw a cat", for_forum=True
    )
    # 旧路径经 _check_and_consume_limit 扣额度；v2 的额度由调度器扣。
    legacy_plugin._check_and_consume_limit.assert_awaited_once()

    env = await make_env(client())
    v2_text = await call(env, for_forum=True)

    assert v2_text == legacy
    assert ("[图像生成完成 - 论坛发帖模式]" in v2_text) == (outcome == "success")


# ===== 额度与准入 =====


class RecordingQuota:
    def __init__(self):
        self.calls = []

    async def reserve(self, request, admission):
        self.calls.append(
            (request.metadata.get("quota"), dict(request.requester), admission)
        )
        return None


@pytest.mark.asyncio
async def test_quota_is_reserved_at_submit_with_event_and_cost_one(make_env, fake_mcp):
    quota = RecordingQuota()
    env = await make_env(service_options={"quota": quota})
    event = _Event()

    await call(env, event)

    ((quota_option, requester, admission),) = quota.calls
    assert quota_option == {"cost": 1}
    assert requester["umo"] == UMO
    assert admission == {"event": event}


@pytest.mark.asyncio
async def test_real_limiter_charges_once_and_denial_uses_its_message(
    make_env, fake_mcp
):
    limiter = RateLimiter(limiter_config(session_max=1))
    env = await make_env(service_options={"quota": RateLimiterQuota(lambda: limiter)})
    # 真实限流器要求完整 UMO（platform:MessageType:session）。
    event = _Event()
    event.unified_msg_origin = FULL_UMO
    try:
        first = await call(env, event)
        second = await call(env, event)

        assert isinstance(first, FakeMcpTypes.CallToolResult)
        assert "已达上限" in second
        assert count(limiter, FULL_UMO) == 1
        assert len(env.client.configs) == 1
    finally:
        await limiter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (QuotaExceededError(QUOTA_DENIED, details={"silent": False}), QUOTA_DENIED),
        # 群名单静默拒绝没有消息，沿用旧路径的兜底文案。
        (QuotaExceededError("", details={"silent": True}), "请求过于频繁，请稍后再试"),
    ],
)
async def test_quota_rejection_text_matches_legacy(scripted, error, expected):
    service = ScriptedService([], submit_error=error)
    env = scripted(service)

    result = await call(env)

    assert result == expected
    assert service.calls == []


# ===== 参考图 =====


@pytest.mark.asyncio
async def test_reference_image_paths_are_ingested_as_trusted_local(make_env, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "ref.png"
    payload = png_bytes((1, 2, 3))
    source.write_bytes(payload)
    env = await make_env(allowed_dirs=[allowed])

    await call(env, reference_image_paths=[str(source)])

    (config,) = env.client.configs
    (reference,) = config.reference_images
    # PathPolicy 只放行参考图缓存目录：本地图经 trusted_local 换成句柄后落到缓存。
    assert reference != str(source)
    assert reference.startswith(str(REFERENCE_IMAGE_CACHE_DIR))
    with open(reference, "rb") as handle:
        assert handle.read() == payload
    tmp_store = env.service._reference_service._stores["tmp"]
    assert len(tmp_store._entries) == 1


@pytest.mark.asyncio
async def test_oversized_local_reference_fails_before_any_job(make_env, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "ref.png"
    source.write_bytes(png_bytes())
    env = await make_env(
        allowed_dirs=[allowed], service_options={"max_reference_bytes": 8}
    )

    result = await call(env, reference_image_paths=[str(source)])

    assert result == v2_failure_text(
        "reference_too_large", "参考图超过 8 字节上限", total_timeout=120
    )
    assert env.client.configs == []
    assert await stored_jobs(env) == []


@pytest.mark.asyncio
async def test_event_inline_reference_still_becomes_a_handle(make_env):
    env = await make_env()
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    env.plugin._fetch_images_from_event.return_value = ([data_uri], [])

    await call(env, use_reference_images=True)

    (config,) = env.client.configs
    assert config.reference_images[0].startswith(str(REFERENCE_IMAGE_CACHE_DIR))


# ===== 校验先于 v2 分支 =====


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_reference", "未取得所需参考图片，未启动生成"),
        ("missing_avatar", "未取得所需用户头像，未启动生成"),
        ("size_conflict", "参数冲突"),
        ("no_candidate", "没有匹配所选供应商"),
    ],
)
async def test_existing_validation_precedes_v2_and_charges_nothing(
    make_env, case, expected
):
    quota = RecordingQuota()
    candidates = [
        ProviderCandidate(
            id="google#1",
            api_type="google",
            settings={"api_keys": ["key"], "model": "image-model"},
            supports_image_edit=True,
        )
    ]
    env = await make_env(
        service_options={"quota": quota},
        candidates=candidates if case == "no_candidate" else (),
    )
    env.plugin.image_generator.submit_v2 = AsyncMock()
    kwargs = {}
    if case == "missing_reference":
        kwargs = {"use_reference_images": True}
    elif case == "missing_avatar":
        kwargs = {"include_user_avatar": True}
    elif case == "size_conflict":
        env.plugin._fetch_images_from_event.return_value = (["ref"], [])
        env.plugin.cfg.preserve_reference_image_size = True
        kwargs = {"use_reference_images": True, "resolution": "1K"}
    else:
        kwargs = {"model": "unknown"}

    result = await call(env, **kwargs)

    assert expected in result
    # 先于 v2 分支：不碰服务、不提交、不扣额度。
    env.plugin._v2_service_for.assert_not_called()
    env.plugin.image_generator.submit_v2.assert_not_awaited()
    assert quota.calls == []
    assert await stored_jobs(env) == []


# ===== 灰度之外与提交阶段参数 =====


@pytest.mark.asyncio
async def test_without_v2_service_the_legacy_flow_is_untouched(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, None, GatedClient())
    plugin._v2_service_for = Mock(return_value=None)
    plugin.image_generator.submit_v2 = AsyncMock()
    generate = AsyncMock(return_value=(False, "provider failed"))
    monkeypatch.setattr(llm_tools, "invoke_generation_core", generate)

    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _context(_Event()), prompt="draw", for_forum=True
    )

    assert result == "❌ 图片生成失败：provider failed"
    plugin._check_and_consume_limit.assert_awaited_once()
    plugin._v2_service_for.assert_called_once_with(UMO)
    plugin.image_generator.submit_v2.assert_not_awaited()
    generate.assert_awaited_once()
    await plugin.background_task_manager.close()


class StrictService(ScriptedService):
    """prepare_references 不带 trusted_local 形参的旧式服务实现。"""

    def __init__(self):
        super().__init__([])
        self.prepare_kwargs = []

    async def prepare_references(self, references, *, requester):
        self.prepare_kwargs.append({})
        return tuple(references)


class TrustingService(ScriptedService):
    def __init__(self):
        super().__init__([])
        self.prepare_kwargs = []

    async def prepare_references(self, references, *, requester, trusted_local=False):
        self.prepare_kwargs.append({"trusted_local": trusted_local})
        return tuple(references)


@pytest.mark.asyncio
async def test_submit_v2_passes_trusted_local_only_when_requested():
    generator = ImageGenerator(
        context=None,
        api_client=object(),
        filter_valid_fn=lambda images, source: images or [],
    )
    strict, trusting = StrictService(), TrustingService()

    for service, trusted in ((strict, False), (trusting, False), (trusting, True)):
        submission = await generator.submit_v2(
            event=SimpleNamespace(unified_msg_origin="u"),
            v2_service=service,
            prompt="draw",
            reference_images=["data:image/png;base64,xx"],
            avatar_reference=[],
            trusted_local=trusted,
        )
        assert submission.handle is not None

    # 默认不传该参数（旧式服务实现照常工作），需要时才显式传 True。
    assert strict.prepare_kwargs == [{}]
    assert trusting.prepare_kwargs == [
        {"trusted_local": False},
        {"trusted_local": True},
    ]


def test_forbid_guard_detects_create_task(monkeypatch):
    # 守卫自检：v2 用例里的 asyncio 视图确实会拒绝 create_task。
    view = AsyncioView()
    with pytest.raises(AssertionError, match="create_task"):
        getattr(view, "create_task")
    assert view.wait_for is asyncio.wait_for


# ===== 批量入口 =====


class BatchClient:
    """每轮最多返回一张新图，让多项与补足轮次都可核对。"""

    def __init__(self):
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        serial = sum(item.prompt == config.prompt for item in self.configs)
        return [f"https://img.test/{config.prompt}-{serial}.png"], [], "ok", None


def batch_candidates():
    return [
        ProviderCandidate(
            id="legacy#1",
            api_type="legacy",
            settings={"api_keys": ["key"], "model": "auto"},
            supports_image_edit=True,
        )
    ]


def batch_tasks():
    return [
        {"name": "a", "prompt": "a", "image_count": 1, "provider": "legacy"},
        {"name": "b", "prompt": "b", "image_count": 2, "provider": "legacy"},
    ]


@pytest.mark.asyncio
async def test_batch_v2_uses_parent_children_and_delivers_without_legacy_task(
    make_env, notifier, monkeypatch
):
    quota = RecordingQuota()
    client = BatchClient()
    env = await make_env(
        client,
        service_options={"quota": quota},
        candidates=batch_candidates(),
    )
    monkeypatch.setattr(
        llm_tools, "run_batch_job", Mock(side_effect=AssertionError("legacy batch"))
    )

    event = _Event()
    payload = json.loads(await call(env, event, batch_tasks=batch_tasks()))

    assert payload["status"] == "running"
    task_id = payload["task_id"]
    assert env.manager._runtime_tasks == {}

    async def delivered():
        return bool(notifier.results)

    await eventually(delivered)
    record = await record_of(env, task_id)
    assert record["kind"] == "batch"
    assert record["status"] == "succeeded"
    assert (record["completed_items"], record["succeeded_items"]) == (2, 2)
    assert record["failed_items"] == 0
    assert record["job_id"]
    assert notifier.results[0]["task_id"] == task_id
    assert [item["name"] for item in notifier.results[0]["items"]] == ["a", "b"]

    jobs = await stored_jobs(env)
    assert len(jobs) == 3
    parent = next(job for job in jobs if job.request.metadata.get("batch_parent"))
    children = [job for job in jobs if job.parent_job_id == parent.job_id]
    assert len(children) == 2
    assert parent.job_id == record["job_id"]
    assert parent.state is JobState.SUCCEEDED
    assert parent.deadline_at is None
    assert parent.request.metadata["quota"] == {"cost": 2}
    assert parent.request.prompt == "批量生图：a、b"
    assert [child.request.source for child in children] == ["llm_batch"] * 2
    assert all(child.request.metadata["quota"] is False for child in children)
    assert all(child.request.metadata["fill_image_count"] is True for child in children)
    assert all(child.deadline_at is not None for child in children)
    assert {child.request.metadata["deadline_seconds"] for child in children} == {
        120,
        240,
    }

    ((quota_option, requester, admission),) = quota.calls
    assert quota_option == {"cost": 2}
    assert requester["umo"] == UMO
    assert admission == {"event": event}
    assert [
        config.image_count for config in client.configs if config.prompt == "b"
    ] == [2, 1]
    env.plugin._check_and_consume_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_v2_quota_denial_creates_no_job_or_record(make_env):
    limiter = RateLimiter(limiter_config(session_max=1))
    client = BatchClient()
    env = await make_env(
        client,
        service_options={"quota": RateLimiterQuota(lambda: limiter)},
        candidates=batch_candidates(),
    )
    event = _Event()
    event.unified_msg_origin = FULL_UMO

    result = await call(env, event, batch_tasks=batch_tasks())

    assert "超过当前会话单周期额度 1" in result
    assert await stored_jobs(env) == []
    assert env.manager._records == {}
    assert client.configs == []
