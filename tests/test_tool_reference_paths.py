"""reference_image_paths 参数与 tool_path_guard 的单元测试。"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import tl.tool_path_guard as path_guard
from tl.core.results import GenerationResult
from tl.core.states import JobState
from tl.image_generator import ImageGenerator
from tl.tool_path_guard import (
    expand_allowed_dirs,
    filter_reference_paths,
    is_path_allowed,
    is_supported_image_file,
    normalize_candidate,
    raw_has_traversal,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/"
    "iZk9HQAAAABJRU5ErkJggg=="
)

# ---------- raw_has_traversal ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a/b.png", False),
        ("a..b.png", False),
        ("../etc/passwd", True),
        ("a/../b", True),
        ("foo/../../etc", True),
        ("foo/%2e%2e/etc", True),
        ("a//b", False),
        ("/foo/bar", False),
        ("", False),
    ],
)
def test_raw_has_traversal_posix(raw, expected):
    assert raw_has_traversal(raw) is expected


def test_raw_has_traversal_windows_style():
    # backslash 会被统一为 / 处理
    assert raw_has_traversal("a\\..\\b") is True
    assert raw_has_traversal("..\\passwd") is True
    assert raw_has_traversal("a\\b\\c.png") is False


# ---------- normalize_candidate ----------


def test_normalize_candidate_strips_quotes(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(PNG_1X1)
    p = normalize_candidate(f'"{f}"')
    assert p is not None and p == f.resolve()


def test_normalize_candidate_empty():
    assert normalize_candidate("") is None
    assert normalize_candidate("   ") is None


def test_normalize_candidate_expanduser(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = normalize_candidate("~/sub/a.png")
    assert p is not None
    assert str(tmp_path) in str(p)


def test_normalize_candidate_file_uri(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(PNG_1X1)
    p = normalize_candidate(f.resolve().as_uri())
    assert p is not None and p == f.resolve()


# ---------- is_path_allowed ----------


def test_is_path_allowed_inside(tmp_path):
    sub = tmp_path / "tool_images"
    sub.mkdir()
    allowed = expand_allowed_dirs([str(tmp_path)])
    f = sub / "a.png"
    f.write_bytes(b"data")
    assert is_path_allowed(f.resolve(), allowed) is True


def test_is_path_allowed_outside(tmp_path):
    allowed = expand_allowed_dirs([str(tmp_path)])
    # /etc/passwd 通常存在但不属于 tmp_path
    etc = Path("/etc/passwd")
    if etc.exists():
        assert is_path_allowed(etc.resolve(), allowed) is False


def test_is_path_allowed_rejects_sibling_prefix(tmp_path):
    sibling = tmp_path.parent / f"{tmp_path.name}-other"
    sibling.mkdir()
    f = sibling / "a.png"
    f.write_bytes(b"data")
    allowed = expand_allowed_dirs([str(tmp_path)])
    assert is_path_allowed(f.resolve(), allowed) is False


def test_is_path_allowed_empty_allowed(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"data")
    assert is_path_allowed(f.resolve(), []) is False


# ---------- filter_reference_paths ----------


def _make_img(dir_path: Path, name: str = "a.png") -> Path:
    p = dir_path / name
    p.write_bytes(PNG_1X1)
    return p


def test_is_supported_image_file_rejects_broken_image(tmp_path):
    f = tmp_path / "broken.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert is_supported_image_file(f) is False


def test_is_supported_image_file_rejects_non_image(tmp_path):
    f = tmp_path / "not-image.png"
    f.write_text("secret", encoding="utf-8")
    assert is_supported_image_file(f) is False


def test_is_supported_image_file_registers_heif_opener_when_available(
    monkeypatch,
    tmp_path,
):
    calls = []
    heif_module = types.ModuleType("pillow_heif")
    heif_module.register_heif_opener = lambda: calls.append("registered")
    monkeypatch.setitem(sys.modules, "pillow_heif", heif_module)
    f = _make_img(tmp_path)

    assert is_supported_image_file(f) is True
    assert calls == ["registered"]


def test_filter_whitelist_accepts_inside(tmp_path):
    f = _make_img(tmp_path)
    accepted, rejected = filter_reference_paths(
        [str(f)], allowed_dirs=[str(tmp_path)], global_mode=False
    )
    assert accepted == [f.resolve().as_uri()]
    assert rejected == []


def test_filter_whitelist_accepts_quoted_path_as_normalized_uri(tmp_path):
    f = _make_img(tmp_path)
    accepted, rejected = filter_reference_paths(
        [f'"{f}"'], allowed_dirs=[str(tmp_path)], global_mode=False
    )
    assert accepted == [f.resolve().as_uri()]
    assert rejected == []


def test_filter_whitelist_rejects_outside(tmp_path):
    # /etc/passwd 在白名单外
    etc = Path("/etc/passwd")
    if not etc.exists():
        pytest.skip("/etc/passwd 不存在，无法测试越界拒绝")
    accepted, rejected = filter_reference_paths(
        [str(etc)], allowed_dirs=[str(tmp_path)], global_mode=False
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_whitelist_rejects_outside_before_image_validation(
    monkeypatch,
    tmp_path,
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    ref = _make_img(outside)

    def fail_if_called(path):
        raise AssertionError(f"不应打开白名单外文件: {path}")

    monkeypatch.setattr(path_guard, "is_supported_image_file", fail_if_called)

    accepted, rejected = path_guard.filter_reference_paths(
        [str(ref)], allowed_dirs=[str(allowed)], global_mode=False
    )

    assert accepted == []
    assert rejected == [str(ref)]


def test_filter_rejects_traversal_even_in_whitelist(tmp_path):
    # 含 .. 的路径即便最终落在白名单内也直接拒绝
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_img(sub, "a.png")
    # 构造含 .. 的等价路径
    rel_with_dotdot = sub / ".." / "sub" / "a.png"
    accepted, rejected = filter_reference_paths(
        [str(rel_with_dotdot)], allowed_dirs=[str(tmp_path)], global_mode=False
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_rejects_nonexistent(tmp_path):
    accepted, rejected = filter_reference_paths(
        [str(tmp_path / "noexist.png")],
        allowed_dirs=[str(tmp_path)],
        global_mode=False,
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_rejects_non_image_even_in_global_mode(tmp_path):
    f = tmp_path / "not-image.png"
    f.write_text("secret", encoding="utf-8")
    accepted, rejected = filter_reference_paths(
        [str(f)], allowed_dirs=[], global_mode=True
    )
    assert accepted == []
    assert rejected == [str(f)]


def test_filter_global_mode_accepts_any_existing_image(tmp_path):
    f = _make_img(tmp_path)
    accepted, rejected = filter_reference_paths(
        [str(f)], allowed_dirs=[], global_mode=True
    )
    assert accepted == [f.resolve().as_uri()]
    assert rejected == []


def test_filter_global_mode_still_rejects_traversal(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_img(sub, "a.png")
    rel_with_dotdot = str(sub / ".." / "sub" / "a.png")
    accepted, rejected = filter_reference_paths(
        [rel_with_dotdot], allowed_dirs=[], global_mode=True
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_global_mode_rejects_nonexistent():
    accepted, rejected = filter_reference_paths(
        ["/this/does/not/exist.png"], allowed_dirs=[], global_mode=True
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_skips_non_string_and_empty(tmp_path):
    accepted, rejected = filter_reference_paths(
        ["", None, "   "],  # type: ignore[list-item]
        allowed_dirs=[str(tmp_path)],
        global_mode=False,
    )
    assert accepted == []
    assert len(rejected) == 3


def test_filter_rejects_non_iterable_raw_paths(tmp_path):
    accepted, rejected = filter_reference_paths(
        True,
        allowed_dirs=[str(tmp_path)],
        global_mode=False,
    )
    assert accepted == []
    assert rejected == ["True"]


def test_filter_rejects_mapping_raw_paths(tmp_path):
    accepted, rejected = filter_reference_paths(
        {"path": str(tmp_path / "a.png")},
        allowed_dirs=[str(tmp_path)],
        global_mode=False,
    )
    assert accepted == []
    assert len(rejected) == 1


def test_filter_rejects_mixed_non_string_items(tmp_path):
    f = _make_img(tmp_path)
    accepted, rejected = filter_reference_paths(
        [str(f), 123, False],
        allowed_dirs=[str(tmp_path)],
        global_mode=False,
    )
    assert accepted == [f.resolve().as_uri()]
    assert rejected == ["123", "False"]


def test_image_handler_keeps_file_uri_reference_images(monkeypatch):
    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.At = type("At", (), {})
    components_module.Image = type("Image", (), {})
    components_module.Reply = type("Reply", (), {})
    monkeypatch.setitem(
        sys.modules, "astrbot.api.message_components", components_module
    )

    tl_utils_module = sys.modules["tl.tl_utils"]
    monkeypatch.setattr(
        tl_utils_module, "AvatarManager", type("AvatarManager", (), {}), raising=False
    )
    monkeypatch.setattr(
        tl_utils_module, "is_valid_base64_image_str", lambda value: False, raising=False
    )

    from tl.image_handler import ImageHandler

    image_handler = ImageHandler(log_debug_fn=lambda msg: None)
    file_uri = "file:///tmp/tool_images/ref.png"

    assert image_handler.filter_valid_reference_images([file_uri], "消息图片") == [
        file_uri
    ]


# ---------- expand_allowed_dirs ----------


def test_expand_default_dirs_always_present():
    # 默认目录即使不存在也保留
    resolved = expand_allowed_dirs(None)
    # ~/.astrbot/data 经 expanduser 后应存在
    home_data = Path("~/.astrbot/data").expanduser().resolve(strict=False)
    assert home_data in resolved


def test_expand_user_configured_must_exist(tmp_path):
    resolved = expand_allowed_dirs([str(tmp_path)])
    assert tmp_path.resolve() in resolved
    # 不存在的用户目录被跳过
    resolved2 = expand_allowed_dirs([str(tmp_path / "noexist")])
    assert tmp_path.resolve() not in resolved2


def test_expand_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(tmp_path))
    resolved = expand_allowed_dirs(None)
    assert tmp_path.resolve() in resolved


def test_expand_env_var_skipped_when_empty(monkeypatch):
    monkeypatch.setenv("ASTRBOT_DATA_PATH", "   ")
    # 不应抛错
    resolved = expand_allowed_dirs(None)
    assert isinstance(resolved, list)


# ---------- schema 集成 ----------


def test_tool_base_properties_has_references():
    import sys
    import types

    if "tl.llm_tools" not in sys.modules:
        for name in (
            "astrbot.core",
            "astrbot.core.agent",
            "astrbot.core.agent.run_context",
            "astrbot.core.agent.tool",
            "astrbot.core.astr_agent_context",
        ):
            if name not in sys.modules:
                sys.modules[name] = types.ModuleType(name)
        sys.modules["astrbot.core.agent.run_context"].ContextWrapper = type(
            "ContextWrapper", (), {}
        )
        sys.modules["astrbot.core.agent.tool"].FunctionTool = type(
            "FunctionTool", (), {"__class_getitem__": classmethod(lambda cls, it: cls)}
        )
        sys.modules["astrbot.core.agent.tool"].ToolExecResult = type(
            "ToolExecResult", (), {}
        )
        sys.modules["astrbot.core.astr_agent_context"].AstrAgentContext = type(
            "AstrAgentContext", (), {}
        )
        if "mcp" not in sys.modules:
            mcp_module = types.ModuleType("mcp")
            mcp_module.types = types.ModuleType("mcp.types")
            sys.modules["mcp"] = mcp_module
            sys.modules["mcp.types"] = mcp_module.types

    from tl.llm_tools import _build_tool_base_properties

    props = _build_tool_base_properties()
    assert "references" in props
    assert props["references"]["type"] == "array"


# ---------- GeminiImageGenerationTool.call 集成 ----------


class _FakeAvatarManager:
    pass


class _FakeV2Service:
    """轻量 v2 投影：只记录提交请求与参考图，不经过调度器。"""

    def __init__(self, generated_path: Path) -> None:
        self.generated_path = generated_path
        self.requests: list[Any] = []
        self.prepared_references: list[tuple[str, ...]] = []
        self.trusted_local: list[bool] = []
        self.closed = False

    async def prepare_references(self, references, *, requester, trusted_local=False):
        self.prepared_references.append(tuple(references))
        self.trusted_local.append(bool(trusted_local))
        return tuple(references)

    async def submit(self, request, *, admission=None, delivery=None):
        self.requests.append(request)
        return SimpleNamespace(job_id="job-fake-1", umo=request.requester.get("umo"))

    def result_wait_timeout(self, deadline_seconds):
        return deadline_seconds + 50

    def attach_delivery(self, job_id, delivery):
        return False

    async def wait(self, job_id, timeout=None):
        return SimpleNamespace(
            state=JobState.SUCCEEDED,
            result=GenerationResult(
                artifacts=(str(self.generated_path),),
                text=None,
                provider="legacy",
                model="auto",
                candidate_id="cand-1",
            ),
            error_code=None,
            error_message=None,
        )

    async def cancel(self, job_id):
        return None


class _FakePlugin:
    def __init__(
        self,
        *,
        allowed_dirs: list[str],
        generated_path: Path,
        event_refs: list[str] | None = None,
    ) -> None:
        self.cfg = SimpleNamespace(
            llm_tool_reference_path_mode="whitelist",
            llm_tool_reference_allowed_dirs=allowed_dirs,
            provider_candidates=[],
            reference_cross_session=False,
            preserve_reference_image_size=False,
        )
        self.api_client = object()
        self.avatar_manager = _FakeAvatarManager()
        self.avatar_handler = SimpleNamespace(
            download_user_avatar=AsyncMock(return_value="avatar-ref")
        )
        self.generated_path = generated_path
        self.event_refs = event_refs or []
        self.v2_service = _FakeV2Service(generated_path)
        self.captured_reference_images: list[str] | None = None
        self.captured_avatar_reference: list[str] | None = None
        self.message_sender = SimpleNamespace(
            merge_available_images=lambda urls, paths: [*(urls or []), *(paths or [])],
        )
        self.generation_tracker = SimpleNamespace(enabled=False)
        self.image_generator = ImageGenerator(
            context=None,
            api_client=object(),
            filter_valid_fn=lambda images, source: images or [],
        )
        self.image_handler = SimpleNamespace(
            fetch_images_from_event=lambda event, include_at_avatars=False: (
                self._fetch_images_from_event(
                    event, include_at_avatars=include_at_avatars
                )
            )
        )

    def _ensure_v2_service(self):
        return self.v2_service

    def get_tool_timeout(self, event):
        return 10

    async def _fetch_images_from_event(self, event, include_at_avatars=False):
        self.captured_avatar_reference = []
        return list(self.event_refs), []


def _tool_context():
    event = SimpleNamespace(get_sender_id=lambda: "sender-1")
    return SimpleNamespace(context=SimpleNamespace(event=event))


@pytest.mark.asyncio
async def test_tool_call_accepts_references_and_ignores_legacy_keys(tmp_path):
    from tl.llm_tools import GeminiImageGenerationTool

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    ref = _make_img(allowed, "ref.png")
    generated = _make_img(tmp_path, "generated.png")
    plugin = _FakePlugin(
        allowed_dirs=[str(allowed)],
        generated_path=generated,
        event_refs=["event-ref"],
    )

    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _tool_context(),
        prompt="改成水彩风",
        references=[str(ref)],
        deliver="return",
        use_reference_images=False,
        include_user_avatar=True,
        reference_image_paths=["/ignored/legacy.png"],
        for_forum=True,
    )

    assert "图像生成完成" in result
    # 用户本地图经路径守卫放行后按 trusted_local 提交，头像引用被旧键忽略。
    (request,) = plugin.v2_service.requests
    assert request.reference_images == (ref.resolve().as_uri(),)
    assert plugin.v2_service.prepared_references == [(ref.resolve().as_uri(),)]
    assert plugin.v2_service.trusted_local == [True]


@pytest.mark.asyncio
async def test_tool_call_rejects_reference_before_admission(tmp_path):
    from tl.llm_tools import GeminiImageGenerationTool

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    generated = _make_img(tmp_path, "generated.png")
    plugin = _FakePlugin(allowed_dirs=[str(allowed)], generated_path=generated)

    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _tool_context(),
        prompt="改成水彩风",
        references=[str(outside / "ref.png")],
    )

    assert "参考图解析失败" in result
    # 参考图解析失败发生在提交之前：没有 v2 请求，也没有建 Job。
    assert plugin.v2_service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("references", "event_refs", "avatar_result", "max_images", "expected"),
    [
        (["current"], [], "avatar-ref", 8, "当前消息没有图片"),
        (["avatar:sender"], [], None, 8, "头像获取失败"),
        (["current", "avatar:sender"], ["event-ref"], "avatar-ref", 1, "参考图共 2 张"),
    ],
)
async def test_tool_call_validates_reference_resolution_before_quota(
    tmp_path,
    references,
    event_refs,
    avatar_result,
    max_images,
    expected,
):
    from tl.llm_tools import GeminiImageGenerationTool

    plugin = _FakePlugin(
        allowed_dirs=[str(tmp_path)], generated_path=tmp_path / "unused.png"
    )
    plugin.event_refs = list(event_refs)
    plugin.image_generator = SimpleNamespace(
        max_reference_images=max_images, get_request_stats=lambda: {}
    )
    plugin.avatar_handler.download_user_avatar = AsyncMock(return_value=avatar_result)

    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _tool_context(), prompt="draw", references=references
    )

    assert expected in result
    # 前置校验不通过时不提交请求，因此不经调度器扣任何额度。
    assert plugin.v2_service.requests == []


@pytest.mark.asyncio
async def test_batch_reference_fields_remain_legacy_until_batch_split(tmp_path):
    from tl.llm_tools import _prepare_batch_tasks

    plugin = _FakePlugin(
        allowed_dirs=[str(tmp_path)], generated_path=tmp_path / "unused.png"
    )
    plugin.cfg.batch_max_tasks = 1
    plugin.cfg.batch_max_images_per_task = 1
    plugin.image_generator = SimpleNamespace(
        max_reference_images=2, get_request_stats=lambda: {}
    )
    plugin._fetch_images_from_event = AsyncMock(return_value=(["event-ref"], []))

    prepared, error = await _prepare_batch_tasks(
        plugin,
        _tool_context().context.event,
        [
            {
                "name": "draw",
                "prompt": "draw",
                "provider": "example",
                "image_count": 1,
                "use_reference_images": True,
            }
        ],
    )

    assert error is None
    assert prepared[0]["reference_images"] == ["event-ref"]
