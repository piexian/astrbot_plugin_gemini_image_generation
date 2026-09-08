"""在生成空闲窗口准备、应用或回滚供应商运行时。"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .plugin_config import ConfigLoader
from .provider_capabilities import candidate_capability
from .provider_metadata import get_provider_spec, iter_provider_specs
from .tl_api import get_api_client
from .web_studio_service import StudioServiceError

_PROVIDER_FIELDS = (
    "vision_provider_id",
    "vision_model",
    "proxy",
    "provider_candidates",
    "provider_candidates_all",
    "provider_polling",
    "provider_overrides",
    "provider_settings_by_type",
    "provider_config_errors",
)
_CLIENT_FIELDS = (
    "api_keys",
    "current_key_index",
    "provider_candidates",
    "provider_candidates_all",
    "_key_manager",
    "_candidate_key_pools",
    "_candidate_key_indices",
    "_candidate_semaphores",
    "proxy",
    "_default_proxy",
    "provider_runtime",
)


@dataclass
class PreparedProviderRuntime:
    fields: dict[str, Any]
    keys: Any
    tool: tuple[str, dict] | None
    previous_fields: dict[str, Any]
    previous_keys: Any
    previous_client: Any
    previous_client_fields: dict[str, Any]
    previous_tool: tuple[str, dict] | None


class ProviderApplication:
    def __init__(self, plugin):
        self.plugin = plugin

    async def prepare(self, settings: dict[str, Any]) -> PreparedProviderRuntime:
        from .llm_tools import _build_tool_description, _build_tool_parameters

        plugin = self.plugin
        # 验证所有启用条目，而非只检查显式轮询表里的候选。
        expected = 0
        for entry in settings.get("provider_overrides", []):
            if not isinstance(entry, dict):
                continue
            enabled = entry.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"false", "0", "no", "off"}
            if enabled is None:
                enabled = True
            if enabled and get_provider_spec(entry.get("__template_key", "")):
                expected += 1
        validation = copy.deepcopy(plugin.cfg)
        validation.provider_config_errors = []
        try:
            ConfigLoader(
                {"provider_settings": {**settings, "provider_polling": []}}
            )._parse_provider_settings(validation)
            if len(validation.provider_candidates) != expected:
                raise StudioServiceError(
                    "启用的供应商配置缺少有效模型或 API Key，请检查配置表"
                )
            for candidate in validation.provider_candidates_all:
                candidate_capability(candidate)
            config = copy.deepcopy(plugin.cfg)
            config.provider_config_errors = []
            ConfigLoader({"provider_settings": settings})._parse_provider_settings(
                config
            )
            shadow = SimpleNamespace(cfg=config)
            tool = (
                (_build_tool_description(shadow), _build_tool_parameters(shadow))
                if plugin.llm_image_tool
                else None
            )
        except StudioServiceError:
            raise
        except Exception as exc:
            raise StudioServiceError(
                "供应商参数校验失败，请检查模型及相关配置字段"
            ) from exc
        names = set(_PROVIDER_FIELDS)
        names.update(
            spec.settings_attr for spec in iter_provider_specs() if spec.settings_attr
        )
        fields = {name: getattr(config, name) for name in names}
        previous = {name: getattr(plugin.cfg, name) for name in names}
        try:
            keys = await plugin.key_manager.clone_for_config(config)
        except Exception:
            raise StudioServiceError(
                "无法安全读取或保存 Key 用量，请待存储恢复后重试", status_code=503
            ) from None
        client = plugin.api_client
        previous_client_fields = (
            {name: getattr(client, name, None) for name in _CLIENT_FIELDS}
            if client
            else {}
        )
        prepared = PreparedProviderRuntime(
            fields=fields,
            keys=keys,
            tool=tool,
            previous_fields=previous,
            previous_keys=plugin.key_manager,
            previous_client=client,
            previous_client_fields=previous_client_fields,
            previous_tool=(
                plugin.llm_image_tool.description,
                plugin.llm_image_tool.parameters,
            )
            if plugin.llm_image_tool
            else None,
        )
        # 此时更新门已封住新生成且所有旧任务结束；持久化失败时旧配置可重建连接。
        if client:
            await client.close()
        return prepared

    def _bind(self, tool: tuple[str, dict] | None) -> None:
        plugin = self.plugin
        plugin._update_modules_api_client()
        if plugin.api_client is None:
            for module in (
                plugin.image_handler,
                plugin.image_generator,
                plugin.vision_handler,
                plugin.web_studio_service,
            ):
                module.api_client = None
        plugin.vision_handler.update_config(
            vision_provider_id=plugin.cfg.vision_provider_id,
            vision_model=plugin.cfg.vision_model,
        )
        if tool is not None and plugin.llm_image_tool is not None:
            plugin.llm_image_tool.description, plugin.llm_image_tool.parameters = tool

    def apply(self, prepared: PreparedProviderRuntime) -> None:
        plugin = self.plugin
        for name, value in prepared.fields.items():
            setattr(plugin.cfg, name, value)
        plugin.key_manager = prepared.keys
        plugin.key_manager.config = plugin.cfg
        keys = [
            key
            for candidate in plugin.cfg.provider_candidates
            for key in candidate.api_keys
        ]
        client = plugin.api_client or get_api_client(keys)
        plugin.api_client = client
        client.provider_runtime = plugin.provider_runtime
        client.generation_scheduler = getattr(plugin, "generation_scheduler", None)
        client.api_keys = keys
        client.current_key_index = 0
        client._candidate_key_indices = {}
        client._candidate_semaphores = {}
        client.set_provider_candidates(
            plugin.cfg.provider_candidates,
            getattr(plugin.cfg, "provider_candidates_all", None),
        )
        client.set_key_manager(plugin.key_manager)
        client.proxy = (
            plugin.cfg.proxy
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
        )
        client._default_proxy = client.proxy
        self._bind(prepared.tool)

    def restore(self, prepared: PreparedProviderRuntime) -> None:
        plugin = self.plugin
        for name, value in prepared.previous_fields.items():
            setattr(plugin.cfg, name, value)
        plugin.key_manager = prepared.previous_keys
        plugin.api_client = prepared.previous_client
        if plugin.api_client is not None:
            for name, value in prepared.previous_client_fields.items():
                setattr(plugin.api_client, name, value)
        self._bind(prepared.previous_tool)
