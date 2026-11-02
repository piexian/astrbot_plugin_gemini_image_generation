"""JSON 模板包。格式与在线服务的批量导入一致。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    PromptTemplate,
    TemplateValidationError,
    template_record,
    utc_now,
    validate_template_data,
)

PACKAGE_FORMAT = "gemini-image-templates"
PACKAGE_VERSION = 1
_PACKAGE_LIMIT = 500
_IGNORED_ITEM_FIELDS = ("preview", "ai_enabled", "origin", "remote")


@dataclass
class PackageError:
    index: int
    message: str


def parse_package(data: Any) -> tuple[list[PromptTemplate], list[PackageError]]:
    """解析模板包。格式不对则整体拒绝；单条不合法只进入错误列表。"""

    if not isinstance(data, dict):
        raise TemplateValidationError("format", "模板包必须是对象")
    if data.get("format") != PACKAGE_FORMAT or data.get("version") != PACKAGE_VERSION:
        raise TemplateValidationError("format", "模板包格式或版本不受支持")
    items = data.get("templates")
    if not isinstance(items, list):
        raise TemplateValidationError("templates", "templates 必须是数组")
    if len(items) > _PACKAGE_LIMIT:
        raise TemplateValidationError(
            "templates", f"模板包不能超过 {_PACKAGE_LIMIT} 条"
        )
    accepted: list[PromptTemplate] = []
    errors: list[PackageError] = []
    for index, item in enumerate(items):
        try:
            accepted.append(_parse_item(item, index=index))
        except TemplateValidationError as exc:
            errors.append(PackageError(index=index, message=str(exc)))
    return accepted, errors


def _parse_item(item: Any, *, index: int = 0) -> PromptTemplate:
    if not isinstance(item, dict):
        raise TemplateValidationError("template", "模板必须是对象")
    payload = {
        key: value for key, value in item.items() if key not in _IGNORED_ITEM_FIELDS
    }
    now = utc_now()
    if not str(payload.get("id") or "").strip():
        payload["id"] = f"import-{index}"
    if not str(payload.get("created_at") or "").strip():
        payload["created_at"] = now
    if not str(payload.get("updated_at") or "").strip():
        payload["updated_at"] = now
    payload["origin"] = "local"
    payload["ai_enabled"] = True
    return validate_template_data(payload, origin="local")


def export_package(templates: list[PromptTemplate]) -> dict[str, Any]:
    return {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "updated_at": utc_now(),
        "templates": [template_record(item, include_local=False) for item in templates],
    }
