"""v2 会话灰度、命令入口接入与 legacy/v2 请求形态对比测试。"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

# 复用现有宿主 stub 与命令插件装配。
from tests.test_limit_admission import (
    _command_class,
    _config,
    _Event,
    _Limiter,
    _run_quick,
)
from tl.core.errors import CoreError
from tl.core.results import GenerationResult
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_errors import api_failure_message
from tl.image_generator import ImageGenerator
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR
from tl.v2_bridge import v2_failure_text

# ===== 会话灰度范围 =====


def test_v2_service_for_gate(command_plugin):
    plugin = command_plugin
    sentinel = object()
    plugin._v2_service = sentinel
    plugin.cfg.scheduler_v2 = False
    plugin.cfg.scheduler_v2_sessions = ()
    # 总开关关 → 一律旧路径。
    assert plugin._v2_service_for("umo-a") is None
    # 总开关开、名单空 → 全部入口走 v2。
    plugin.cfg.scheduler_v2 = True
    assert plugin._v2_service_for("umo-a") is sentinel
    assert plugin._v2_service_for(None) is sentinel
    # 名单非空且命中 → v2；未命中 → 旧路径。
    plugin.cfg.scheduler_v2_sessions = ("umo-a",)
    assert plugin._v2_service_for("umo-a") is sentinel
    assert plugin._v2_service_for("umo-b") is None


# ===== 命令入口 v2 行为 =====


class _FakeV2Service:
    # 镜像 JobScheduler.result_wait_timeout 的默认公式（observer_timeout=120）。
    OBSERVER_TIMEOUT = 120.0

    def __init__(
        self,
        *,
        state=JobState.SUCCEEDED,
        result=None,
        wait_error=None,
        prepare_error=None,
        submit_error=None,
    ):
        self.state = state
        self.result = result
        self.wait_error = wait_error
        self.prepare_error = prepare_error
        self.submit_error = submit_error
        self.calls: list[tuple] = []

    def result_wait_timeout(self, deadline_seconds):
        return deadline_seconds + 1.0 + 2 * self.OBSERVER_TIMEOUT + 10

    async def prepare_references(self, references, *, requester):
        if self.prepare_error is not None:
            raise self.prepare_error
        self.calls.append(("prepare", tuple(references)))
        return tuple(references)

    async def submit(self, request):
        if self.submit_error is not None:
            raise self.submit_error
        self.calls.append(("submit", request.prompt))
        return SimpleNamespace(job_id="job-v2-1")

    async def wait(self, job_id, timeout=None):
        self.calls.append(("wait", job_id, timeout))
        if self.wait_error is not None:
            raise self.wait_error
        return SimpleNamespace(state=self.state, result=self.result)

    async def cancel(self, job_id):
        self.calls.append(("cancel", job_id))


def _wire_sender(plugin):
    sent = []

    async def send_results(**kwargs):
        sent.append(kwargs)

    async def duration(*args, **kwargs):
        if False:
            yield None

    async def archive_images(urls, paths, **kwargs):
        return []

    plugin.message_sender = SimpleNamespace(
        send_results_with_stream_retry=send_results, send_api_duration=duration
    )
    plugin.web_studio_service = SimpleNamespace(archive_images=archive_images)
    plugin.generation_tracker = SimpleNamespace(
        begin=AsyncMock(return_value={"job_id": "job-q"}),
        complete=AsyncMock(),
        update=AsyncMock(),
        fail=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_quick_generate_v2_skips_entry_tracker_writes(command_plugin):
    plugin = command_plugin
    plugin.cfg.scheduler_v2 = True
    service = _FakeV2Service(
        result=GenerationResult(
            artifacts=("https://img.test/a.png",),
            text="ok",
            provider="legacy",
            model="auto",
        )
    )
    plugin._v2_service_for = lambda umo: service
    sent = _wire_sender(plugin)

    outputs = await _run_quick(plugin)

    assert "🎨 使用手办化模式生成图像..." in outputs
    assert sent and sent[0]["image_urls"] == ["https://img.test/a.png"]
    # 投影负责历史：入口不再 begin/complete/fail/update。
    plugin.generation_tracker.begin.assert_not_awaited()
    plugin.generation_tracker.complete.assert_not_awaited()
    plugin.generation_tracker.fail.assert_not_awaited()
    plugin.generation_tracker.update.assert_not_awaited()
    # 旧客户端不再被入口直接调用。
    plugin.api_client.generate_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_generate_v2_failure_message_matches_legacy_wording(
    command_plugin,
):
    plugin = command_plugin
    plugin.cfg.scheduler_v2 = True
    service = _FakeV2Service(state=JobState.FAILED)
    service.result = None

    # wait 返回的 JobResult 形态带 error_code。
    async def wait(job_id, timeout=None):
        return SimpleNamespace(
            state=JobState.FAILED,
            result=None,
            error_code="provider_http_429",
            error_message="限流",
        )

    service.wait = wait
    plugin._v2_service_for = lambda umo: service
    _wire_sender(plugin)

    outputs = await _run_quick(plugin)

    expected = api_failure_message(
        message="限流", status_code=429, error_type=None, total_timeout=120
    )
    assert expected in outputs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"), [("quick_sticker", ("draw",)), ("change_style", ("ink",))]
)
async def test_sticker_and_style_pass_v2_service(command_plugin, name, args):
    plugin = command_plugin
    plugin.cfg.enable_sticker_split = True
    sentinel = object()
    plugin._v2_service_for = lambda umo: sentinel
    event = _Event()
    event.message_str = ""
    _ = [result async for result in getattr(plugin, name)(event, *args)]
    kwargs = plugin.image_generator.generate_image_core.await_args.kwargs
    assert kwargs["v2_service"] is sentinel


# ===== ImageGenerator v2 分支 =====


def _make_generator(api_client=None):
    return ImageGenerator(
        context=None,
        api_client=api_client if api_client is not None else object(),
        filter_valid_fn=lambda images, source: images or [],
    )


@pytest.mark.asyncio
async def test_image_generator_v2_success_returns_split_artifacts():
    result = GenerationResult(
        artifacts=("https://img.test/a.png", "/tmp/b.png"),
        text="ok",
        provider="legacy",
        model="auto",
        candidate_id="cand-1",
        stats={"retry_count": 2, "successful_model_alias": "别名"},
    )
    service = _FakeV2Service(result=result)
    generator = _make_generator()

    success, data = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=["data:image/png;base64,xx"],
        avatar_reference=[],
        v2_service=service,
    )

    assert success is True
    urls, paths, text, thought = data
    assert urls == ["https://img.test/a.png"]
    assert paths == ["/tmp/b.png"]
    assert text == "ok"
    assert thought is None
    stats = generator.get_request_stats()
    assert stats["successful_provider"] == "legacy"
    assert stats["successful_model"] == "auto"
    assert stats["successful_candidate_id"] == "cand-1"
    assert stats["retry_count"] == 2
    assert stats["successful_model_alias"] == "别名"
    # 等待上限取调度器的 result_wait_timeout（含观察者钩子时间）。
    submit_timeout = [call[2] for call in service.calls if call[0] == "wait"][0]
    assert submit_timeout == service.result_wait_timeout(generator.total_timeout)


@pytest.mark.asyncio
async def test_image_generator_v2_failure_uses_shared_wording():
    async def wait(job_id, timeout=None):
        return SimpleNamespace(
            state=JobState.FAILED,
            result=None,
            error_code="provider_network",
            error_message="网络异常",
        )

    service = _FakeV2Service()
    service.wait = wait
    generator = _make_generator()

    success, message = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        v2_service=service,
    )

    assert success is False
    assert message == api_failure_message(
        message="网络异常", status_code=None, error_type="network", total_timeout=120
    )


@pytest.mark.asyncio
async def test_image_generator_v2_cancel_shields_job_cancel():
    service = _FakeV2Service()
    service.wait_error = asyncio.CancelledError()
    generator = _make_generator()

    with pytest.raises(asyncio.CancelledError):
        await generator.generate_image_core(
            event=SimpleNamespace(unified_msg_origin="u"),
            prompt="draw",
            reference_images=[],
            avatar_reference=[],
            v2_service=service,
        )

    # 调用方取消时先取消 Job 再抛出。
    assert ("cancel", "job-v2-1") in service.calls


@pytest.mark.asyncio
async def test_image_generator_without_v2_service_keeps_legacy_path():
    calls = []

    class Client:
        async def generate_image(self, config, **kwargs):
            calls.append(config)
            return [], ["/tmp/out.png"], None, None

    generator = ImageGenerator(
        context=None, api_client=Client(), filter_valid_fn=lambda i, s: i or []
    )
    success, _ = await generator.generate_image_core(
        event=None, prompt="draw", reference_images=[], avatar_reference=[]
    )
    assert success is True
    assert len(calls) == 1


# ===== legacy 与 v2 请求形态对比 =====


def _png_data_uri() -> str:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (9, 8, 7)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class _ConfigRecordingClient:
    def __init__(self):
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        return (["https://img.test/out.png"], [], "ok", None)


_SHAPE_FIELDS = (
    "prompt",
    "resolution",
    "aspect_ratio",
    "suppress_resolution",
    "requested_provider",
    "requested_model",
    "enable_smart_retry",
    "image_input_mode",
    "image_count",
)


def _assert_same_shape(legacy_config, v2_config):
    for field in _SHAPE_FIELDS:
        assert getattr(legacy_config, field) == getattr(v2_config, field), field
    # 参考图：旧路径为 base64 data URI，v2 为落盘文件路径，字节必须一致。
    legacy_ref = legacy_config.reference_images[0]
    v2_ref = v2_config.reference_images[0]
    assert legacy_ref.startswith("data:image/")
    assert not v2_ref.startswith("data:")
    assert Path(v2_ref).is_file()
    assert base64.b64decode(legacy_ref.split(",", 1)[1]) == Path(v2_ref).read_bytes()


def _make_plugin():
    """与 test_limit_admission.command_plugin 同款装配的独立实例。"""

    plugin = _command_class()()
    plugin.cfg = _config()
    plugin.cfg.preserve_reference_image_size = False
    plugin.cfg.enable_smart_retry = True
    plugin.cfg.enable_sticker_split = False
    plugin.cfg.sticker_grid_rows = plugin.cfg.sticker_grid_cols = 4
    plugin.cfg.quick_mode_overrides = {}
    plugin.rate_limiter = _Limiter()
    plugin.api_client = SimpleNamespace(
        generate_image=AsyncMock(side_effect=RuntimeError("provider failed"))
    )
    plugin._ensure_api_client = lambda: plugin.api_client is not None
    plugin.avatar_handler = SimpleNamespace(
        should_use_avatar=AsyncMock(return_value=False),
        should_use_avatar_for_prompt=AsyncMock(return_value=False),
    )
    plugin.image_handler = SimpleNamespace(
        fetch_images_from_event=AsyncMock(return_value=(["ref.png"], [])),
        filter_valid_reference_images=lambda values, **kwargs: list(values),
    )
    plugin.image_generator = SimpleNamespace(
        generate_image_core=AsyncMock(return_value=(False, "provider failed")),
        get_request_stats=lambda: {},
    )
    plugin.generation_tracker = SimpleNamespace(
        begin=AsyncMock(return_value={"job_id": "job-q"}),
        complete=AsyncMock(),
        update=AsyncMock(),
        fail=AsyncMock(),
    )

    async def duration(*args, **kwargs):
        if False:
            yield None

    plugin.message_sender = SimpleNamespace(send_api_duration=duration)
    return plugin


@pytest.fixture
def command_plugin():
    return _make_plugin()


@pytest.mark.asyncio
async def test_quick_command_request_shape_legacy_vs_v2(tmp_path, command_plugin):
    data_uri = _png_data_uri()

    # 旧路径：命令入口直连 legacy client。
    plugin = command_plugin
    legacy_client = _ConfigRecordingClient()
    plugin.api_client = SimpleNamespace(generate_image=legacy_client.generate_image)
    plugin.image_handler.fetch_images_from_event = AsyncMock(
        return_value=([data_uri], [])
    )
    plugin.image_handler.filter_valid_reference_images = lambda values, **kwargs: list(
        values
    )
    _wire_sender(plugin)
    await _run_quick(plugin)
    assert len(legacy_client.configs) == 1

    # v2 路径：真实 build_generation_stack + 记录入参的假客户端。
    v2_client = _ConfigRecordingClient()
    service = build_generation_stack(
        v2_client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (REFERENCE_IMAGE_CACHE_DIR,),
    )
    plugin2 = _make_plugin()
    plugin2.cfg.scheduler_v2 = True
    plugin2._v2_service_for = lambda umo: service
    plugin2.api_client = SimpleNamespace(generate_image=v2_client.generate_image)
    plugin2._ensure_api_client = lambda: True
    plugin2.image_handler = SimpleNamespace(
        fetch_images_from_event=AsyncMock(return_value=([data_uri], [])),
        filter_valid_reference_images=lambda values, **kwargs: list(values),
    )
    _wire_sender(plugin2)
    try:
        await _run_quick(plugin2)
        assert len(v2_client.configs) == 1
        _assert_same_shape(legacy_client.configs[0], v2_client.configs[0])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_sticker_generate_core_request_shape_legacy_vs_v2(tmp_path):
    data_uri = _png_data_uri()

    legacy_client = _ConfigRecordingClient()
    generator_legacy = ImageGenerator(
        context=None,
        api_client=legacy_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    success, _ = await generator_legacy.generate_image_core(
        event=None,
        prompt="表情包网格",
        reference_images=[data_uri],
        avatar_reference=[],
    )
    assert success is True
    assert len(legacy_client.configs) == 1

    v2_client = _ConfigRecordingClient()
    service = build_generation_stack(
        v2_client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (REFERENCE_IMAGE_CACHE_DIR,),
    )
    generator_v2 = ImageGenerator(
        context=None,
        api_client=v2_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    try:
        success2, data2 = await generator_v2.generate_image_core(
            event=None,
            prompt="表情包网格",
            reference_images=[data_uri],
            avatar_reference=[],
            v2_service=service,
        )
        assert success2 is True
        assert data2[0] == ["https://img.test/out.png"]
        _assert_same_shape(legacy_client.configs[0], v2_client.configs[0])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quick_generate_v2_cancel_shields_job_cancel(command_plugin):
    plugin = command_plugin
    plugin.cfg.scheduler_v2 = True
    service = _FakeV2Service()
    service.wait_error = asyncio.CancelledError()
    plugin._v2_service_for = lambda umo: service
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""

    with pytest.raises(asyncio.CancelledError):
        async for _ in plugin._quick_generate_image(event, "draw"):
            pass

    # 等待期被取消：先取消 Job 再抛出；tracker 由投影负责，入口不写。
    assert ("cancel", "job-v2-1") in service.calls
    plugin.generation_tracker.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_generator_v2_prepare_core_error_returns_shared_wording():
    """prepare_references 抛 CoreError：不外抛，按错误码给旧路径同措辞提示。"""

    service = _FakeV2Service(
        prepare_error=CoreError(
            "参考图过大", code="reference_too_large", retryable=False
        )
    )
    generator = _make_generator()

    success, message = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=["data:image/png;base64,xx"],
        avatar_reference=[],
        v2_service=service,
    )

    assert success is False
    assert message == v2_failure_text(
        "reference_too_large", "参考图过大", total_timeout=120
    )
    # 提交未发生，也没有可取消的 Job。
    assert ("cancel", "job-v2-1") not in service.calls


@pytest.mark.asyncio
async def test_image_generator_v2_wait_timeout_cancels_job_and_times_out():
    """wait 超时：先取消 Job，再返回旧路径同款超时提示。"""

    service = _FakeV2Service(wait_error=asyncio.TimeoutError())
    generator = _make_generator()

    success, message = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        v2_service=service,
    )

    assert success is False
    assert message == api_failure_message(
        message="生成等待超时",
        status_code=None,
        error_type="timeout",
        total_timeout=120,
    )
    assert ("cancel", "job-v2-1") in service.calls


@pytest.mark.asyncio
async def test_image_generator_v2_wait_unknown_error_cancels_job():
    """wait 未知异常：先取消 Job，再按旧路径措辞返回，不外抛。"""

    service = _FakeV2Service(wait_error=ValueError("boom"))
    generator = _make_generator()

    success, message = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        v2_service=service,
    )

    assert success is False
    assert message == "❌ 生成图像时发生错误: boom"
    assert ("cancel", "job-v2-1") in service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "expected_mode"),
    [
        ("gemini", "gemini-2.5-flash-image", "provider_model_retry"),
        ("gemini", None, "provider_retry"),
        (None, "gpt-image-2", "model_polling"),
        (None, None, "full_polling"),
    ],
)
async def test_image_generator_v2_routing_mode_follows_request(
    provider, model, expected_mode
):
    """routing_mode 与旧客户端同函数计算，不写死 full_polling。"""

    service = _FakeV2Service(
        result=GenerationResult(artifacts=("https://img.test/a.png",), text="ok")
    )
    generator = _make_generator()

    success, _ = await generator.generate_image_core(
        event=SimpleNamespace(unified_msg_origin="u"),
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        requested_provider=provider,
        requested_model=model,
        v2_service=service,
    )

    assert success is True
    assert generator.get_request_stats()["routing_mode"] == expected_mode


@pytest.mark.asyncio
async def test_quick_generate_v2_wait_timeout_yields_message_and_cancels(
    command_plugin,
):
    plugin = command_plugin
    plugin.cfg.scheduler_v2 = True
    service = _FakeV2Service(wait_error=asyncio.TimeoutError())
    plugin._v2_service_for = lambda umo: service
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""

    outputs = await _run_quick(plugin)

    expected = api_failure_message(
        message="生成等待超时",
        status_code=None,
        error_type="timeout",
        total_timeout=120,
    )
    assert expected in outputs
    assert ("cancel", "job-v2-1") in service.calls
    # 超时后直接结束，不再发送结果。
    plugin.generation_tracker.complete.assert_not_awaited()
