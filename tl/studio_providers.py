"""Studio 供应商原始配置的安全视图、校验与事务保存。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger

from .api_normalize import normalize_api_type
from .model_catalog import (
    ModelCatalogError,
    ModelCatalogService,
    catalog_capabilities,
    make_catalog_request,
    safe_target,
)
from .plugin_config import _clean_api_keys
from .provider_hooks import materialize_service_account_json
from .provider_runtime import ProviderRuntimeBusy
from .studio_vision_providers import VisionProviderDirectory
from .web_studio_service import StudioServiceError

_COMMON_FIELDS = ("proxy", "vision_provider_id", "vision_model")
_SECRET_FIELDS = frozenset({"api_keys", "api_base", "proxy"})
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "hint",
        "default",
        "options",
        "slider",
        "condition",
        "file_types",
    }
)
_MAX_STRING = 16384


def _invalid(location: str) -> StudioServiceError:
    # location 只由服务端字段名和条目位置构造，不能拼接输入值或未知字段名。
    return StudioServiceError(f"{location} 格式或取值无效")


def _conflict() -> StudioServiceError:
    return StudioServiceError(
        "供应商配置已更新，请重新加载后再保存",
        status_code=409,
        data={"reason": "revision"},
    )


def _protected_url(value: Any) -> bool:
    if not isinstance(value, str):
        return bool(value)
    if "?" in value or "#" in value:
        return True
    try:
        address = value.strip()
        parsed = urlsplit(
            address if "://" in address or address.startswith("//") else "//" + address
        )
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        # 无法解析的旧 URL 不应因解析失败降级成明文展示。
        return bool(value)


def _valid_type(value: Any, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        try:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        except OverflowError:
            return False
    if kind in ("string", "text"):
        return isinstance(value, str) and len(value) <= _MAX_STRING
    if kind == "list":
        return isinstance(value, list) and all(
            isinstance(item, str) and len(item) <= _MAX_STRING for item in value
        )
    # file 类型（如 vertex 服务账号凭证）值为路径列表，仅透传展示，不允许在 WebUI 改写
    if kind == "file":
        return isinstance(value, list) and all(
            isinstance(item, str) and len(item) <= _MAX_STRING for item in value
        )
    return False


class ProviderConfigService:
    def __init__(
        self,
        raw_config,
        context,
        *,
        config_lock,
        runtime,
        prepare,
        apply,
        restore,
    ):
        self.raw_config = raw_config
        self.context = context
        self.config_lock = config_lock
        self.runtime = runtime
        self.prepare = prepare
        self.apply = apply
        self.restore = restore
        self.catalog = ModelCatalogService()
        self.vision_directory = VisionProviderDirectory(context)
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))[
            "provider_settings"
        ]["items"]
        self.templates = {
            name: {
                "label": template.get("description", name),
                "fields": {
                    key: {
                        k: copy.deepcopy(v)
                        for k, v in field.items()
                        if k in _SCHEMA_KEYS
                    }
                    for key, field in template["items"].items()
                },
            }
            for name, template in schema["provider_overrides"]["templates"].items()
        }
        self.common_fields = {
            key: {
                k: copy.deepcopy(v) for k, v in schema[key].items() if k in _SCHEMA_KEYS
            }
            for key in _COMMON_FIELDS
        }
        self.polling_options = frozenset(schema["provider_polling"]["options"])

    def _settings(self) -> dict[str, Any]:
        settings = self.raw_config.get("provider_settings", {})
        if not isinstance(settings, dict) or not isinstance(
            settings.get("provider_overrides", []), list
        ):
            raise StudioServiceError(
                "供应商配置表格式无效，请在插件配置中修正后重载",
                status_code=409,
                data={"reason": "revision"},
            )
        return settings

    def _revision(self) -> str:
        data = json.dumps(self._settings(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _entry_id(revision: str, index: int) -> str:
        return hashlib.sha256(f"{revision}:{index}".encode()).hexdigest()

    @staticmethod
    def _api_type(entry: Any) -> str:
        value = entry.get("__template_key", "") if isinstance(entry, dict) else ""
        return normalize_api_type(value) if isinstance(value, str) else ""

    @staticmethod
    def _safe_fields(raw: dict, fields: dict) -> dict[str, dict]:
        values, secrets = {}, {}
        for key, field in fields.items():
            if key not in raw and key != "api_keys":
                continue
            value = raw.get(key, field.get("default"))
            if isinstance(value, str) and field["type"] in {"bool", "int", "float"}:
                try:
                    if field["type"] == "bool":
                        token = value.strip().lower()
                        if token in {
                            "true",
                            "1",
                            "yes",
                            "on",
                            "false",
                            "0",
                            "no",
                            "off",
                        }:
                            value = token in {"true", "1", "yes", "on"}
                    else:
                        value = int(value) if field["type"] == "int" else float(value)
                except (ValueError, OverflowError):
                    pass
            if key == "api_keys":
                keys = _clean_api_keys(value)
                secrets[key] = {"present": bool(keys), "count": len(keys)}
                if key in raw:
                    if isinstance(value, list):
                        values[key] = keys
                    elif isinstance(value, str):
                        # string 型 api_keys（如 vertex 单凭证）按原文透传
                        values[key] = value.strip()
            elif key in {"api_base", "proxy"} and _protected_url(value):
                secrets[key] = {"present": True, "preview": "已配置"}
            elif _valid_type(value, field["type"]):
                # 旧字段若错误地嵌入对象，不把其中未知的潜在密钥序列化出来。
                values[key] = copy.deepcopy(value)
        return {"values": values, "secrets": secrets}

    async def get_vision_providers(self) -> dict[str, Any]:
        self._ensure_open()
        return self.vision_directory.snapshot()

    def _ensure_open(self) -> None:
        if self.runtime.closed:
            raise StudioServiceError("插件正在关闭", status_code=503)

    async def close(self) -> None:
        await self.catalog.close()

    @staticmethod
    def _polling(settings: dict) -> list[str]:
        raw = settings.get("provider_polling") or []
        if not isinstance(raw, list):
            raise StudioServiceError(
                "轮询顺序格式无效，请在插件配置中修正", status_code=409
            )
        return list(
            dict.fromkeys(
                normalize_api_type(item)
                for item in raw
                if isinstance(item, str) and item.strip()
            )
        )

    def _snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        revision = self._revision()
        entries = []
        for index, raw in enumerate(settings.get("provider_overrides", [])):
            api_type = self._api_type(raw)
            template = self.templates.get(api_type)
            fields = template["fields"] if template else {}
            safe = (
                self._safe_fields(raw, fields)
                if template
                else {"values": {}, "secrets": {}}
            )
            # 未知供应商仍只展示密钥存在性，不显示任何原始值。
            if not template and isinstance(raw, dict):
                safe["secrets"] = self._safe_fields(
                    raw,
                    {key: {"type": "string"} for key in _SECRET_FIELDS if key in raw},
                )["secrets"]
            entries.append(
                {
                    "id": self._entry_id(revision, index),
                    "api_type": api_type,
                    "supported": template is not None,
                    **safe,
                    "unknown_fields": [
                        key
                        for key in raw
                        if key not in fields and key != "__template_key"
                    ]
                    if isinstance(raw, dict)
                    else [],
                }
            )
        polling = self._polling(settings)
        return {
            "revision": revision,
            "provider_polling": polling,
            "entries": entries,
            "common": self._safe_fields(settings, self.common_fields),
            "templates": copy.deepcopy(self.templates),
            "common_fields": copy.deepcopy(self.common_fields),
            "provider_types": [
                {"id": name, "label": template["label"]}
                for name, template in self.templates.items()
            ],
            **self.vision_directory.snapshot(),
            "key_values_visible": True,
            "model_catalog": catalog_capabilities(),
            "busy": bool(self.runtime.busy),
            "requires_reload": bool(self.runtime.failed or self.runtime.closed),
        }

    @staticmethod
    def _effective_proxy(entry: dict, common: dict) -> str | None:
        for value in (entry.get("proxy"), common.get("proxy")):
            if value is not None and not isinstance(value, str):
                raise _invalid("proxy")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return (
            os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
            or None
        )

    def _catalog_request(self, api_type: str, entry: dict, common: dict, key: str):
        default_base = self.templates[api_type]["fields"]["api_base"].get("default", "")
        base = entry.get("api_base")
        if base is not None and not isinstance(base, str):
            raise _invalid("api_base")
        return make_catalog_request(
            api_type,
            (base or "").strip() or default_base,
            key,
            self._effective_proxy(entry, common),
        )

    def _entry_model_request(self, payload: dict):
        if set(payload) - {"kind", "revision", "entry", "common", "confirmed_target"}:
            raise _invalid("模型查询")
        revision = self._revision()
        if payload.get("revision") != revision:
            raise _conflict()
        if type(payload.get("confirmed_target", False)) is not bool:
            raise _invalid("confirmed_target")
        entry = payload.get("entry")
        if not isinstance(entry, dict) or set(entry) - {
            "id",
            "api_type",
            "values",
            "secret_actions",
        }:
            raise _invalid("entry")
        if "id" not in entry or not isinstance(entry.get("api_type"), str):
            raise _invalid("entry")
        api_type = entry["api_type"]
        if api_type not in self.templates:
            raise _invalid("entry.api_type")
        original = {}
        if entry["id"] is not None:
            by_id = {
                self._entry_id(revision, i): value
                for i, value in enumerate(
                    self._settings().get("provider_overrides", [])
                )
            }
            if not isinstance(entry["id"], str) or entry["id"] not in by_id:
                raise _invalid("entry.id")
            original = by_id[entry["id"]]
            if not isinstance(original, dict) or api_type != self._api_type(original):
                raise _invalid("entry.api_type")
        fields = {
            name: value
            for name, value in self.templates[api_type]["fields"].items()
            if name in _SECRET_FIELDS
        }
        old_connection = {
            name: value for name, value in original.items() if name in fields
        }
        connection = self._merge_fields(
            old_connection,
            entry.get("values", {}),
            entry.get("secret_actions", {}),
            fields,
            "entry",
        )
        common = payload.get("common", {})
        if not isinstance(common, dict) or set(common) - {"values", "secret_actions"}:
            raise _invalid("common")
        old_common = {"proxy": self._settings().get("proxy", "")}
        merged_common = self._merge_fields(
            old_common,
            common.get("values", {}),
            common.get("secret_actions", {}),
            {"proxy": self.common_fields["proxy"]},
            "common",
        )
        keys = _clean_api_keys(connection.get("api_keys"))
        if not keys:
            raise StudioServiceError("请先填写至少一个 API Key，再拉取模型")
        query = self._catalog_request(api_type, connection, merged_common, keys[0])
        if keys[0] in _clean_api_keys(original.get("api_keys")) and not payload.get(
            "confirmed_target", False
        ):
            try:
                previous = self._catalog_request(
                    api_type, old_connection, old_common, keys[0]
                )
                changed = (previous.url, previous.proxy) != (query.url, query.proxy)
            except (ModelCatalogError, StudioServiceError):
                changed = True
            if changed:
                # 宿主 bridge 的错误只传字符串，确认状态走成功信封以保留目标信息。
                return {"confirmation_required": True, "target": safe_target(query)}
        return query

    async def fetch_models(self, payload: dict) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(payload, dict):
            raise _invalid("模型查询")
        try:
            if payload.get("kind") == "vision":
                if set(payload) != {"kind", "provider_id"}:
                    raise _invalid("视觉模型查询")
                provider = self.vision_directory.get_instance(payload["provider_id"])
                return await self.catalog.fetch_vision(provider)
            if payload.get("kind") != "entry":
                raise _invalid("模型查询类型")
            async with self.config_lock:
                self._ensure_open()
                query = self._entry_model_request(payload)
            if isinstance(query, dict):
                return query
            # 网络等待不占配置锁，不借用生成客户端或 Key 额度。
            return await self.catalog.fetch(query)
        except ModelCatalogError as exc:
            raise StudioServiceError(
                exc.message, status_code=exc.status_code, data={"reason": exc.reason}
            ) from None

    async def get_config(self) -> dict[str, Any]:
        async with self.config_lock:
            return self._snapshot()

    @staticmethod
    def _validate_value(value: Any, field: dict, location: str) -> None:
        if not _valid_type(value, field["type"]):
            raise _invalid(location)
        if "options" in field:
            values = value if isinstance(value, list) else [value]
            if any(item not in field["options"] for item in values):
                raise _invalid(location)
        if "slider" in field:
            slider = field["slider"]
            if not slider["min"] <= value <= slider["max"]:
                raise _invalid(location)
            step = slider.get("step", 0)
            if step:
                steps = (value - slider["min"]) / step
                if not math.isclose(steps, round(steps), abs_tol=1e-9):
                    raise _invalid(location)

    @staticmethod
    def _checked_keys(value: Any, location: str) -> list[str]:
        if (
            not isinstance(value, list)
            or len(value) > 200
            or any(not isinstance(item, str) or len(item) > 8192 for item in value)
        ):
            raise _invalid(location)
        return list(dict.fromkeys(_clean_api_keys(value)))

    @staticmethod
    def _string_key_field(fields: dict, key: str) -> bool:
        return fields.get(key, {}).get("type") == "string"

    def _coerce_api_keys(
        self, value: Any, fields: dict, key: str, location: str
    ) -> Any:
        allow_str = self._string_key_field(fields, key)
        if isinstance(value, str):
            # 仅 string 型 api_keys（如 vertex 单凭证）接受字符串，且持久化为字符串
            if not allow_str:
                raise _invalid(location)
            value = [value]
        keys = self._checked_keys(value, location)
        if allow_str:
            return keys[0] if keys else ""
        return keys

    def _merge_fields(
        self,
        old: dict,
        values: Any,
        actions: Any,
        fields: dict,
        location: str,
    ) -> dict:
        if not isinstance(values, dict) or not isinstance(actions, dict):
            raise _invalid(location)
        if set(values) - fields.keys() or set(actions) - (
            _SECRET_FIELDS & fields.keys()
        ):
            raise _invalid(location)
        merged = copy.deepcopy(old)
        for key, value in values.items():
            if fields.get(key, {}).get("type") == "file" and isinstance(value, list):
                # file 字段里的内联服务账号 JSON 落盘为文件引用，配置只存路径
                value = [
                    (
                        materialize_service_account_json(item)
                        if item.startswith("{")
                        else item
                    )
                    for item in value
                ]
            if (
                key == "service_account_json"
                and isinstance(value, str)
                and value.strip().startswith("{")
            ):
                # 粘贴内容落盘为文件引用，配置不保留 JSON 原文
                merged["service_account_json"] = ""
                merged["service_account_files"] = [
                    materialize_service_account_json(value.strip())
                ]
                continue
            if key in actions or (
                key in {"api_base", "proxy"} and _protected_url(old.get(key, ""))
            ):
                raise _invalid(f"{location}.{key}（请使用密钥操作）")
            if key in old and type(value) is type(old[key]) and value == old[key]:
                continue
            if key == "api_keys":
                merged[key] = self._coerce_api_keys(
                    value, fields, key, f"{location}.api_keys"
                )
                continue
            self._validate_value(value, fields[key], f"{location}.{key}")
            merged[key] = copy.deepcopy(value)
        for key, action in actions.items():
            if (
                not isinstance(action, dict)
                or not isinstance(action.get("mode"), str)
                or action["mode"] not in {"keep", "replace", "clear", "append"}
                or (action["mode"] == "append" and key != "api_keys")
            ):
                raise _invalid(f"{location}.{key}")
            mode = action["mode"]
            if set(action) != (
                {"mode", "value"} if mode in {"replace", "append"} else {"mode"}
            ):
                raise _invalid(f"{location}.{key}")
            if mode == "keep":
                continue
            if mode == "append":
                additions = self._checked_keys(action["value"], f"{location}.api_keys")
                combined = list(
                    dict.fromkeys(_clean_api_keys(old.get(key)) + additions)
                )
                merged[key] = self._coerce_api_keys(
                    combined, fields, key, f"{location}.api_keys"
                )
                continue
            value = (
                action["value"]
                if mode == "replace"
                else (
                    ""
                    if self._string_key_field(fields, key)
                    else ([] if key == "api_keys" else "")
                )
            )
            self._validate_value(value, fields[key], f"{location}.{key}")
            merged[key] = (
                self._coerce_api_keys(value, fields, key, f"{location}.api_keys")
                if key == "api_keys"
                else copy.deepcopy(value)
            )
        return merged

    def _merge_payload(self, payload: dict, revision: str) -> dict:
        if set(payload) != {"revision", "provider_polling", "entries", "common"}:
            raise _invalid("供应商配置")
        polling = payload["provider_polling"]
        allowed_polling = self.polling_options | set(self._polling(self._settings()))
        if (
            not isinstance(polling, list)
            or len(polling) > len(allowed_polling)
            or any(
                not isinstance(item, str) or item not in allowed_polling
                for item in polling
            )
            or len(set(polling)) != len(polling)
        ):
            raise _invalid("provider_polling")
        entries = payload["entries"]
        if not isinstance(entries, list) or len(entries) > 100:
            raise _invalid("entries（最多 100 条）")
        previous = self._settings()
        by_id = {
            self._entry_id(revision, index): raw
            for index, raw in enumerate(previous.get("provider_overrides", []))
        }
        merged_entries, seen = [], set()
        for index, entry in enumerate(entries):
            location = f"entries[{index + 1}]"
            if (
                not isinstance(entry, dict)
                or set(entry)
                - {
                    "id",
                    "api_type",
                    "values",
                    "secret_actions",
                }
                or not {"id", "api_type"} <= entry.keys()
            ):
                raise _invalid(location)
            entry_id, api_type = entry["id"], entry["api_type"]
            if not isinstance(api_type, str) or len(api_type) > _MAX_STRING:
                raise _invalid(f"{location}.api_type")
            if entry_id is not None:
                if (
                    not isinstance(entry_id, str)
                    or entry_id in seen
                    or entry_id not in by_id
                ):
                    raise _invalid(f"{location}.id")
                seen.add(entry_id)
                old = by_id[entry_id]
                if api_type != self._api_type(old):
                    raise _invalid(f"{location}.api_type")
            elif api_type not in self.templates:
                raise _invalid(f"{location}.api_type")
            else:
                old = {
                    "__template_key": api_type,
                    **{
                        key: copy.deepcopy(field["default"])
                        for key, field in self.templates[api_type]["fields"].items()
                        if "default" in field
                    },
                }
            values, actions = entry.get("values", {}), entry.get("secret_actions", {})
            if api_type not in self.templates:
                if (
                    values != {}
                    or not isinstance(actions, dict)
                    or any(
                        key not in _SECRET_FIELDS or action != {"mode": "keep"}
                        for key, action in actions.items()
                    )
                ):
                    raise _invalid(f"{location}（未知供应商只能保留或删除）")
                merged_entries.append(copy.deepcopy(old))
            else:
                merged_entries.append(
                    self._merge_fields(
                        old,
                        values,
                        actions,
                        self.templates[api_type]["fields"],
                        location,
                    )
                )
        common = payload["common"]
        if not isinstance(common, dict) or set(common) - {"values", "secret_actions"}:
            raise _invalid("common")
        merged = self._merge_fields(
            previous,
            common.get("values", {}),
            common.get("secret_actions", {}),
            self.common_fields,
            "common",
        )
        merged.update(
            provider_polling=copy.deepcopy(polling), provider_overrides=merged_entries
        )
        return merged

    async def save_config(self, payload: dict) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("revision"), str
        ):
            raise _invalid("revision")
        task = asyncio.create_task(self._save_transaction(copy.deepcopy(payload)))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 即使请求被重复取消，也等磁盘/运行时事务收尾，不能释放更新门。
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.exception() is not None:
                logger.warning("[供应商] 请求取消后的配置事务失败")
            raise

    def _host_revision(self) -> int | None:
        value = getattr(self.raw_config, "_save_revision", None)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def _persist(self, settings: dict) -> Any:
        update = {"provider_settings": copy.deepcopy(settings)}
        save_async = getattr(self.raw_config, "save_config_async", None)
        if callable(save_async):
            return await save_async(update)
        return await asyncio.to_thread(self.raw_config.save_config, update)

    def _superseded(self, expected: dict, host_revision: int | None) -> bool:
        return self.raw_config.get("provider_settings", {}) != expected or (
            host_revision is not None and self._host_revision() != host_revision + 1
        )

    async def _rollback(self, previous: dict, merged: dict, prepared: Any) -> None:
        host_revision = self._host_revision()
        failed = False
        try:
            committed = await self._persist(previous)
            if committed is False or self._superseded(previous, host_revision):
                failed = True
        except Exception:
            if self.raw_config.get("provider_settings") == merged:
                self.raw_config["provider_settings"] = copy.deepcopy(previous)
            failed = True
        try:
            self.restore(prepared)
        except Exception:
            failed = True
        if failed:
            self.runtime.failed = True
            logger.warning("[供应商] 配置回滚失败，生成已暂停，需重载插件")

    async def _save_transaction(self, payload: dict) -> dict[str, Any]:
        async with self.config_lock:
            revision = self._revision()
            if payload["revision"] != revision:
                raise _conflict()
            if self.runtime.failed or self.runtime.closed:
                raise StudioServiceError(
                    "供应商运行时不可用，请重载插件", status_code=503
                )
            try:
                with self.runtime.update():
                    return await self._update(payload, revision)
            except ProviderRuntimeBusy:
                raise StudioServiceError(
                    "生成任务正在运行，请稍后再次保存供应商配置",
                    status_code=409,
                    data={"reason": "busy"},
                ) from None

    async def _update(self, payload: dict, revision: str) -> dict[str, Any]:
        merged = self._merge_payload(payload, revision)
        if not any(
            callable(getattr(self.raw_config, name, None))
            for name in (
                "save_config_async",
                "save_config",
            )
        ):
            raise StudioServiceError(
                "当前宿主不提供配置保存接口，供应商配置只读", status_code=503
            )
        previous = copy.deepcopy(self._settings())
        try:
            prepared = await self.prepare(copy.deepcopy(merged))
        except StudioServiceError:
            raise
        except Exception:
            logger.warning("[供应商] 无法准备新供应商配置，原配置保持不变")
            raise StudioServiceError(
                "供应商配置无法应用，请检查配置条目", status_code=400
            ) from None
        if self._revision() != revision:
            self.runtime.failed = True
            raise _conflict()
        host_revision = self._host_revision()
        try:
            committed = await self._persist(merged)
        except Exception:
            current = self.raw_config.get("provider_settings", {})
            newer_host = host_revision is not None and self._host_revision() not in {
                host_revision,
                host_revision + 1,
            }
            if newer_host or (current != merged and current != previous):
                self.runtime.failed = True
                raise _conflict() from None
            if self.raw_config.get("provider_settings") == merged:
                self.raw_config["provider_settings"] = copy.deepcopy(previous)
            logger.warning("[供应商] 配置保存失败，原运行时保持不变")
            raise StudioServiceError(
                "保存供应商配置失败，原配置保持不变", status_code=500
            ) from None
        if committed is False or self._superseded(merged, host_revision):
            # 宿主更新优先，绝不能把旧快照回写覆盖它。
            self.runtime.failed = True
            raise _conflict()
        try:
            self.apply(prepared)
        except Exception:
            await self._rollback(previous, merged, prepared)
            raise StudioServiceError(
                "应用供应商配置失败，需重载插件"
                if self.runtime.failed
                else "应用供应商配置失败，已恢复原配置",
                status_code=500,
                data={"requires_reload": bool(self.runtime.failed)},
            ) from None
        logger.info("[供应商] Studio 配置已保存并即时生效")
        return self._snapshot()
