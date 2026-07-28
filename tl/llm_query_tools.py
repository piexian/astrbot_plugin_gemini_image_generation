"""Read-only LLM tools for provider capabilities and background tasks."""

from __future__ import annotations

import json
from typing import Any

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .provider_capabilities import candidate_capability, select_candidates


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _session_id(context: ContextWrapper[AstrAgentContext]) -> str:
    event = context.context.event
    return str(getattr(event, "unified_msg_origin", None) or "unknown")


@dataclass
class ProviderModelQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "gemini_image_provider_models"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = (
        "查询当前已配置的图片供应商、原始模型、模型别名和生成模式。"
        "只有需要了解模型可配置参数时才设置 detail=true。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "可选，按供应商名称精确过滤",
                },
                "model": {
                    "type": "string",
                    "description": "可选，按原始模型或别名精确过滤",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否返回该模型在生图工具中支持的可配置参数",
                    "default": False,
                },
            },
        }
    )
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        del context
        candidates = list(getattr(self.plugin.cfg, "provider_candidates", []) or [])
        selected = select_candidates(
            candidates,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
        )
        detail = bool(kwargs.get("detail", False))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in selected:
            capability = candidate_capability(candidate)
            row: dict[str, Any] = {
                "provider": str(getattr(candidate, "api_type", "") or ""),
                "model": str(getattr(candidate, "model", "") or ""),
                "alias": getattr(candidate, "model_alias", None),
                "generation_modes": list(capability.get("generation_modes") or []),
            }
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
        result: dict[str, Any] = {"models": rows}
        if not rows:
            result["message"] = "没有匹配的已配置供应商或模型"
        return _json(result)


@dataclass
class BackgroundTaskStatusTool(FunctionTool[AstrAgentContext]):
    name: str = "gemini_image_task_status"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = "使用后台任务号查询当前会话中的图片生成进度和结果摘要。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "生图工具返回的后台任务号",
                }
            },
            "required": ["task_id"],
        }
    )
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return _json({"error": "缺少 task_id"})
        try:
            record = await self.plugin.background_task_manager.get(
                task_id,
                _session_id(context),
            )
        except PermissionError:
            return _json({"error": "任务不存在或不属于当前会话"})
        if record is None:
            return _json({"error": "任务不存在或已过期"})
        record.pop("session_id", None)
        return _json(record)
