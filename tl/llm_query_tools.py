"""只读查询和生成记录共享。这两个工具不扣额度，也不走生成路径。"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .core.errors import redact_text
from .generation_tracker import requester_from_event
from .internal_references import (
    InternalReferenceError,
    ReferenceResolver,
    parse_references,
)
from .llm_output import sanitize_task_result
from .provider_capabilities import candidate_capability, select_candidates

_KINDS = ("models", "templates", "history", "tasks")
_TASK_SUMMARY_KEYS = (
    "task_id",
    "kind",
    "status",
    "routing_mode",
    "message",
    "created_at",
    "updated_at",
    "total_items",
    "completed_items",
    "succeeded_items",
    "failed_items",
    "generated_images",
    "error",
)
_HISTORY_HINT = "多张图片时用 gen:<记录ID>#<序号> 选择其中一张，序号从 1 开始。"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _event(context: ContextWrapper[AstrAgentContext]) -> Any:
    return context.context.event


def _session_id(context: ContextWrapper[AstrAgentContext]) -> str:
    return str(getattr(_event(context), "unified_msg_origin", None) or "unknown")


def _single_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _limit(value: Any, default: int, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or value < 1:
        return default
    return min(value, maximum)


def _query(value: Any) -> str | dict[str, str]:
    if value is None:
        return ""
    if not isinstance(value, str):
        return {"error": "query 必须是字符串"}
    text = value.strip()
    if len(text) > 100:
        return {"error": "query 不能超过 100 字"}
    return text


@dataclass
class GeminiImageLookupTool(FunctionTool[AstrAgentContext]):
    name: str = "gemini_image_lookup"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = (
        "查询模型、提示词模板、可引用的生成记录或后台任务。"
        "kind=models 查看已配置的供应商、模型和参数，只有需要参数细节时才把 detail 设为 true。"
        "kind=templates 查看可用的提示词模板（id、名称、说明、默认尺寸、是否需要参考图）。"
        "kind=history 返回可引用的 gen: 标识。"
        "kind=tasks 查看当前会话的后台任务；任务完成后会自动通知，不需要反复查询。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_KINDS),
                    "description": "查询类别：models、templates、history 或 tasks",
                },
                "query": {
                    "type": "string",
                    "description": "可选过滤文本，不超过 100 字；tasks 给出任务号时查询单条",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，非法或超界时使用默认值或上限",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否返回补充细节",
                    "default": False,
                },
            },
            "required": ["kind"],
        }
    )
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        kind = kwargs.get("kind")
        if kind not in _KINDS:
            return _json(
                {"error": "kind 必须是 models、templates、history、tasks 之一"}
            )
        query = _query(kwargs.get("query"))
        if isinstance(query, dict):
            return _json(query)
        detail = kwargs.get("detail") is True
        if kind == "models":
            result = self._models(query, _limit(kwargs.get("limit"), 20, 50), detail)
        elif kind == "templates":
            result = self._templates(query, _limit(kwargs.get("limit"), 10, 20), detail)
        elif kind == "history":
            result = self._history(
                _event(context), query, _limit(kwargs.get("limit"), 5, 20)
            )
        else:
            result = await self._tasks(
                context, query, _limit(kwargs.get("limit"), 10, 20)
            )
        logger.debug(
            f"[gemini_image_lookup] tool={kind}, umo={_session_id(context)}, "
            f"query_len={len(query)}, hits={result.get('total', len(result.get('history', result.get('tasks', result.get('models', result.get('templates', [])))))) if isinstance(result, dict) else 0}"
        )
        return _json(result)

    def _models(self, query: str, limit: int, detail: bool) -> dict[str, Any]:
        candidates = list(getattr(self.plugin.cfg, "provider_candidates", []) or [])
        needle = query.casefold()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in select_candidates(candidates):
            capability = candidate_capability(candidate)
            row: dict[str, Any] = {
                "provider": str(getattr(candidate, "api_type", "") or ""),
                "model": str(getattr(candidate, "model", "") or ""),
                "alias": getattr(candidate, "model_alias", None),
                "generation_modes": list(capability.get("generation_modes") or []),
            }
            if needle and not any(
                needle in str(row[key] or "").casefold()
                for key in ("provider", "model", "alias")
            ):
                continue
            if detail:
                parameters = json.loads(
                    json.dumps(capability.get("parameters") or {}, ensure_ascii=False)
                )
                image_count = parameters.get("image_count")
                if isinstance(image_count, dict):
                    image_count["maximum"] = int(
                        getattr(self.plugin.cfg, "batch_max_images_per_task", 10)
                    )
                    image_count["maximum_scope"] = "batch_task_item"
                    image_count["native_request_maximum_scope"] = "provider_request"
                    image_count["description"] = (
                        "批量任务中单个命名项目标图片数量。maximum 是批量项目标上限，"
                        "插件可能拆分为多次请求补齐；native_request_maximum 是供应商"
                        "单次上游请求的原生上限。"
                    )
                row["parameters"] = parameters
            signature = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            rows.append(row)
        result: dict[str, Any] = {
            "models": rows[:limit],
            "total": len(rows),
            "truncated": len(rows) > limit,
        }
        if not rows:
            result["message"] = "没有匹配的已配置供应商或模型"
        return result

    def _templates(self, query: str, limit: int, detail: bool) -> dict[str, Any]:
        store = getattr(self.plugin, "prompt_templates", None)
        if store is None:
            return {"error": "模板库不可用"}
        words = [word.casefold() for word in query.split()]
        ranked: list[tuple[int, int, Any]] = []
        for index, template in enumerate(store.list_templates()):
            if not template.ai_enabled:
                continue
            rank = _template_rank(template, words)
            if rank is None:
                continue
            ranked.append((rank, index, template))
        ranked.sort(key=lambda item: (item[0], item[1]))
        items = [_template_item(template, detail) for _, _, template in ranked]
        result: dict[str, Any] = {
            "templates": items[:limit],
            "total": len(items),
            "truncated": len(items) > limit,
        }
        return result

    def _history(self, event: Any, query: str, limit: int) -> dict[str, Any]:
        tracker = getattr(self.plugin, "generation_tracker", None)
        if tracker is None or not getattr(tracker, "enabled", False):
            return {"error": "生成历史未开启"}
        requester = requester_from_event(event)
        umo = requester.get("umo") or ""
        user_id = requester.get("user_id") or ""
        if not umo:
            return {"error": "当前会话无法识别，不能查询生成历史"}
        if not user_id:
            return {"error": "当前用户无法识别，不能查询生成历史"}
        needle = query.casefold()
        matched: list[tuple[str, dict[str, Any], int]] = []
        seen: set[str] = set()
        for record in tracker.records_snapshot():
            owner = record.get("requester") or {}
            record_umo = str(owner.get("umo") or "")
            record_user = str(owner.get("user_id") or "")
            images = [str(item) for item in record.get("images") or [] if item]
            if (
                not record_umo
                or record.get("status") not in {"succeeded", "partial_success"}
                or not images
            ):
                continue
            own = record_user == user_id
            if record_umo != umo and not (own and record.get("shared") is True):
                continue
            prompt = str(record.get("prompt") or "")
            if needle and needle not in prompt.casefold():
                continue
            job_id = str(record.get("job_id") or "")
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            item = {
                "ref": f"gen:{job_id}",
                "images": len(images),
                "prompt": _single_line(prompt, 60),
                "created_at": str(record.get("created_at") or ""),
            }
            if own:
                item["shared"] = record.get("shared") is True
            template_id = str((record.get("params") or {}).get("template_id") or "")
            if template_id:
                item["template_id"] = template_id
            matched.append((str(record.get("created_at") or ""), item, len(images)))
        matched.sort(key=lambda item: item[0], reverse=True)
        items = [item for _, item, _count in matched[:limit]]
        result: dict[str, Any] = {
            "history": items,
            "total": len(matched),
            "truncated": len(matched) > limit,
        }
        if any(count > 1 for _, _item, count in matched[:limit]):
            result["hint"] = _HISTORY_HINT
        return result

    async def _tasks(
        self, context: ContextWrapper[AstrAgentContext], query: str, limit: int
    ) -> dict[str, Any]:
        manager = self.plugin.background_task_manager
        session_id = _session_id(context)
        if query:
            try:
                record = await manager.get(query, session_id)
            except PermissionError:
                return {"error": "任务不存在或不属于当前会话"}
            if record is None:
                return {"error": "任务不存在或已过期"}
            record.pop("session_id", None)
            return sanitize_task_result(record)
        records = await manager.list_for_session(session_id, 21)
        items = [_task_summary(record) for record in records[:20]]
        shown = items[:limit]
        return {
            "tasks": sanitize_task_result(shown),
            "total": len(items) if len(records) <= 20 else len(records),
            "truncated": len(items) > limit or len(records) > 20,
        }


@dataclass
class GeminiImageShareTool(FunctionTool[AstrAgentContext]):
    name: str = "gemini_image_share"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = (
        "把自己生成的图片设为可跨会话使用，或取消共享。"
        "只有用户明确表示要把某张图用在别的群或私聊之后才能调用；取消共享同样要等用户明确要求。"
        "只能操作自己生成的图。共享以整条生成记录为单位，引用里的序号会被忽略。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "references": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {"type": "string"},
                    "description": "只接受 gen: 引用，1 到 8 项",
                },
                "shared": {
                    "type": "boolean",
                    "description": "true 设为共享，false 取消共享",
                },
            },
            "required": ["references", "shared"],
        }
    )
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        umo = _session_id(context)
        logger.debug(
            f"[gemini_image_share] tool=share, umo={umo}, "
            f"references={len(kwargs.get('references') or [])}"
        )
        tracker = getattr(self.plugin, "generation_tracker", None)
        if tracker is None or not getattr(tracker, "enabled", False):
            return _json({"error": "生成历史未开启"})
        requester = requester_from_event(_event(context))
        user_id = requester.get("user_id") or ""
        umo = requester.get("umo") or ""
        if not user_id or not umo:
            return _json({"error": "当前会话或用户无法识别，不能修改共享状态"})
        shared = kwargs.get("shared")
        references = kwargs.get("references")
        if type(shared) is not bool:
            return _json({"error": "shared 必须是布尔值"})
        if (
            not isinstance(references, list)
            or not 1 <= len(references) <= 8
            or any(not isinstance(item, str) for item in references)
        ):
            return _json({"error": "references 必须是 1 到 8 项的字符串数组"})
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in references:
            job_id, error = await self._job_id(raw, _event(context))
            if error is not None:
                results.append({"ref": raw.strip(), "error": error})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            ref = f"gen:{job_id}"
            try:
                record = await tracker.set_shared(
                    job_id, shared, user_id=user_id, umo=umo
                )
            except ValueError as exc:
                logger.warning(
                    f"[gemini_image_share] share failed: task_id={job_id}, umo={umo}, error={redact_text(str(exc))[:240]}"
                )
                results.append({"ref": ref, "error": _share_error(exc)})
                continue
            results.append(
                {"ref": ref, "shared": shared, "shared_at": record.get("shared_at")}
            )
        logger.info(
            f"[gemini_image_share] share completed: umo={umo}, success={sum('error' not in item for item in results)}, failed={sum('error' in item for item in results)}"
        )
        return _json({"results": results})

    async def _job_id(self, raw: str, event: Any) -> tuple[str | None, str | None]:
        try:
            specs = parse_references([raw])
        except InternalReferenceError as exc:
            return None, exc.message
        spec = specs[0]
        if spec.kind not in {"gen_last", "gen_job"}:
            return None, "只接受 gen: 引用"
        if spec.kind == "gen_job":
            return spec.job_id, None
        tracker = self.plugin.generation_tracker
        resolver = ReferenceResolver(
            image_handler=None,
            avatar_handler=None,
            generation_tracker=tracker,
            settings=lambda: {
                "cross_session": bool(
                    getattr(self.plugin.cfg, "reference_cross_session", False)
                ),
                "history_enabled": bool(getattr(tracker, "enabled", False)),
                "max_reference_images": 8,
                "allowed_dirs": [],
                "global_paths": False,
            },
        )
        try:
            resolved = await resolver.resolve([spec], event)
        except InternalReferenceError as exc:
            return None, exc.message
        name = resolved.gallery_names[0]
        for record in tracker.records_snapshot():
            if name in [str(item) for item in record.get("images") or []]:
                return str(record.get("job_id") or ""), None
        return None, "本会话没有可引用的生成"


def _share_error(exc: ValueError) -> str:
    if str(exc) in {"生成记录不存在", "只有请求者本人可以修改共享状态"}:
        return "找不到可共享的生成记录，或者这不是你生成的图"
    return str(exc)


def _template_rank(template: Any, words: list[str]) -> int | None:
    if not words:
        return 0
    name = template.name.casefold()
    tags = " ".join(template.tags).casefold()
    description = template.ai_description.casefold()
    if not all(word in name or word in tags or word in description for word in words):
        return None
    if any(word in name for word in words):
        return 0
    if any(word in tags for word in words):
        return 1
    return 2


def _template_item(template: Any, detail: bool) -> dict[str, Any]:
    item = {
        "id": template.id,
        "name": _single_line(template.name, 40),
        "description": _single_line(template.ai_description, 80),
        "reference": template.reference,
    }
    if template.defaults:
        item["defaults"] = dict(template.defaults)
    if detail:
        item["preview"] = (
            template.content.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", " ")[:200]
        )
    return item


def _task_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _TASK_SUMMARY_KEYS if key in record}
