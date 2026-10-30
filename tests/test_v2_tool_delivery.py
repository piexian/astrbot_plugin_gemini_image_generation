"""LLM 工具后台投递适配器：结果送达、记录收尾、失败通知与记录晚绑定。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

# 复用现有宿主 stub（mcp / astrbot 桩）与事件假对象。
from tests.test_llm_routing_and_tasks import _Event
from tl import llm_tools
from tl import v2_tool_delivery as delivery_module
from tl.background_tasks import BackgroundTaskManager
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.states import JobState
from tl.v2_bridge import v2_failure_message
from tl.v2_tool_delivery import ToolBackgroundDelivery, ToolBatchDelivery

SCENE = "后台任务"
TOTAL_TIMEOUT = 120
RESULT = GenerationResult(
    artifacts=("https://img.test/a.png", "/tmp/b.png"),
    text="ok",
    provider="legacy",
    model="auto",
    candidate_id="cand-1",
    stats={"successful_model_alias": "别名"},
)
# 记录里随任务变化、与收尾字段无关的键。
VOLATILE_KEYS = {"task_id", "created_at", "updated_at", "job_id", "session_id"}


class Notifier:
    """替身 notify_llm_background_result / report_background_failure，记录调用。"""

    def __init__(self, ok=True, error=None, hang=False):
        self.ok = ok
        self.error = error
        self.hang = hang
        self.results: list[tuple] = []
        self.failures: list[tuple] = []

    async def notify(self, plugin, event, task_result, *, notice, scene):
        self.results.append((task_result, notice, scene))
        if self.hang:
            await asyncio.sleep(30)
        if self.error is not None:
            raise self.error
        return self.ok

    async def report(self, plugin, event, failure_summary, *, scene, task_id=None):
        self.failures.append((failure_summary, scene, task_id))
        return self.ok


@pytest.fixture
def notifier(monkeypatch):
    recorder = Notifier()
    # 旧路径与适配器都经 background_dispatch 分发；失败通知另有一处直接引用。
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
def plugin(tmp_path):
    return SimpleNamespace(
        background_task_manager=BackgroundTaskManager(tmp_path / "bg"),
        message_sender=SimpleNamespace(
            merge_available_images=lambda urls, paths: [*(urls or []), *(paths or [])],
            prepare_text_content=lambda text, refs: text or "",
        ),
        api_client=None,
        image_generator=SimpleNamespace(total_timeout=TOTAL_TIMEOUT),
    )


def make_job(state=JobState.DELIVERY_PENDING, **fields):
    return Job(
        job_id="job-v2-1",
        request=GenerationRequest(prompt="draw", source="llm_tool"),
        state=state,
        **fields,
    )


async def new_record(plugin, event):
    record = await plugin.background_task_manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )
    return record["task_id"]


async def read_record(plugin, event, task_id):
    return await plugin.background_task_manager.get(task_id, event.unified_msg_origin)


def stable(record):
    return {key: value for key, value in record.items() if key not in VOLATILE_KEYS}


async def legacy_record(plugin, event, *, success, result_data, stats):
    """旧路径 _await_generation_task_and_send 收尾后的记录，用于逐字对比。"""
    task_id = await new_record(plugin, event)

    async def finished():
        return success, result_data, stats

    await llm_tools._await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(finished()),
        scene=SCENE,
        task_id=task_id,
        notify_llm=True,
    )
    return await read_record(plugin, event, task_id)


# ===== 成功送达 =====


@pytest.mark.asyncio
async def test_deliver_dispatches_result_and_finalizes_record_like_legacy(
    plugin, notifier
):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    await adapter.deliver(make_job(), RESULT)

    (task_result, notice, scene) = notifier.results[0]
    assert scene == SCENE
    assert task_result == {
        "task_id": task_id,
        "status": "succeeded",
        "image_urls": ["https://img.test/a.png"],
        "image_paths": ["/tmp/b.png"],
        "text_content": "ok",
    }
    assert task_id in notice
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "succeeded"
    assert record["message"] == "图片生成已完成并发送"
    assert record["job_id"] == "job-v2-1"
    assert record["items"] == [
        {
            "name": "single",
            "success": True,
            "provider": "legacy",
            "model": "auto",
            "alias": "别名",
            "candidate_id": "cand-1",
            "delivery_success": True,
            "image_urls": ["https://img.test/a.png"],
            "image_paths": ["/tmp/b.png"],
        }
    ]
    assert (record["completed_items"], record["succeeded_items"]) == (1, 1)
    assert record["failed_items"] == 0

    # 与旧路径对同一份结果收尾的记录逐字一致（除任务号、时间与 job_id）。
    legacy = await legacy_record(
        plugin,
        event,
        success=True,
        result_data=(["https://img.test/a.png"], ["/tmp/b.png"], "ok", None),
        stats={
            "successful_provider": "legacy",
            "successful_model": "auto",
            "successful_model_alias": "别名",
            "successful_candidate_id": "cand-1",
        },
    )
    assert stable(record) == stable(legacy)


@pytest.mark.asyncio
async def test_deliver_not_delivered_marks_partial_success_and_raises(plugin, notifier):
    notifier.ok = False
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    with pytest.raises(RuntimeError, match="未送达"):
        await adapter.deliver(make_job(), RESULT)

    record = await read_record(plugin, event, task_id)
    assert record["status"] == "partial_success"
    assert record["message"] == (
        "图片生成已完成，但结果发送未成功；可查询任务获取图片 URL/路径"
    )
    assert record["items"][0]["delivery_success"] is False
    assert record["job_id"] == "job-v2-1"
    legacy = await legacy_record(
        plugin,
        event,
        success=True,
        result_data=(["https://img.test/a.png"], ["/tmp/b.png"], "ok", None),
        stats={
            "successful_provider": "legacy",
            "successful_model": "auto",
            "successful_model_alias": "别名",
            "successful_candidate_id": "cand-1",
        },
    )
    assert stable(record) == stable(legacy)


@pytest.mark.asyncio
async def test_dispatch_exception_counts_as_not_delivered(plugin, notifier):
    notifier.error = RuntimeError("agent exploded")
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    with pytest.raises(RuntimeError, match="未送达"):
        await adapter.deliver(make_job(), RESULT)

    record = await read_record(plugin, event, task_id)
    assert record["status"] == "partial_success"
    assert record["items"][0]["delivery_success"] is False


@pytest.mark.asyncio
async def test_record_persistence_failure_does_not_fail_the_delivery(
    plugin, notifier, monkeypatch
):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    async def broken_save():
        raise OSError("disk full")

    monkeypatch.setattr(plugin.background_task_manager, "_save", broken_save)

    # 结果已送达：记录落盘失败只记日志，不能把投递改判为失败。
    await adapter.deliver(make_job(), RESULT)

    record = await read_record(plugin, event, task_id)
    assert record["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_during_deliver_propagates_and_leaves_record_alone(
    plugin, notifier
):
    notifier.hang = True
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    task = asyncio.create_task(adapter.deliver(make_job(), RESULT))
    for _ in range(100):
        if notifier.results:
            break
        await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await read_record(plugin, event, task_id))["status"] == "running"


# ===== 失败与中断 =====


@pytest.mark.asyncio
async def test_deliver_failure_reports_failed_job_and_finalizes_record_like_legacy(
    plugin, notifier
):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)
    job = make_job(
        JobState.FAILED, error_code="provider_http_429", error_message="限流"
    )
    expected = v2_failure_message(job, total_timeout=TOTAL_TIMEOUT)

    await adapter.deliver_failure(job)

    assert notifier.failures == [(expected, SCENE, task_id)]
    assert notifier.results == []
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "failed"
    assert record["message"] == expected
    assert record["job_id"] == "job-v2-1"
    assert (record["completed_items"], record["failed_items"]) == (1, 1)
    assert record["items"][0]["success"] is False
    legacy = await legacy_record(
        plugin, event, success=False, result_data=expected, stats={}
    )
    assert stable(record) == stable(legacy)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "message"),
    [
        (JobState.CANCELLED, "图片生成已取消"),
        (JobState.INTERRUPTED, "图片生成已中断"),
        (JobState.ORPHANED, "图片生成已中断"),
    ],
)
async def test_deliver_failure_for_cancelled_or_interrupted_only_updates_record(
    plugin, notifier, state, message
):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(task_id)

    await adapter.deliver_failure(make_job(state))

    # 取消、关闭、重启不是生成失败：不打扰用户，也不唤醒 Agent。
    assert notifier.failures == [] and notifier.results == []
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "interrupted"
    assert record["message"] == message
    assert record["job_id"] == "job-v2-1"


# ===== 记录晚绑定 =====


@pytest.mark.asyncio
async def test_deliver_waits_for_late_record_binding(plugin, notifier):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)

    task = asyncio.create_task(adapter.deliver(make_job(), RESULT))
    await asyncio.sleep(0.05)
    # 记录尚未绑定：不能碰记录，也不能带着空任务号先发出去。
    assert not task.done()
    assert notifier.results == []

    adapter.bind_record(task_id)
    await asyncio.wait_for(task, 2)

    assert notifier.results[0][0]["task_id"] == task_id
    assert (await read_record(plugin, event, task_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_deliver_failure_waits_for_late_record_binding(plugin, notifier):
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)

    task = asyncio.create_task(adapter.deliver_failure(make_job(JobState.CANCELLED)))
    await asyncio.sleep(0.05)
    assert not task.done()
    assert (await read_record(plugin, event, task_id))["status"] == "running"

    adapter.bind_record(task_id)
    await asyncio.wait_for(task, 2)

    assert (await read_record(plugin, event, task_id))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_binding_timeout_continues_without_task_id(plugin, notifier, monkeypatch):
    monkeypatch.setattr(delivery_module, "_RECORD_BIND_TIMEOUT_SECONDS", 0.05)
    event = _Event()
    task_id = await new_record(plugin, event)
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)

    await asyncio.wait_for(adapter.deliver(make_job(), RESULT), 2)

    # 超时后不带任务号继续送达，已有记录不被误改。
    assert notifier.results[0][0]["task_id"] == ""
    assert (await read_record(plugin, event, task_id))["status"] == "running"


@pytest.mark.asyncio
async def test_bind_none_releases_waiting_delivery_immediately(plugin, notifier):
    event = _Event()
    adapter = ToolBackgroundDelivery(plugin, event, scene=SCENE)
    adapter.bind_record(None)

    # 默认的 30 秒等待不应触发：记录没建成时工具会绑定 None。
    await asyncio.wait_for(adapter.deliver(make_job(), RESULT), 2)

    assert notifier.results[0][0]["task_id"] == ""


# ===== 批量进度与汇总 =====


def batch_child(job_id, name, count=1, state=JobState.RUNNING, **fields):
    return Job(
        job_id=job_id,
        request=GenerationRequest(
            prompt=name,
            source="llm_batch",
            image_count=count,
            metadata={"item_name": name},
        ),
        state=state,
        **fields,
    )


async def new_batch_record(plugin, event, total=2):
    record = await plugin.background_task_manager.create(
        session_id=event.unified_msg_origin,
        kind="batch",
        routing_mode="full_polling",
        message="running",
        total_items=total,
    )
    return record["task_id"]


@pytest.mark.asyncio
async def test_batch_delivery_tracks_children_and_sends_legacy_summary(
    plugin, notifier
):
    event = _Event()
    task_id = await new_batch_record(plugin, event)
    adapter = ToolBatchDelivery(plugin, event)
    adapter.bind_record(task_id)
    parent = make_job()
    first = batch_child("child-1", "第一张", 2)
    second = batch_child(
        "child-2", "第二张", state=JobState.FAILED, error_message="上游失败"
    )

    await adapter.child_started(parent, first)
    assert (await read_record(plugin, event, task_id))["current_item"] == "第一张"
    await adapter.child_finished(parent, first, RESULT)
    await adapter.child_finished(parent, second, None)
    await adapter.deliver(
        parent, GenerationResult(artifacts=RESULT.artifacts, partial=True)
    )

    payload, notice, scene = notifier.results[0]
    assert payload["task_id"] == task_id
    assert payload["result"] == notice
    assert "第一张: 2/2 张" in notice
    assert "第二张: 失败或未完成，0/1 张，上游失败" in notice
    assert scene == f"批量任务/{task_id}"
    assert [item["name"] for item in payload["items"]] == ["第一张", "第二张"]
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "partial_success"
    assert record["completed_items"] == 2
    assert record["succeeded_items"] == 1
    assert record["failed_items"] == 1
    assert record["current_item"] is None
    assert record["items"][0]["provider"] == "legacy"
    assert record["items"][0]["alias"] == "别名"
    assert record["items"][1]["error"] == "上游失败"


@pytest.mark.asyncio
async def test_batch_delivery_not_delivered_marks_partial_and_raises(plugin, notifier):
    notifier.ok = False
    event = _Event()
    task_id = await new_batch_record(plugin, event, total=1)
    adapter = ToolBatchDelivery(plugin, event)
    adapter.bind_record(task_id)
    parent = make_job()
    child = batch_child("child-1", "唯一", 2)
    await adapter.child_finished(parent, child, RESULT)

    with pytest.raises(RuntimeError, match="未送达"):
        await adapter.deliver(parent, GenerationResult(artifacts=RESULT.artifacts))

    record = await read_record(plugin, event, task_id)
    assert record["status"] == "partial_success"
    assert record["succeeded_items"] == 1
    assert record["message"] == (
        "批量生成已完成，但结果发送未成功；可查询任务获取图片 URL/路径"
    )


@pytest.mark.asyncio
async def test_batch_delivery_failure_reports_summary_and_marks_failed(
    plugin, notifier
):
    event = _Event()
    task_id = await new_batch_record(plugin, event, total=1)
    adapter = ToolBatchDelivery(plugin, event)
    adapter.bind_record(task_id)
    parent = make_job(JobState.FAILED, error_code="batch_all_failed")
    child = batch_child(
        "child-1", "失败项", state=JobState.FAILED, error_message="没有图片"
    )
    await adapter.child_finished(parent, child, None)

    await adapter.deliver_failure(parent)

    assert notifier.results == []
    summary, scene, notified_task = notifier.failures[0]
    assert "失败项: 失败或未完成，0/1 张，没有图片" in summary
    assert scene == f"批量任务/{task_id}" and notified_task == task_id
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "failed"
    assert record["failed_items"] == 1


@pytest.mark.asyncio
async def test_batch_parent_failure_with_output_still_reports_failure(plugin, notifier):
    event = _Event()
    task_id = await new_batch_record(plugin, event, total=1)
    adapter = ToolBatchDelivery(plugin, event)
    adapter.bind_record(task_id)
    parent = make_job(JobState.FAILED, error_code="store_failed")
    await adapter.child_finished(parent, batch_child("child-1", "已产出", 2), RESULT)

    await adapter.deliver_failure(parent)

    assert notifier.results == []
    assert len(notifier.failures) == 1
    assert (await read_record(plugin, event, task_id))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [JobState.CANCELLED, JobState.INTERRUPTED])
async def test_batch_delivery_interruption_does_not_notify(plugin, notifier, state):
    event = _Event()
    task_id = await new_batch_record(plugin, event, total=1)
    adapter = ToolBatchDelivery(plugin, event)
    adapter.bind_record(task_id)

    await adapter.deliver_failure(make_job(state))

    assert notifier.results == [] and notifier.failures == []
    record = await read_record(plugin, event, task_id)
    assert record["status"] == "interrupted"
    assert record["message"] == "批量生成已中断"
