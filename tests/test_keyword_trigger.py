"""关键词触发：归一化、匹配与注入计划的纯函数行为。

配置保存、生成路径注入与预演接口的覆盖见后续用例；本文件聚焦匹配语义。
"""

from __future__ import annotations

from tl.keyword_trigger import (
    KeywordBinding,
    KeywordTriggerService,
    match_auto,
    match_explicit,
    normalize_binding,
    normalize_bindings,
    normalize_trigger_settings,
    plan_expansion,
)


def _binding(
    order=0, words=("水彩",), ref_ids=("ref-aaaaaaaaaaaa",), append="", enabled=True
):
    return KeywordBinding(
        order=order,
        name=f"绑定{order + 1}",
        words=list(words),
        ref_ids=list(ref_ids),
        prompt_append=append,
        enabled=enabled,
    )


def _resolve(ref_ids):
    def resolve(ref_id):
        if ref_id in ref_ids:
            return {"path": f"/data/references/{ref_id}.png", "label": f"标签{ref_id}"}
        return None

    return resolve


# ---------- 设置归一化 ----------


def test_settings_defaults_when_raw_invalid():
    settings = normalize_trigger_settings(None)
    assert settings.mode == "command"
    assert settings.wrapper_left == ""
    assert settings.wrapper_right == ""
    assert settings.max_refs == 3


def test_settings_rejects_one_sided_wrapper():
    settings = normalize_trigger_settings(
        {"mode": "auto", "wrapper_left": "#", "max_refs": 5}
    )
    assert settings.mode == "auto"
    assert settings.wrapper_left == ""
    assert settings.wrapper_right == ""
    assert settings.max_refs == 5


def test_settings_clamps_max_refs():
    assert normalize_trigger_settings({"max_refs": 99}).max_refs == 10
    assert normalize_trigger_settings({"max_refs": -3}).max_refs == 0
    assert normalize_trigger_settings({"max_refs": "abc"}).max_refs == 3


def test_settings_invalid_mode_falls_back_to_command():
    assert normalize_trigger_settings({"mode": "wild"}).mode == "command"


# ---------- 绑定归一化 ----------


def test_binding_requires_words():
    assert normalize_binding({"name": "x", "words": []}, 0) is None
    assert normalize_binding({"words": ["  "]}, 0) is None
    assert normalize_binding("not-a-dict", 0) is None


def test_binding_filters_invalid_ref_ids_and_keeps_order():
    binding = normalize_binding(
        {
            "name": "画风",
            "words": ["水彩", "水彩", "彩铅"],
            "ref_ids": ["ref-aaaaaaaaaaaa", "bad-id", "ref-bbbbbbbbbbbb"],
            "prompt_append": " 转换为水彩风格 ",
            "enabled": "off",
        },
        0,
    )
    assert binding.words == ["水彩", "彩铅"]
    assert binding.ref_ids == ["ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb"]
    assert binding.prompt_append == "转换为水彩风格"
    assert binding.enabled is False


def test_binding_name_falls_back_to_order():
    binding = normalize_binding({"words": ["a"]}, 2)
    assert binding.name == "绑定3"


def test_normalize_bindings_skips_invalid_entries():
    bindings = normalize_bindings(
        [{"words": ["a"]}, {"name": "no-words"}, {"words": ["b"]}]
    )
    assert [binding.order for binding in bindings] == [0, 2]


# ---------- 匹配语义 ----------


def test_match_auto_substring_is_casefold():
    bindings = [_binding(0, words=("Ghibli", "水彩"))]
    settings = normalize_trigger_settings({"mode": "auto"})
    hits = match_auto("来一张 ghibli 风格的猫", bindings, settings)
    assert hits == [(0, ["Ghibli"])]
    assert match_auto("把图改成水彩", bindings, settings) == [(0, ["水彩"])]
    assert match_auto("普通消息", bindings, settings) == []


def test_match_auto_wrapper_requires_wrapped_form():
    bindings = [_binding(0, words=("水彩",))]
    settings = normalize_trigger_settings(
        {"mode": "auto", "wrapper_left": "#", "wrapper_right": "#"}
    )
    assert match_auto("把图改成水彩", bindings, settings) == []
    assert match_auto("把图改成 #水彩# 风格", bindings, settings) == [(0, ["水彩"])]


def test_match_auto_skips_disabled_bindings():
    bindings = [_binding(0, words=("水彩",), enabled=False)]
    settings = normalize_trigger_settings({"mode": "auto"})
    assert match_auto("改成水彩", bindings, settings) == []


def test_match_explicit_ignores_wrapper():
    bindings = [_binding(0, words=("水彩",))]
    settings = normalize_trigger_settings(
        {"mode": "command", "wrapper_left": "#", "wrapper_right": "#"}
    )
    assert settings.wrapper_left == "#"
    assert match_explicit("把图改成水彩", bindings) == [(0, ["水彩"])]
    assert match_explicit("", bindings) == []


def test_match_collects_all_matched_words_in_binding():
    bindings = [_binding(0, words=("水彩", "彩铅"))]
    settings = normalize_trigger_settings({"mode": "auto"})
    assert match_auto("水彩与彩铅", bindings, settings) == [(0, ["水彩", "彩铅"])]


# ---------- 注入计划 ----------


def test_plan_expansion_dedups_ref_across_bindings_and_keeps_order():
    bindings = [
        _binding(0, words=("水彩",), ref_ids=("ref-aaaaaaaaaaaa", "ref-cccccccccccc")),
        _binding(1, words=("彩铅",), ref_ids=("ref-bbbbbbbbbbbb", "ref-aaaaaaaaaaaa")),
    ]
    hits = [(0, ["水彩"]), (1, ["彩铅"])]
    plan = plan_expansion(
        hits,
        bindings,
        slots=10,
        resolve_ref=_resolve(
            {"ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb", "ref-cccccccccccc"}
        ),
    )
    assert [item["id"] for item in plan.refs] == [
        "ref-aaaaaaaaaaaa",
        "ref-cccccccccccc",
        "ref-bbbbbbbbbbbb",
    ]


def test_plan_expansion_drops_beyond_slots_and_records():
    bindings = [
        _binding(0, words=("水彩",), ref_ids=("ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb"))
    ]
    plan = plan_expansion(
        [(0, ["水彩"])],
        bindings,
        slots=1,
        resolve_ref=_resolve({"ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb"}),
    )
    assert [item["id"] for item in plan.refs] == ["ref-aaaaaaaaaaaa"]
    assert plan.dropped == [{"binding": "绑定1", "id": "ref-bbbbbbbbbbbb"}]


def test_plan_expansion_missing_refs_do_not_consume_slots():
    bindings = [
        _binding(0, words=("水彩",), ref_ids=("ref-missing00000", "ref-aaaaaaaaaaaa"))
    ]
    plan = plan_expansion(
        [(0, ["水彩"])], bindings, slots=1, resolve_ref=_resolve({"ref-aaaaaaaaaaaa"})
    )
    assert [item["id"] for item in plan.refs] == [
        "ref-missing00000",
        "ref-aaaaaaaaaaaa",
    ]
    assert plan.refs[0]["missing"] is True
    assert plan.refs[1]["missing"] is False


def test_plan_expansion_joins_prompt_append_in_hit_order():
    bindings = [
        _binding(0, words=("水彩",), append="转为水彩"),
        _binding(1, words=("人物",), append="保持人物一致"),
    ]
    plan = plan_expansion(
        [(1, ["人物"]), (0, ["水彩"])], bindings, slots=0, resolve_ref=_resolve(set())
    )
    assert plan.prompt_append == "保持人物一致\n转为水彩"


# ---------- 运行时服务 ----------


def test_service_mode_gates_auto_and_budget_caps_total():
    service = KeywordTriggerService(_resolve({"ref-aaaaaaaaaaaa"}))
    service.refresh(
        {
            "mode": "command",
            "max_refs": 1,
            "bindings": [
                {
                    "name": "画风",
                    "words": ["水彩"],
                    "ref_ids": ["ref-aaaaaaaaaaaa"],
                    "prompt_append": "转为水彩",
                }
            ],
        }
    )
    assert service.expand_auto("水彩", slots=5) is None
    expansion = service.expand_explicit("水彩", slots=5)
    assert expansion is not None
    assert service.expansion_paths(expansion) == [
        "/data/references/ref-aaaaaaaaaaaa.png"
    ]
    assert expansion.prompt_append == "转为水彩"


def test_service_budget_takes_min_of_slots_and_max_refs():
    service = KeywordTriggerService(_resolve({"ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb"}))
    service.refresh(
        {
            "mode": "command",
            "max_refs": 2,
            "bindings": [
                {
                    "words": ["水彩"],
                    "ref_ids": ["ref-aaaaaaaaaaaa", "ref-bbbbbbbbbbbb"],
                }
            ],
        }
    )
    expansion = service.expand_explicit("水彩", slots=1)
    assert [item["id"] for item in expansion.refs] == ["ref-aaaaaaaaaaaa"]
    assert expansion.dropped == [{"binding": "绑定1", "id": "ref-bbbbbbbbbbbb"}]


# ---------- 审查修复：长度、数量与总量上限 ----------


def test_word_length_limit_is_32_chars_not_16():
    long_word = "一" * 24
    binding = normalize_binding({"words": [long_word]}, 0)
    assert binding is not None
    assert binding.words == [long_word]


def test_word_count_capped_at_16():
    words = [f"词{index}" for index in range(17)]
    binding = normalize_binding({"words": words}, 0)
    assert binding is not None
    assert len(binding.words) == 16


def test_bindings_capped_at_32():
    bindings = normalize_bindings([{"words": [f"词{index}"]} for index in range(33)])
    assert len(bindings) == 32


def test_prompt_append_total_budget_drops_overflow_fragments():
    from tl.keyword_trigger import MAX_PROMPT_APPEND_TOTAL

    huge = "a" * (MAX_PROMPT_APPEND_TOTAL - 100)
    bindings = [
        _binding(0, words=("第一",), append=huge),
        _binding(1, words=("第二",), append="b" * 200),
    ]
    plan = plan_expansion(
        [(0, ["第一"]), (1, ["第二"])],
        bindings,
        slots=10,
        resolve_ref=_resolve(set()),
    )
    assert plan.prompt_append == huge
    assert plan.dropped_fragments == ["绑定2"]


def test_prompt_append_budget_counts_separators_not_first_line():
    """拼接总长恰等于预算时不能误丢：分隔符只算片段之间的。"""
    from tl.keyword_trigger import MAX_PROMPT_APPEND_TOTAL

    # 500+1+500+1+500+1+497 = 2000，恰达预算；旧实现首条多计 1 导致误丢
    remainder = MAX_PROMPT_APPEND_TOTAL - 3 * 500 - 3
    bindings = [
        _binding(0, words=("一",), append="a" * 500),
        _binding(1, words=("二",), append="b" * 500),
        _binding(2, words=("三",), append="c" * 500),
        _binding(3, words=("四",), append="d" * remainder),
    ]
    hits = [(0, ["一"]), (1, ["二"]), (2, ["三"]), (3, ["四"])]
    plan = plan_expansion(hits, bindings, slots=10, resolve_ref=_resolve(set()))
    assert len(plan.prompt_append) == MAX_PROMPT_APPEND_TOTAL
    assert plan.dropped_fragments == []
