"""提示词模板的数据模型与校验。

规则与在线模板服务的上传校验一致，导出的包可以直接导入。
名称、说明、作者和标签会进入主 Agent 的工具结果，因此在入库前清洗。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import quote, urlsplit

_ID_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_PREVIEW_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_ALLOWED_PLACEHOLDERS = ("{{input}}", "{{rows}}", "{{cols}}")
_REFERENCES = frozenset({"none", "optional", "required"})
_ORIGINS = frozenset({"builtin", "local", "remote"})
_NAME_MAX = 40
_DESCRIPTION_MAX = 80
_CONTENT_MAX = 8000
_AUTHOR_MAX = 32
_SOURCE_URL_MAX = 300
_TAG_COUNT_MAX = 10
_TAG_LEN_MAX = 20
_REMOTE_SOURCE_MAX = 100
_REMOTE_ID_MAX = 64


class TemplateValidationError(ValueError):
    """字段校验失败。说明只包含字段名和原因，不回显用户输入。"""

    def __init__(self, field_name: str, message: str) -> None:
        self.field = field_name
        super().__init__(message)


@dataclass
class RemoteOrigin:
    source: str
    remote_id: str


@dataclass
class PromptTemplate:
    id: str
    name: str
    content: str
    ai_description: str = ""
    defaults: dict[str, str] = field(default_factory=dict)
    reference: str = "none"
    tags: list[str] = field(default_factory=list)
    author: str | None = None
    source_url: str | None = None
    preview: str | None = None
    ai_enabled: bool = True
    origin: str = "local"
    remote: RemoteOrigin | None = None
    created_at: str = ""
    updated_at: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _char_len(value: str) -> int:
    return len(value)


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc"


def _strip_controls(value: str) -> str:
    return "".join(char for char in value if not _is_control(char))


def _has_forbidden_control(value: str) -> bool:
    return any(_is_control(char) and char not in "\n\r\t" for char in value)


def _fail(field_name: str, message: str) -> None:
    raise TemplateValidationError(field_name, message)


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _fail(field_name, f"{field_name} 必须是字符串")
    return value


def _clean_single_line(
    value: str, field_name: str, limit: int, *, required: bool
) -> str:
    cleaned = _strip_controls(value.strip())
    if required and not cleaned:
        _fail(field_name, f"{field_name} 必须是 1 到 {limit} 字")
    if _char_len(cleaned) > limit:
        _fail(field_name, f"{field_name} 不能超过 {limit} 字")
    return cleaned


def _check_placeholders(content: str) -> None:
    rest = content
    while True:
        start = rest.find("{{")
        if start < 0:
            return
        end = rest.find("}}", start)
        if end < 0:
            _fail("content", "content 包含未闭合的占位符")
        placeholder = rest[start : end + 2]
        if placeholder not in _ALLOWED_PLACEHOLDERS:
            _fail("content", "content 包含不支持的占位符，只允许 input、rows、cols")
        rest = rest[end + 2 :]


def _is_resolution(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,2}K", value))


def _is_aspect_ratio(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,2}:[0-9]{1,2}", value))


def _validate_defaults(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail("defaults", "defaults 必须是对象")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if key == "resolution":
            if not isinstance(item, str) or not _is_resolution(item):
                _fail("defaults", "defaults.resolution 必须形如 2K")
            cleaned[key] = item
        elif key == "aspect_ratio":
            if not isinstance(item, str) or not _is_aspect_ratio(item):
                _fail("defaults", "defaults.aspect_ratio 必须形如 16:9")
            cleaned[key] = item
        else:
            _fail("defaults", "defaults 含有不支持的字段")
    return cleaned


def _validate_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail("tags", "tags 必须是数组")
    if len(value) > _TAG_COUNT_MAX:
        _fail("tags", f"tags 不能超过 {_TAG_COUNT_MAX} 个")
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail("tags", f"tags 中每个标签必须是 1 到 {_TAG_LEN_MAX} 字")
        tag = _strip_controls(item.strip())
        if not tag or _char_len(tag) > _TAG_LEN_MAX:
            _fail("tags", f"tags 中每个标签必须是 1 到 {_TAG_LEN_MAX} 字")
        if tag not in seen:
            seen.append(tag)
    return seen


def _last_label_is_numeric(host: str) -> bool:
    """最后一段是纯数字或 0x 十六进制时，按 IP 字面量拒绝。"""

    text = host[:-1] if host.endswith(".") else host
    label = text.rsplit(".", 1)[-1].lower()
    if label.isdigit():
        return True
    if not label.startswith("0x"):
        return False
    digits = label[2:]
    return bool(digits) and all(char in "0123456789abcdef" for char in digits)


def _is_ip_host(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return _last_label_is_numeric(host)
    return True


def _encode_url_part(value: str, safe: str) -> str:
    return quote(value, safe=safe)


def validate_source_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail("source_url", "source_url 必须是字符串")
    trimmed = value.strip()
    if not trimmed:
        return None
    if any(_is_control(char) or char.isspace() for char in trimmed):
        _fail("source_url", "source_url 不能包含空白或控制字符")
    if _char_len(trimmed) > _SOURCE_URL_MAX:
        _fail("source_url", f"source_url 不能超过 {_SOURCE_URL_MAX} 字")
    try:
        parsed = urlsplit(trimmed)
        parsed_port = parsed.port
    except ValueError:
        _fail("source_url", "source_url 必须是 https 链接")
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        _fail("source_url", "source_url 必须是 https 链接")
    if parsed.username or parsed.password:
        _fail("source_url", "source_url 不能包含账号口令")
    host = parsed.hostname
    if _is_ip_host(host) or "." not in host:
        _fail("source_url", "source_url 必须是公开域名")
    try:
        host_ascii = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        _fail("source_url", "source_url 必须是公开域名")
    path = _encode_url_part(parsed.path or "/", "/%-._~!$&'()*+,;=:@")
    if not path.startswith("/"):
        path = "/" + path
    port = f":{parsed_port}" if parsed_port else ""
    normalized = f"https://{host_ascii}{port}{path}"
    if parsed.query:
        normalized += "?" + _encode_url_part(parsed.query, "&=%-._~!$'()*+,;:@/?")
    if parsed.fragment:
        normalized += "#" + _encode_url_part(parsed.fragment, "%-._~!$&'()*+,;=:@/?")
    if _char_len(normalized) > _SOURCE_URL_MAX:
        _fail("source_url", f"source_url 不能超过 {_SOURCE_URL_MAX} 字")
    return normalized


def _validate_remote(value: Any) -> RemoteOrigin | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _fail("remote", "remote 必须是对象")
    source = value.get("source")
    remote_id = value.get("remote_id")
    if not isinstance(source, str) or not isinstance(remote_id, str):
        _fail("remote", "remote 必须包含 source 和 remote_id")
    source = _clean_single_line(
        source, "remote.source", _REMOTE_SOURCE_MAX, required=True
    )
    remote_id = _clean_single_line(
        remote_id, "remote.remote_id", _REMOTE_ID_MAX, required=True
    )
    return RemoteOrigin(source=source, remote_id=remote_id)


def _validate_timestamp(value: Any, field_name: str) -> str:
    """校验 UTC ISO-8601 时间戳，接受可选小数秒（在线源给出微秒精度）。"""

    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        _fail(field_name, f"{field_name} 必须是 UTC 的 ISO-8601 时间")
    try:
        # 小数秒需先归一，fromisoformat 在 3.11 前不接受 "Z" 后缀。
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(field_name, f"{field_name} 不是有效时间")
    return value


def validate_template_data(data: Any, *, origin: str | None = None) -> PromptTemplate:
    """校验并清洗一条模板。未知字段忽略；``nsfw`` 为真时拒绝。"""

    if not isinstance(data, dict):
        _fail("template", "模板必须是对象")
    if data.get("nsfw") is True:
        _fail("nsfw", "不接受 nsfw 内容")
    template_id = _require_text(data.get("id"), "id").strip()
    if _ID_RE.fullmatch(template_id) is None:
        _fail("id", "id 只允许 1 到 48 位小写字母、数字、下划线或连字符")
    name = _clean_single_line(
        _require_text(data.get("name"), "name"), "name", _NAME_MAX, required=True
    )
    description_raw = data.get("ai_description")
    if description_raw is None:
        ai_description = ""
    else:
        ai_description = _clean_single_line(
            _require_text(description_raw, "ai_description"),
            "ai_description",
            _DESCRIPTION_MAX,
            required=False,
        )
    content = _require_text(data.get("content"), "content").strip()
    if not content or _char_len(content) > _CONTENT_MAX:
        _fail("content", f"content 必须是 1 到 {_CONTENT_MAX} 字")
    if _has_forbidden_control(content):
        _fail("content", "content 不能包含除换行、回车、制表符以外的控制字符")
    _check_placeholders(content)
    defaults = _validate_defaults(data.get("defaults"))
    reference_raw = data.get("reference")
    if reference_raw in (None, ""):
        reference = "none"
    elif isinstance(reference_raw, str) and reference_raw in _REFERENCES:
        reference = reference_raw
    else:
        _fail("reference", "reference 只允许 none、optional 或 required")
    tags = _validate_tags(data.get("tags"))
    author_raw = data.get("author")
    author = None
    if author_raw is not None:
        author = (
            _clean_single_line(
                _require_text(author_raw, "author"),
                "author",
                _AUTHOR_MAX,
                required=False,
            )
            or None
        )
    source_url = validate_source_url(data.get("source_url"))
    preview_raw = data.get("preview")
    preview = None
    if isinstance(preview_raw, str) and preview_raw.strip():
        preview = preview_raw.strip()
        if _PREVIEW_RE.fullmatch(preview) is None or "/" in preview or "\\" in preview:
            _fail("preview", "preview 只允许文件名")
    ai_enabled_raw = data.get("ai_enabled", True)
    if not isinstance(ai_enabled_raw, bool):
        _fail("ai_enabled", "ai_enabled 必须是布尔值")
    resolved_origin = origin if origin is not None else data.get("origin", "local")
    if resolved_origin not in _ORIGINS:
        _fail("origin", "origin 只允许 builtin、local 或 remote")
    remote = _validate_remote(data.get("remote"))
    created_at = _validate_timestamp(data.get("created_at"), "created_at")
    updated_at = _validate_timestamp(data.get("updated_at"), "updated_at")
    return PromptTemplate(
        id=template_id,
        name=name,
        ai_description=ai_description,
        content=content,
        defaults=defaults,
        reference=reference,
        tags=tags,
        author=author,
        source_url=source_url,
        preview=preview,
        ai_enabled=ai_enabled_raw,
        origin=resolved_origin,
        remote=remote,
        created_at=created_at,
        updated_at=updated_at,
    )


def template_record(
    template: PromptTemplate, *, include_local: bool = True
) -> dict[str, Any]:
    """可写回文件的记录。在线包导出时不带只在本地使用的字段。"""

    record: dict[str, Any] = {
        "id": template.id,
        "name": template.name,
        "content": template.content,
        "reference": template.reference,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }
    if template.ai_description:
        record["ai_description"] = template.ai_description
    if template.defaults:
        record["defaults"] = dict(template.defaults)
    if template.tags:
        record["tags"] = list(template.tags)
    if template.author:
        record["author"] = template.author
    if template.source_url:
        record["source_url"] = template.source_url
    if not include_local:
        return record
    record["ai_enabled"] = template.ai_enabled
    record["origin"] = template.origin
    if template.preview:
        record["preview"] = template.preview
    if template.remote is not None:
        record["remote"] = {
            "source": template.remote.source,
            "remote_id": template.remote.remote_id,
        }
    return record
