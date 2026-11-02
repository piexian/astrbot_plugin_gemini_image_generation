"""提示词模板库：校验、渲染、存储、包、协议，以及快速命令接线。"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from tests.test_limit_admission import _command_class, _Event, _Limiter
from tests.test_v2_command_paths import _FakeV2Service, _wire_sender
from tests.test_web_studio_service import _config
from tl.core.results import GenerationResult
from tl.enhanced_prompts import (
    get_avatar_prompt,
    get_card_prompt,
    get_figure_prompt,
    get_mobile_prompt,
    get_poster_prompt,
    get_q_version_sticker_prompt,
    get_sticker_prompt,
    get_wallpaper_prompt,
)
from tl.prompt_templates import (
    PromptTemplate,
    PromptTemplateStore,
    SearchPage,
    SourceAuthError,
    SourceRateLimited,
    SourceRejected,
    SourceStatus,
    SourceUnavailable,
    TemplateSource,
    TemplateValidationError,
    builtin_template,
    export_package,
    parse_package,
    render_template,
    validate_source_url,
    validate_template_data,
)
from tl.prompt_templates import store as store_module

STAMP = "2026-01-01T00:00:00Z"
FIXTURE = Path(__file__).parent / "fixtures" / "gemini_image_templates.json"
LABELS = {
    "poster": "海报主题与要求：",
    "wallpaper": "画面描述：",
    "card": "卡片内容：",
    "mobile": "画面描述：",
}


def _item(**overrides):
    data = {
        "id": "local-1",
        "name": "名称",
        "content": "正文",
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    data.update(overrides)
    return data


def _assert_field(payload, field):
    with pytest.raises(TemplateValidationError) as caught:
        validate_template_data(payload)
    assert field in str(caught.value)
    assert "正文" not in str(caught.value) or field == "content"


def test_field_boundaries():
    assert validate_template_data(_item(name="字" * 40)).name == "字" * 40
    _assert_field(_item(name="字" * 41), "name")
    _assert_field(_item(name="  "), "name")
    _assert_field(_item(id="Bad"), "id")
    _assert_field(_item(id="a" * 49), "id")
    cleaned = validate_template_data(
        _item(ai_description="说\n明\u0000", author="作\u0001者\t", name="名\n称")
    )
    assert cleaned.ai_description == "说明"
    assert cleaned.author == "作者"
    assert cleaned.name == "名称"
    assert validate_template_data(_item(content="a" * 8000)).content
    _assert_field(_item(content="a" * 8001), "content")
    _assert_field(_item(content="   "), "content")
    _assert_field(_item(content="有\u0000控制符"), "content")
    for bad in ("{{ input }}", "{{user_input}}", "{{}}", "{{未闭合"):
        _assert_field(_item(content=bad), "content")
    assert validate_template_data(_item(content="{{rows}}x{{cols}} {{input}}")).content
    _assert_field(_item(defaults={"size": "2K"}), "defaults")
    _assert_field(_item(defaults="2K"), "defaults")
    _assert_field(_item(defaults={"resolution": "2k"}), "defaults")
    _assert_field(_item(defaults={"aspect_ratio": "16x9"}), "defaults")
    ok = validate_template_data(
        _item(defaults={"resolution": "12K", "aspect_ratio": "16:9"})
    )
    assert ok.defaults == {"resolution": "12K", "aspect_ratio": "16:9"}
    for value in ("none", "optional", "required"):
        assert validate_template_data(_item(reference=value)).reference == value
    _assert_field(_item(reference="sometimes"), "reference")
    tags = validate_template_data(_item(tags=["a", "a", " b "])).tags
    assert tags == ["a", "b"]
    _assert_field(_item(tags=[str(i) for i in range(11)]), "tags")
    _assert_field(_item(tags=["字" * 21]), "tags")
    assert validate_template_data(_item(tags=["字" * 20])).tags == ["字" * 20]
    _assert_field(_item(nsfw=True), "nsfw")
    assert validate_template_data(_item(nsfw=False, extra=1)).id == "local-1"


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/",
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://example.com/",
        "//example.com",
        "example.com",
        "https://",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://2130706433/",
        "https://0x7f.1/",
        "https://user:pw@example.com/",
        "https://exa mple.com/",
        "https://example.com/a\tb",
        "https://example.com/a\nb",
        "https://example.com/a\u0000b",
        "https://example.com/" + "a" * 281,
        "https://example.com/" + "字" * 200,
        "https://[::1",
        "https://example.com:99999/",
        "https://example.com:abc/",
        "https://1.2.3.4.5/",
        "https://example.123/",
    ],
)
def test_source_url_rejects(value):
    with pytest.raises(TemplateValidationError) as caught:
        validate_source_url(value)
    message = str(caught.value)
    assert "source_url" in message
    assert value not in message
    assert "pw" not in message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("   ", None),
        ("\u3000", None),
        (
            "https://ai.google.dev/gemini-api/docs/image-generation?hl=zh-cn#prompting",
            "https://ai.google.dev/gemini-api/docs/image-generation?hl=zh-cn#prompting",
        ),
        ("https://AI.Google.DEV/Docs/Image", "https://ai.google.dev/Docs/Image"),
        ("HTTPS://Example.com/a", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("  https://example.com/a \n", "https://example.com/a"),
        ("https://中文.中国/", "https://xn--fiq228c.xn--fiqs8s/"),
        ("https://example.com/路径", "https://example.com/%E8%B7%AF%E5%BE%84"),
        ("https://example.com/" + "a" * 280, "https://example.com/" + "a" * 280),
    ],
)
def test_source_url_normalizes(value, expected):
    assert validate_source_url(value) == expected


def test_render_replaces_placeholders_once():
    template = validate_template_data(_item(content="网格{{rows}}*{{cols}}\n{{input}}"))
    rendered = render_template(template, "看到{{rows}}", rows=6, cols=4)
    assert rendered == "网格6*4\n看到{{rows}}"
    plain = validate_template_data(_item(content="只要画面"))
    assert render_template(plain, "") == "只要画面"
    assert render_template(plain, " 一只猫 ") == "只要画面\n用户需求：一只猫"


def test_builtin_prompts_match_previous_wording():
    for key, old_fn in {
        "avatar": get_avatar_prompt,
        "poster": get_poster_prompt,
        "wallpaper": get_wallpaper_prompt,
        "card": get_card_prompt,
        "mobile": get_mobile_prompt,
    }.items():
        template = builtin_template(key)
        for text in ("", "一只猫"):
            old = old_fn(text)
            new = render_template(template, text)
            label = LABELS.get(key)
            assert new == (old if label is None else old.replace(label, "用户需求："))
    pvc = builtin_template("figure-pvc")
    for text in ("", "一只猫"):
        assert render_template(pvc, text) == get_figure_prompt(text, 1)
    gk = builtin_template("figure-gk")
    assert render_template(gk, "") == get_figure_prompt("", 2)
    assert (
        render_template(gk, "一只猫") == get_figure_prompt("", 2) + "\n用户需求：一只猫"
    )
    assert get_figure_prompt("一只猫", 2) == get_figure_prompt("", 2) + "\n一只猫"
    for rows, cols in ((4, 4), (6, 4)):
        old_empty = get_sticker_prompt("", rows=rows, cols=cols)
        new_empty = render_template(
            builtin_template("sticker"), "", rows=rows, cols=cols
        )
        assert old_empty == new_empty + "\n"
        old = get_sticker_prompt("一只猫", rows=rows, cols=cols)
        new = render_template(
            builtin_template("sticker"), "一只猫", rows=rows, cols=cols
        )
        assert old == new_empty + "\n\n附加要求：一只猫"
        assert new == new_empty + "\n用户需求：一只猫"
        simple = builtin_template("sticker-simple")
        base = get_q_version_sticker_prompt("", rows=rows, cols=cols)
        assert render_template(simple, "", rows=rows, cols=cols) == base
        assert (
            render_template(simple, "一只猫", rows=rows, cols=cols)
            == base + "\n用户需求：一只猫"
        )
        assert (
            get_q_version_sticker_prompt("一只猫", rows=rows, cols=cols)
            == base + "\n\nAdditional user requirements: 一只猫"
        )


@pytest.mark.asyncio
async def test_store_roundtrip_override_and_limits(tmp_path, monkeypatch):
    store = PromptTemplateStore(tmp_path)
    created = await store.create({"name": "海报", "content": "自定义海报"})
    assert (
        created.id
        == "t-" + __import__("hashlib").sha256("海报".encode()).hexdigest()[:8]
    )
    assert created.origin == "local"
    loaded = PromptTemplateStore(tmp_path)
    assert loaded.get(created.id).content == "自定义海报"
    assert [item.id for item in loaded.list_templates()][:9] == [
        item.id for item in loaded.list_templates() if item.origin == "builtin"
    ]
    updated = await store.update(
        "poster", {"content": "覆盖后的海报", "defaults": {"resolution": "4K"}}
    )
    assert updated.content == "覆盖后的海报"
    assert updated.defaults["resolution"] == "4K"
    assert updated.defaults["aspect_ratio"] == "16:9"
    assert store.builtin_override("poster")["defaults"] == {"resolution": "4K"}
    restored = await store.restore_builtin("poster")
    assert restored.content == builtin_template("poster").content
    assert store.builtin_override("poster") == {}
    with pytest.raises(TemplateValidationError):
        await store.delete("poster")
    with pytest.raises(TemplateValidationError):
        await store.create({"id": "poster", "name": "冲突", "content": "x"})
    disabled = await store.set_ai_enabled(created.id, False)
    assert disabled.ai_enabled is False

    monkeypatch.setattr(store_module, "_MAX_LOCAL", 1)
    with pytest.raises(TemplateValidationError, match="1000"):
        await store.create({"name": "另一条", "content": "y"})
    monkeypatch.setattr(store_module, "_MAX_LOCAL", 1000)
    monkeypatch.setattr(store_module, "_MAX_BYTES", 32)
    with pytest.raises(TemplateValidationError, match="8 MB"):
        await store.update(created.id, {"content": "仍然很短但超过测试上限"})
    assert PromptTemplateStore(tmp_path).get(created.id).ai_enabled is False


@pytest.mark.asyncio
async def test_corrupt_file_is_kept_and_newer_schema_is_not_overwritten(tmp_path):
    path = tmp_path / "prompt_templates.json"
    path.write_text("{", encoding="utf-8")
    store = PromptTemplateStore(tmp_path)
    assert store.get("avatar").name == "头像"
    renamed = list(tmp_path.glob("prompt_templates.json.corrupt-*"))
    assert len(renamed) == 1
    assert not path.exists()

    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "templates": [_item(content="不能丢")],
                "builtin_overrides": {"poster": {"content": "不能丢"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()
    readonly = PromptTemplateStore(tmp_path)
    assert readonly.get("poster").content == builtin_template("poster").content
    with pytest.raises(TemplateValidationError, match="版本过新"):
        await readonly.create({"name": "新", "content": "x"})
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_invalid_record_is_skipped_and_concurrent_creates_keep_every_row(
    tmp_path,
):
    path = tmp_path / "prompt_templates.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "templates": [_item(id="good", content="好的"), {"id": "bad"}],
                "builtin_overrides": {"poster": {"content": ""}, "missing": {}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = PromptTemplateStore(tmp_path)
    assert store.get("good").content == "好的"
    assert store.get("bad") is None
    assert store.builtin_override("poster") == {}

    fresh = PromptTemplateStore(tmp_path / "concurrent")

    async def create(index):
        return await fresh.create({"name": f"item-{index}", "content": f"c{index}"})

    created = await asyncio.gather(*(create(index) for index in range(20)))
    assert len({item.id for item in created}) == 20
    assert len(PromptTemplateStore(tmp_path / "concurrent").list_templates()) == 29


def test_package_roundtrip_and_item_errors():
    package = json.loads(FIXTURE.read_text(encoding="utf-8"))
    package["templates"][0]["preview"] = "cover.png"
    package["templates"][0]["ai_enabled"] = False
    package["templates"][0]["origin"] = "remote"
    package["templates"][0]["unknown"] = {"ignored": True}
    accepted, errors = parse_package(package)
    assert errors == []
    assert accepted[0].reference == "required"
    assert "{{rows}}" in accepted[0].content and "{{cols}}" in accepted[0].content
    assert accepted[0].author == "示例作者"
    assert accepted[0].source_url.startswith("https://ai.google.dev/")
    assert accepted[0].preview is None
    assert accepted[0].ai_enabled is True
    assert accepted[0].origin == "local"
    exported = export_package(accepted)
    again, again_errors = parse_package(exported)
    assert again_errors == []
    assert [item.content for item in again] == [item.content for item in accepted]
    assert "preview" not in exported["templates"][0]
    assert "ai_enabled" not in exported["templates"][0]

    broken = json.loads(FIXTURE.read_text(encoding="utf-8"))
    broken["templates"][1]["source_url"] = "http://example.com/"
    broken["templates"].append({"name": "缺字段"})
    accepted, errors = parse_package(broken)
    assert [item.id for item in accepted] == [
        "line-sticker",
        "avatar-plain",
        "figure-note",
    ]
    assert [item.index for item in errors] == [1, 4]
    assert "source_url" in errors[0].message
    with pytest.raises(TemplateValidationError):
        parse_package({"format": "other", "version": 1, "templates": []})
    with pytest.raises(TemplateValidationError):
        parse_package(
            {"format": "gemini-image-templates", "version": 1, "templates": [{}] * 501}
        )


@pytest.mark.asyncio
async def test_import_assigns_ids_unless_kept(tmp_path):
    store = PromptTemplateStore(tmp_path)
    package = json.loads(FIXTURE.read_text(encoding="utf-8"))
    package["templates"].append(
        {
            "id": "poster",
            "name": "冲突",
            "content": "x",
            "created_at": STAMP,
            "updated_at": STAMP,
        }
    )
    assigned = await store.import_package(package)
    assert [item["ok"] for item in assigned] == [True, True, True, True, True]
    assert all(
        item["id"] not in {row["id"] for row in package["templates"]}
        for item in assigned
        if item["ok"]
    )
    kept = await store.import_package(package, keep_ids=True)
    assert kept[0]["ok"] is True and kept[0]["id"] == "line-sticker"
    assert kept[-1]["ok"] is False


class _MemorySource:
    def __init__(self):
        self.deleted = []

    async def search(self, query, *, tag=None, cursor=None, limit=20):
        return SearchPage([], None)

    async def get_many(self, ids):
        return []

    async def status(self):
        return SourceStatus("1", 0, {"read": 10})

    async def catalog_version(self):
        from tl.prompt_templates import CatalogVersion

        return CatalogVersion("abc", 0, STAMP, 0)

    async def upload(self, template):
        if not template.content:
            raise SourceRejected("正文为空")
        return template

    async def update(self, template_id, template, *, etag=None):
        if etag == "stale":
            raise SourceAuthError("密钥无效")
        return template

    async def delete(self, template_id):
        self.deleted.append(template_id)


@pytest.mark.asyncio
async def test_memory_source_satisfies_protocol():
    source = _MemorySource()
    assert isinstance(source, TemplateSource)
    page = await source.search("猫", tag="海报", limit=5)
    assert page.templates == []
    status = await source.status()
    assert status.rate_limits["read"] == 10
    template = builtin_template("avatar")
    assert (await source.upload(template)).id == "avatar"
    with pytest.raises(SourceAuthError):
        await source.update("avatar", template, etag="stale")
    await source.delete("avatar")
    assert source.deleted == ["avatar"]
    with pytest.raises(SourceRejected):
        await source.upload(PromptTemplate(id="x", name="x", content=""))
    assert issubclass(SourceRateLimited, SourceUnavailable.__bases__[0])
    assert (
        SourceUnavailable("down").__class__.__mro__[1].__name__ == "TemplateSourceError"
    )


def _command_plugin(tmp_path):
    plugin = _command_class()()
    plugin.cfg = _config()
    plugin.cfg.preserve_reference_image_size = False
    plugin.cfg.enable_smart_retry = True
    plugin.cfg.enable_sticker_split = False
    plugin.cfg.sticker_grid_rows = plugin.cfg.sticker_grid_cols = 4
    plugin.cfg.quick_mode_overrides = {}
    plugin.rate_limiter = _Limiter()
    plugin.prompt_templates = PromptTemplateStore(tmp_path)
    plugin._ensure_api_client = lambda: plugin.api_client is not None
    plugin.avatar_handler = type("A", (), {})()
    plugin.avatar_handler.should_use_avatar = _async_false
    plugin.avatar_handler.should_use_avatar_for_prompt = _async_false
    plugin.image_handler = type("H", (), {})()
    plugin.image_handler.fetch_images_from_event = _async_refs
    plugin.image_handler.filter_valid_reference_images = lambda values, **kwargs: list(
        values
    )
    plugin.image_generator = type(
        "G", (), {"generate_image_core": _async_fail, "submit_v2": _async_fail}
    )()
    plugin._v2_service_for = lambda umo: None
    return plugin


async def _async_false(*args, **kwargs):
    return False


async def _async_refs(*args, **kwargs):
    return (["ref.png"], [])


async def _async_fail(*args, **kwargs):
    return False, "provider failed"


class _Client:
    def __init__(self):
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        return (["https://img.test/out.png"], [], "ok", None)


class _CaptureService(_FakeV2Service):
    def __init__(self):
        super().__init__(
            result=GenerationResult(artifacts=("https://img.test/a.png",), text="ok")
        )
        self.request = None

    async def submit(self, request, *, admission=None):
        self.request = request
        return await super().submit(request, admission=admission)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "prompt", "template_id", "rows", "cols"),
    [
        ("quick_avatar", "一只猫", "avatar", 4, 4),
        ("quick_poster", "一只猫", "poster", 4, 4),
        ("quick_wallpaper", "", "wallpaper", 4, 4),
        ("quick_card", "一只猫", "card", 4, 4),
        ("quick_mobile", "一只猫", "mobile", 4, 4),
        ("quick_figure", "一只猫", "figure-pvc", 4, 4),
        ("quick_figure", "2 一只猫", "figure-gk", 4, 4),
        ("quick_sticker", "一只猫", "sticker", 6, 4),
        ("quick_sticker", "简单 一只猫", "sticker-simple", 6, 4),
    ],
)
async def test_quick_commands_render_templates_on_both_paths(
    tmp_path, method, prompt, template_id, rows, cols
):
    plugin = _command_plugin(tmp_path)
    plugin.cfg.sticker_grid_rows, plugin.cfg.sticker_grid_cols = rows, cols
    client = _Client()
    plugin.api_client = client
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""
    outputs = [item async for item in getattr(plugin, method)(event, prompt)]
    assert any("生成" in item for item in outputs)
    rendered_input = "一只猫" if "一只猫" in prompt else ""
    expected = render_template(
        builtin_template(template_id), rendered_input, rows=rows, cols=cols
    )
    assert client.configs[0].prompt == expected
    assert (
        plugin.generation_tracker.begin.await_args.kwargs["params"]["template_id"]
        == template_id
    )

    plugin = _command_plugin(tmp_path / "v2")
    plugin.cfg.sticker_grid_rows, plugin.cfg.sticker_grid_cols = rows, cols
    service = _CaptureService()
    plugin._v2_service_for = lambda umo: service
    plugin.api_client = _Client()
    _wire_sender(plugin)
    outputs = [item async for item in getattr(plugin, method)(event, prompt)]
    assert any("生成" in item for item in outputs)
    assert service.request.metadata["template_id"] == template_id
    assert service.request.prompt == expected
    plugin.generation_tracker.begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_dimensions_follow_override_then_config_then_builtin(tmp_path):
    plugin = _command_plugin(tmp_path)
    client = _Client()
    plugin.api_client = client
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""
    await _collect(plugin.quick_avatar(event, "一只猫"))
    assert (client.configs[-1].resolution, client.configs[-1].aspect_ratio) == (
        "1K",
        "1:1",
    )

    plugin.cfg.quick_mode_overrides = {"avatar": ("2K", "9:16")}
    await _collect(plugin.quick_avatar(event, "一只猫"))
    assert (client.configs[-1].resolution, client.configs[-1].aspect_ratio) == (
        "2K",
        "9:16",
    )

    await plugin.prompt_templates.update("avatar", {"defaults": {"resolution": "4K"}})
    await _collect(plugin.quick_avatar(event, "一只猫"))
    assert (client.configs[-1].resolution, client.configs[-1].aspect_ratio) == (
        "4K",
        "9:16",
    )


@pytest.mark.asyncio
async def test_sticker_reference_rule_and_corrupt_store(tmp_path):
    plugin = _command_plugin(tmp_path)

    async def no_images(*args, **kwargs):
        return ([], [])

    plugin.image_handler.fetch_images_from_event = no_images
    event = _Event()
    event.message_str = ""
    outputs = await _collect(plugin.quick_sticker(event, "一只猫"))
    assert "请附上一张清晰的角色参考图（如头像或原表情）后再试。" in outputs[1]

    await plugin.prompt_templates.update("sticker", {"reference": "optional"})
    client = _Client()
    plugin.api_client = client
    _wire_sender(plugin)
    await _collect(plugin.quick_sticker(event, "一只猫"))
    assert client.configs[0].prompt

    broken = tmp_path / "broken"
    (broken / "prompt_templates.json").parent.mkdir()
    (broken / "prompt_templates.json").write_text("{", encoding="utf-8")
    plugin.prompt_templates = PromptTemplateStore(broken)
    plugin.api_client = _Client()
    _wire_sender(plugin)
    plugin.image_handler.fetch_images_from_event = _async_refs
    await _collect(plugin.quick_poster(event, "一只猫"))
    assert plugin.api_client.configs[0].prompt == render_template(
        builtin_template("poster"), "一只猫"
    )


async def _collect(generator):
    return [item async for item in generator]


def test_command_decorator_stays_on_handle_quick_mode():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert _provider_operation(found["_handle_quick_mode"]) == "command"
    assert _provider_operation(found["_quick_template"]) is None
    assert _provider_operation(found["_quick_dimensions"]) is None


def _provider_operation(node):
    for decorator in node.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "provider_operation"
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
        ):
            return decorator.args[0].value
    return None


@pytest.mark.asyncio
async def test_poster_route_is_parsed_before_template_render(tmp_path):
    plugin = _command_plugin(tmp_path)
    client = _Client()
    plugin.api_client = client
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""
    await _collect(plugin.quick_poster(event, "google/image-model 一只猫"))
    config = client.configs[0]
    assert (config.requested_provider, config.requested_model) == (
        "google",
        "image-model",
    )
    assert "google/image-model" not in config.prompt
    assert config.prompt == render_template(builtin_template("poster"), "一只猫")


@pytest.mark.asyncio
async def test_avatar_keyword_uses_the_original_text(tmp_path):
    import sys

    utils = sys.modules["tl.tl_utils"]
    utils.AvatarManager = getattr(utils, "AvatarManager", type("AvatarManager", (), {}))
    utils.download_qq_avatar = getattr(utils, "download_qq_avatar", None)
    import tl.avatar_handler as avatar_module

    avatar_module.logger = type(
        "Logger",
        (),
        {
            "info": staticmethod(lambda *args, **kwargs: None),
            "warning": staticmethod(lambda *args, **kwargs: None),
            "debug": staticmethod(lambda *args, **kwargs: None),
        },
    )()
    plugin = _command_plugin(tmp_path)
    plugin.avatar_handler = avatar_module.AvatarHandler(auto_avatar_reference=True)
    flags = []

    async def fetch(event, include_at_avatars=False):
        flags.append(include_at_avatars)
        return ([], [])

    plugin.image_handler.fetch_images_from_event = fetch
    plugin.api_client = _Client()
    _wire_sender(plugin)
    event = _Event()
    event.message_str = ""
    event.get_messages = lambda: []
    event.get_self_id = lambda: "bot"
    await _collect(plugin.quick_avatar(event, "一只猫"))
    assert flags == [False]
    flags.clear()
    await _collect(plugin.quick_avatar(event, "我的头像"))
    assert flags == [True]


@pytest.mark.asyncio
async def test_import_without_id_or_timestamps_gets_a_local_id(tmp_path):
    store = PromptTemplateStore(tmp_path)
    package = {
        "format": "gemini-image-templates",
        "version": 1,
        "templates": [{"name": "夜色海报", "content": "只要画面"}],
    }
    accepted, errors = parse_package(package)
    assert errors == []
    assert accepted[0].id == "import-0"
    assert accepted[0].created_at.endswith("Z")
    imported = await store.import_package(package)
    assert imported[0]["ok"] is True
    assert imported[0]["id"] not in {None, "import-0"}
    assert store.get(imported[0]["id"]).content == "只要画面"
    kept = await store.import_package(package, keep_ids=True)
    assert kept[0]["ok"] is False
    assert "id" in kept[0]["error"]
