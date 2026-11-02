"""查询和共享工具只读返回，不进入生成路径。"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

for module_name in (
    "astrbot.core",
    "astrbot.core.agent",
    "astrbot.core.agent.run_context",
    "astrbot.core.agent.tool",
    "astrbot.core.astr_agent_context",
):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))


class _FunctionTool:
    @classmethod
    def __class_getitem__(cls, item):
        return cls


sys.modules["astrbot.core.agent.run_context"].ContextWrapper = type(
    "ContextWrapper", (), {}
)
sys.modules["astrbot.core.agent.tool"].FunctionTool = _FunctionTool
sys.modules["astrbot.core.agent.tool"].ToolExecResult = type("ToolExecResult", (), {})
sys.modules["astrbot.core.astr_agent_context"].AstrAgentContext = type(
    "AstrAgentContext", (), {}
)
from tl.background_tasks import BackgroundTaskManager  # noqa: E402
from tl.generation_tracker import GenerationTracker  # noqa: E402
from tl.internal_references import ReferenceResolver, parse_references  # noqa: E402
from tl.llm_query_tools import GeminiImageLookupTool, GeminiImageShareTool  # noqa: E402
from tl.plugin_config import ProviderCandidate  # noqa: E402
from tl.prompt_templates import PromptTemplateStore  # noqa: E402


def _event(umo="session:a", user="100", sender=True):
    payload = {
        "unified_msg_origin": umo,
        "get_sender_name": lambda: "用户",
        "get_group_id": lambda: "",
        "get_message_type": lambda: SimpleNamespace(value="FriendMessage"),
    }
    if sender:
        payload["get_sender_id"] = lambda: user
    return SimpleNamespace(**payload)


def _context(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), (0, 0, 0)).save(path)


async def _record(
    tracker,
    *,
    umo,
    user="100",
    prompt="一只猫",
    status="succeeded",
    images=("a.png",),
    template_id=None,
):
    names = []
    for name in images:
        _png(tracker.gallery_dir / name)
        names.append(name)
    record = await tracker.begin(
        source="command",
        prompt=prompt,
        params={"template_id": template_id},
        requester={
            "user_id": user,
            "user_name": "用户",
            "umo": umo,
            "chat_type": "private",
        },
    )
    if status == "succeeded":
        await tracker.complete(
            record["job_id"], image_files=names, text_content=None, stats={}
        )
    elif status == "partial_success":
        await tracker.complete(
            record["job_id"],
            image_files=names,
            text_content=None,
            stats={},
            status="partial_success",
        )
    else:
        await tracker.fail(record["job_id"], error="失败")
    return record["job_id"]


def _plugin(**values):
    return SimpleNamespace(
        cfg=SimpleNamespace(
            provider_candidates=values.get("candidates", []),
            batch_max_images_per_task=4,
            reference_cross_session=False,
            llm_tool_reference_path_mode="whitelist",
        ),
        prompt_templates=values.get("templates"),
        generation_tracker=values.get("tracker"),
        background_task_manager=values.get("tasks"),
    )


def test_lookup_and_share_schemas_do_not_promise_generation_inputs():
    lookup = GeminiImageLookupTool(plugin=None)
    share = GeminiImageShareTool(plugin=None)
    assert lookup.parameters["required"] == ["kind"]
    assert lookup.parameters["properties"]["kind"]["enum"] == [
        "models",
        "templates",
        "history",
        "tasks",
    ]
    assert set(lookup.parameters["properties"]) == {"kind", "query", "limit", "detail"}
    assert share.parameters["required"] == ["references", "shared"]
    assert "template 参数" not in lookup.description
    assert "references" not in lookup.description
    assert "明确" in share.description and "跨会话" in share.description


@pytest.mark.asyncio
async def test_models_filter_limit_and_detail():
    plugin = _plugin(
        candidates=[
            ProviderCandidate(
                "google#1",
                "google",
                {"model": "gemini-fast", "api_keys": ["k"]},
                model_alias="fast",
            ),
            ProviderCandidate(
                "google#2", "google", {"model": "gemini-pro", "api_keys": ["k"]}
            ),
        ]
    )
    tool = GeminiImageLookupTool(plugin=plugin)
    listed = json.loads(
        await tool.call(_context(_event()), kind="models", query="FAST")
    )
    assert listed["total"] == 1 and listed["models"][0]["alias"] == "fast"
    assert "parameters" not in listed["models"][0]
    detailed = json.loads(
        await tool.call(
            _context(_event()), kind="models", query="google", detail=True, limit=1
        )
    )
    assert detailed["truncated"] is True and detailed["total"] >= 1
    assert "parameters" in detailed["models"][0]
    missing = json.loads(
        await tool.call(_context(_event()), kind="models", query="不存在")
    )
    assert missing["models"] == [] and "没有匹配" in missing["message"]
    defaulted = json.loads(
        await tool.call(_context(_event()), kind="models", limit="bad")
    )
    assert defaulted["total"] == 2 and defaulted["truncated"] is False
    invalid = json.loads(await tool.call(_context(_event())))
    assert invalid["error"] == "kind 必须是 models、templates、history、tasks 之一"


@pytest.mark.asyncio
async def test_templates_rank_preview_and_hide_disabled(tmp_path):
    store = PromptTemplateStore(tmp_path)
    named = await store.create(
        {"name": "夜色海报", "content": "第一行\n第二行", "tags": ["风景"]}
    )
    tagged = await store.create(
        {
            "name": "普通卡片",
            "content": "正文",
            "tags": ["夜色"],
            "ai_description": "其他",
        }
    )
    described = await store.create(
        {"name": "说明卡片", "content": "正文", "ai_description": "适合夜色"}
    )
    hidden = await store.create({"name": "夜色隐藏", "content": "不要出现"})
    await store.set_ai_enabled(hidden.id, False)
    tool = GeminiImageLookupTool(plugin=_plugin(templates=store))
    listed = json.loads(
        await tool.call(_context(_event()), kind="templates", query="夜色")
    )
    assert [item["id"] for item in listed["templates"][:3]] == [
        named.id,
        tagged.id,
        described.id,
    ]
    assert hidden.id not in [item["id"] for item in listed["templates"]]
    assert "content" not in listed["templates"][0]
    detailed = json.loads(
        await tool.call(
            _context(_event()), kind="templates", query="夜色海报", detail=True
        )
    )
    assert detailed["templates"][0]["preview"] == "第一行 第二行"
    both = json.loads(
        await tool.call(_context(_event()), kind="templates", query="夜色 海报")
    )
    assert [item["id"] for item in both["templates"]] == [named.id]


@pytest.mark.asyncio
async def test_history_scope_summary_and_errors(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    own = await _record(
        tracker,
        umo="session:a",
        prompt="第一行\n一只很长的猫" + "猫" * 80,
        images=("one.png", "two.png"),
        template_id="poster",
    )
    await _record(
        tracker, umo="session:a", user="200", prompt="别人的猫", images=("other.png",)
    )
    shared = await _record(
        tracker, umo="session:b", prompt="我的共享猫", images=("shared.png",)
    )
    await tracker.set_shared(shared, True, user_id="100", umo="session:b")
    foreign = await _record(
        tracker,
        umo="session:c",
        user="300",
        prompt="别人共享猫",
        images=("foreign.png",),
    )
    await tracker.set_shared(foreign, True, user_id="300", umo="session:c")
    old = await _record(
        tracker, umo="session:a", prompt="旧记录猫", images=("old.png",)
    )
    tracker._jobs[old]["requester"].pop("umo")
    await _record(tracker, umo="session:a", prompt="失败猫", status="failed", images=())
    partial = await _record(
        tracker,
        umo="session:a",
        prompt="部分猫",
        status="partial_success",
        images=("part.png",),
    )
    tool = GeminiImageLookupTool(plugin=_plugin(tracker=tracker))
    listed = json.loads(await tool.call(_context(_event()), kind="history", limit=10))
    refs = [item["ref"] for item in listed["history"]]
    assert f"gen:{own}" in refs and f"gen:{shared}" in refs and f"gen:{partial}" in refs
    assert f"gen:{foreign}" not in refs and f"gen:{old}" not in refs
    own_item = next(item for item in listed["history"] if item["ref"] == f"gen:{own}")
    assert own_item["images"] == 2 and own_item["template_id"] == "poster"
    assert "\n" not in own_item["prompt"] and len(own_item["prompt"]) <= 60
    assert own_item["shared"] is False
    other_item = next(
        item for item in listed["history"] if "别人的猫" in item["prompt"]
    )
    assert "shared" not in other_item
    assert listed["hint"].startswith("多张图片时")
    filtered = json.loads(
        await tool.call(_context(_event()), kind="history", query="共享猫", limit=1)
    )
    assert (
        filtered["history"][0]["ref"] == f"gen:{shared}"
        and filtered["truncated"] is False
    )
    assert "umo" not in json.dumps(listed)
    tracker.enabled = False
    disabled = json.loads(await tool.call(_context(_event()), kind="history"))
    assert disabled == {"error": "生成历史未开启"}
    await tracker.close()


@pytest.mark.asyncio
async def test_tasks_are_session_scoped_and_clamped(tmp_path):
    manager = BackgroundTaskManager(tmp_path, retention_hours=24)
    first = await manager.create(
        session_id="session:a", kind="single", routing_mode="auto", message="一"
    )
    await manager.create(
        session_id="session:a", kind="single", routing_mode="auto", message="二"
    )
    foreign = await manager.create(
        session_id="session:b", kind="single", routing_mode="auto", message="别的"
    )
    tool = GeminiImageLookupTool(plugin=_plugin(tasks=manager))
    listed = json.loads(await tool.call(_context(_event()), kind="tasks", limit=1))
    assert (
        len(listed["tasks"]) == 1
        and listed["total"] == 2
        and listed["truncated"] is True
    )
    assert "session_id" not in json.dumps(listed)
    owned = json.loads(
        await tool.call(_context(_event()), kind="tasks", query=first["task_id"])
    )
    assert owned["task_id"] == first["task_id"] and "session_id" not in owned
    denied = json.loads(
        await tool.call(_context(_event()), kind="tasks", query=foreign["task_id"])
    )
    assert denied == {"error": "任务不存在或不属于当前会话"}
    missing = json.loads(
        await tool.call(_context(_event()), kind="tasks", query="img-missing")
    )
    assert missing == {"error": "任务不存在或已过期"}
    await manager.close()


@pytest.mark.asyncio
async def test_share_is_owner_only_and_history_follows_it(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    job_id = await _record(tracker, umo="session:a", images=("cat.png", "dog.png"))
    other = await _record(tracker, umo="session:a", user="200", images=("other.png",))
    plugin = _plugin(tracker=tracker)
    tool = GeminiImageShareTool(plugin=plugin)
    shared = json.loads(
        await tool.call(
            _context(_event()),
            references=[f"gen:{job_id}", f"gen:{job_id}#2", "current"],
            shared=True,
        )
    )
    assert (
        shared["results"][0]["ref"] == f"gen:{job_id}"
        and shared["results"][0]["shared"] is True
    )
    assert (
        len([item for item in shared["results"] if item["ref"] == f"gen:{job_id}"]) == 1
    )
    assert shared["results"][1]["error"] == "只接受 gen: 引用"
    denied = json.loads(
        await tool.call(
            _context(_event(user="200")), references=[f"gen:{job_id}"], shared=False
        )
    )
    missing = json.loads(
        await tool.call(_context(_event()), references=["gen:missing"], shared=True)
    )
    hidden = "找不到可共享的生成记录，或者这不是你生成的图"
    assert denied["results"][0]["error"] == missing["results"][0]["error"] == hidden
    assert tracker.get(job_id)["shared"] is True
    latest = json.loads(
        await tool.call(
            _context(_event(user="200")), references=["gen:last"], shared=True
        )
    )
    assert latest["results"][0]["ref"] == f"gen:{other}"
    cleared = json.loads(
        await tool.call(_context(_event()), references=[f"gen:{job_id}"], shared=False)
    )
    assert (
        cleared["results"][0]["shared"] is False
        and cleared["results"][0]["shared_at"] is None
    )
    await tool.call(_context(_event()), references=[f"gen:{job_id}"], shared=True)
    lookup = GeminiImageLookupTool(plugin=plugin)
    owner_other_session = json.loads(
        await lookup.call(_context(_event(umo="session:b")), kind="history", limit=10)
    )
    assert f"gen:{job_id}" in [item["ref"] for item in owner_other_session["history"]]
    stranger = json.loads(
        await lookup.call(
            _context(_event(umo="session:b", user="300")), kind="history", limit=10
        )
    )
    assert f"gen:{job_id}" not in [item["ref"] for item in stranger["history"]]
    resolver = ReferenceResolver(
        image_handler=None,
        avatar_handler=None,
        generation_tracker=tracker,
        settings=lambda: {
            "cross_session": False,
            "history_enabled": True,
            "max_reference_images": 8,
            "allowed_dirs": [],
            "global_paths": False,
        },
    )
    resolved = await resolver.resolve(
        parse_references([f"gen:{job_id}"]),
        _event(umo="session:b", user="300"),
    )
    assert resolved.gallery_names == ["cat.png"]
    before = tracker.get(job_id)["shared"]
    unrecognized = json.loads(
        await tool.call(
            _context(_event(sender=False)), references=[f"gen:{job_id}"], shared=False
        )
    )
    assert unrecognized == {"error": "当前会话或用户无法识别，不能修改共享状态"}
    assert tracker.get(job_id)["shared"] is before
    tracker.enabled = False
    disabled = json.loads(
        await tool.call(_context(_event()), references=[f"gen:{job_id}"], shared=False)
    )
    assert disabled == {"error": "生成历史未开启"}
    await tracker.close()


def test_new_tools_replace_old_registrations():
    source = Path("main.py").read_text(encoding="utf-8")
    start = source.index("def _register_llm_tools")
    body = source[start : source.index("\n    def ", start + 1)]
    assert "GeminiImageLookupTool" in body and "GeminiImageShareTool" in body
    assert "lookup_tool, share_tool" in body
    assert (
        "ProviderModelQueryTool" not in body and "BackgroundTaskStatusTool" not in body
    )
    assert "gemini_image_provider_models" not in body
    assert "gemini_image_task_status" not in body
