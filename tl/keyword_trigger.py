"""关键词触发：按配置绑定把参考图与提示词片段注入生成请求。

匹配语义（auto 与 command 两种模式共用）：
- 字面子串匹配，ASCII 按 casefold 忽略大小写，不做词边界
- auto 模式可配置包裹符（左右同空或同有值），有包裹时仅匹配 左符+词+右符
- command 模式（/快速 关键词 子指令）忽略包裹符
- 匹配对象是用户原始文本，不是模板渲染产物
- 单绑定多词命中只注入一次；多绑定按配置顺序稳定排列；ref_id 跨绑定去重
- max_refs 是单次请求自动注入的总上限，超限丢弃并如实上报

绑定与设置存放在插件配置 keyword_trigger 下（template_list + object），
本模块只做归一化与匹配，不落盘、不依赖参考图库。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

REF_ID = re.compile(r"^ref-[0-9a-f]{12}$")
_MAX_NAME = 64
_MAX_WORD = 32
_MAX_WORDS = 16
_MAX_PROMPT_APPEND = 500
_MAX_WRAPPER_CHARS = 2
_MAX_MAX_REFS = 10
MAX_BINDINGS = 32
MAX_PROMPT_APPEND_TOTAL = 2000
_FALSE_TEXT = frozenset({"false", "0", "no", "off"})
_MODES = frozenset({"command", "auto"})


@dataclass
class TriggerSettings:
    mode: str = "command"
    wrapper_left: str = ""
    wrapper_right: str = ""
    max_refs: int = 3


@dataclass
class KeywordBinding:
    order: int
    name: str
    words: list[str]
    ref_ids: list[str]
    prompt_append: str
    enabled: bool


@dataclass
class Expansion:
    hits: list[dict[str, Any]] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    prompt_append: str = ""
    dropped: list[dict[str, Any]] = field(default_factory=list)
    dropped_fragments: list[str] = field(default_factory=list)


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _bool(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TEXT
    return bool(value)


def _string_list(value: Any, *, item_limit: int, count_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value[:32]:
        text = str(item or "").strip()[:item_limit]
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= count_limit:
            break
    return cleaned


def normalize_trigger_settings(raw: Any) -> TriggerSettings:
    """归一化触发设置；非法值回退默认并保持包裹符左右同空或同有值。"""
    source = raw if isinstance(raw, dict) else {}
    mode = str(source.get("mode") or "").strip().lower()
    if mode not in _MODES:
        mode = "command"
    left = _text(source.get("wrapper_left"), _MAX_WRAPPER_CHARS)
    right = _text(source.get("wrapper_right"), _MAX_WRAPPER_CHARS)
    if bool(left) != bool(right):
        logger.warning("[关键词] 包裹符必须左右同空或同有值，已忽略当前配置")
        left = right = ""
    try:
        max_refs = int(source.get("max_refs", 3))
    except (TypeError, ValueError):
        max_refs = 3
    if not 0 <= max_refs <= _MAX_MAX_REFS:
        max_refs = max(min(max_refs, _MAX_MAX_REFS), 0)
    return TriggerSettings(
        mode=mode,
        wrapper_left=left,
        wrapper_right=right,
        max_refs=max_refs,
    )


def normalize_binding(raw: Any, order: int) -> KeywordBinding | None:
    """归一化单条绑定；缺关键词或全部字段无效时返回 None。"""
    if not isinstance(raw, dict):
        return None
    words = _string_list(raw.get("words"), item_limit=_MAX_WORD, count_limit=_MAX_WORDS)
    if not words:
        return None
    ref_ids = [
        item
        for item in _string_list(
            raw.get("ref_ids"), item_limit=_MAX_WORD, count_limit=_MAX_WORDS
        )
        if REF_ID.fullmatch(item)
    ]
    return KeywordBinding(
        order=order,
        name=_text(raw.get("name"), _MAX_NAME) or f"绑定{order + 1}",
        words=words,
        ref_ids=ref_ids,
        prompt_append=_text(raw.get("prompt_append"), _MAX_PROMPT_APPEND),
        enabled=_bool(raw.get("enabled", True)),
    )


def normalize_bindings(raw: Any) -> list[KeywordBinding]:
    """按配置顺序归一化绑定列表；无效条目跳过并告警。"""
    if not isinstance(raw, list):
        return []
    bindings: list[KeywordBinding] = []
    for order, item in enumerate(raw[:MAX_BINDINGS]):
        binding = normalize_binding(item, order)
        if binding is None:
            logger.warning(f"[关键词] 第 {order + 1} 条绑定缺少触发关键词，已跳过")
            continue
        bindings.append(binding)
    return bindings


def _fold(text: str) -> str:
    return text.casefold()


def match_auto(
    text: str, bindings: list[KeywordBinding], settings: TriggerSettings
) -> list[tuple[int, list[str]]]:
    """auto 模式匹配：有包裹符时仅识别包裹形态，否则子串。"""
    haystack = _fold(str(text or ""))
    if not haystack:
        return []
    wrapped = bool(settings.wrapper_left and settings.wrapper_right)
    hits: list[tuple[int, list[str]]] = []
    for binding in bindings:
        if not binding.enabled:
            continue
        matched: list[str] = []
        for word in binding.words:
            needle = (
                _fold(f"{settings.wrapper_left}{word}{settings.wrapper_right}")
                if wrapped
                else _fold(word)
            )
            if needle and needle in haystack:
                matched.append(word)
        if matched:
            hits.append((binding.order, matched))
    return hits


def match_explicit(
    text: str, bindings: list[KeywordBinding]
) -> list[tuple[int, list[str]]]:
    """command 模式匹配：显式子指令即意图，子串命中且忽略包裹符。"""
    haystack = _fold(str(text or ""))
    if not haystack:
        return []
    hits: list[tuple[int, list[str]]] = []
    for binding in bindings:
        if not binding.enabled:
            continue
        matched = [word for word in binding.words if _fold(word) in haystack]
        if matched:
            hits.append((binding.order, matched))
    return hits


def plan_expansion(
    hits: list[tuple[int, list[str]]],
    bindings: list[KeywordBinding],
    slots: int,
    resolve_ref: Callable[[str], dict[str, Any] | None],
) -> Expansion:
    """把命中绑定展开为注入计划：去重、稳定顺序、槽位上限与丢弃记录。"""
    by_order = {binding.order: binding for binding in bindings}
    expansion = Expansion()
    seen_refs: set[str] = set()
    for order, matched_words in hits:
        binding = by_order.get(order)
        if binding is None:
            continue
        expansion.hits.append(
            {
                "name": binding.name,
                "words": matched_words,
                "prompt_append": binding.prompt_append,
            }
        )
        for ref_id in binding.ref_ids:
            if ref_id in seen_refs:
                continue
            seen_refs.add(ref_id)
            resolved = resolve_ref(ref_id)
            if resolved is None:
                expansion.refs.append(
                    {"id": ref_id, "label": "", "missing": True, "path": None}
                )
                continue
            if slots <= 0:
                expansion.dropped.append({"binding": binding.name, "id": ref_id})
                continue
            slots -= 1
            expansion.refs.append(
                {
                    "id": ref_id,
                    "label": resolved.get("label") or ref_id,
                    "missing": False,
                    "path": resolved.get("path"),
                }
            )
    fragments: list[str] = []
    used = 0
    for hit in expansion.hits:
        fragment = str(hit["prompt_append"])
        if not fragment:
            continue
        # 累计片段按稳定顺序受总量预算约束，超限的不拼接并如实记录
        separator = 1 if fragments else 0
        if used + len(fragment) + separator > MAX_PROMPT_APPEND_TOTAL:
            expansion.dropped_fragments.append(str(hit["name"]))
            continue
        fragments.append(fragment)
        used += len(fragment) + separator
    expansion.prompt_append = "\n".join(fragments)
    return expansion


class KeywordTriggerService:
    """运行时触发器：持有归一化后的配置，供命令、工具与预演共用。"""

    def __init__(self, resolve_ref: Callable[[str], dict[str, Any] | None]) -> None:
        self._resolve_ref = resolve_ref
        self._settings = TriggerSettings()
        self._bindings: list[KeywordBinding] = []

    def refresh(self, raw_trigger: Any) -> None:
        """从配置原始值重建运行时状态；保存后即时调用。"""
        source = raw_trigger if isinstance(raw_trigger, dict) else {}
        self._settings = normalize_trigger_settings(source)
        self._bindings = normalize_bindings(source.get("bindings"))

    @property
    def mode(self) -> str:
        return self._settings.mode

    def enabled_bindings(self) -> list[KeywordBinding]:
        return [binding for binding in self._bindings if binding.enabled]

    def _expand(self, hits: list[int], slots: int) -> Expansion:
        budget = max(min(slots, self._settings.max_refs), 0)
        return plan_expansion(hits, self._bindings, budget, self._resolve_ref)

    def expand_auto(self, prompt_text: str, slots: int) -> Expansion | None:
        """auto 模式：未启用或无命中返回 None。"""
        if self._settings.mode != "auto":
            return None
        hits = match_auto(prompt_text, self._bindings, self._settings)
        if not hits:
            return None
        return self._expand(hits, slots)

    def expand_explicit(self, prompt_text: str, slots: int) -> Expansion | None:
        """command 模式（子指令/预演）：无命中返回 None。"""
        hits = match_explicit(prompt_text, self._bindings)
        if not hits:
            return None
        return self._expand(hits, slots)

    def expansion_paths(self, expansion: Expansion) -> list[str]:
        """注入用的本地路径列表；缺失条目不产生路径。"""
        return [
            str(item["path"])
            for item in expansion.refs
            if not item.get("missing") and item.get("path")
        ]
