"""只投影本体现有 LLM 配置的选择信息，不导出连接凭据。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .web_studio_service import StudioServiceError


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class VisionProviderDirectory:
    def __init__(self, context):
        self.context = context

    def snapshot(self) -> dict[str, Any]:
        manager = getattr(self.context, "provider_manager", None)
        rows = getattr(manager, "providers_config", None)
        sources = getattr(manager, "provider_sources_config", [])
        try:
            if not isinstance(rows, list):
                config = self.context.get_config()
                rows = config.get("provider", [])
                sources = config.get("provider_sources", [])
            if not isinstance(rows, list) or not isinstance(sources, list):
                raise ValueError("Invalid host provider configuration")
        except Exception:
            logger.warning("[供应商] 无法读取本体 LLM 配置列表")
            return {
                "vision_providers": [],
                "vision_providers_available": False,
                "vision_providers_warning": "无法读取本体提供商配置，可刷新重试或手动填写 ID",
            }
        source_map = {
            row["id"]: row
            for row in sources
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        loaded = set()
        instances = getattr(manager, "inst_map", None)
        if isinstance(instances, dict):
            loaded = {key for key, value in instances.items() if value is not None}
        else:
            getter = getattr(self.context, "get_all_providers", None)
            if callable(getter):
                try:
                    for instance in getter():
                        try:
                            loaded.add(instance.meta().id)
                        except Exception:
                            continue
                except Exception:
                    pass
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get("enable") is False:
                continue
            provider_id = _text(row.get("id"))
            source_id = _text(row.get("provider_source_id"))
            source = source_map.get(source_id, {})
            kind = row.get("provider_type")
            if not kind and source_id:
                kind = source.get("provider_type", "chat_completion")
            if kind != "chat_completion" or not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            model = _text(row.get("model", source.get("model")))
            label = provider_id
            if source_id:
                label += f"（{source_id}）"
            result.append(
                {
                    "id": provider_id,
                    "label": label,
                    "model": model,
                    "source_id": source_id,
                    "available": provider_id in loaded,
                }
            )
        return {
            "vision_providers": result,
            "vision_providers_available": True,
            "vision_providers_warning": ""
            if result
            else "本体暂无已启用的 LLM 提供商配置",
        }

    def get_instance(self, provider_id: Any):
        snapshot = self.snapshot()
        if not snapshot["vision_providers_available"]:
            raise StudioServiceError(
                "无法读取本体提供商配置，请稍后重试", status_code=503
            )
        if not isinstance(provider_id, str) or provider_id not in {
            row["id"] for row in snapshot["vision_providers"]
        }:
            raise StudioServiceError("请先选择本体已启用的 LLM 提供商")
        getter = getattr(self.context, "get_provider_by_id", None)
        provider = getter(provider_id) if callable(getter) else None
        if provider is None or not callable(getattr(provider, "get_models", None)):
            raise StudioServiceError(
                "所选视觉提供商尚未加载或不能拉取模型，请先在本体检查配置",
                status_code=503,
            )
        return provider
