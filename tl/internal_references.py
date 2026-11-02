"""内部引用的语法与解析。本模块不接入命令或工具。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .generation_tracker import _SAFE_FILE_NAME
from .tool_path_guard import filter_reference_paths, raw_has_traversal

_MAX_ITEMS = 16
_MAX_TOKEN_LENGTH = 512
_MAX_INDEX = 20
_DISPLAY_LENGTH = 64
_USER_ID = re.compile(r"^[0-9A-Za-z_.-]{1,64}$")
_GEN_JOB = re.compile(r"^gen:([A-Za-z0-9_.-]{1,128})(?:#([1-9][0-9]*))?$")
_AUTO_AT_FLAGS = ("is_auto", "auto", "auto_at", "autoAt")


class InternalReferenceError(Exception):
    """某一项引用无法使用。``code`` 沿用现有参考图错误码。"""

    def __init__(self, token: str, code: str, message: str) -> None:
        self.token = token
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ReferenceSpec:
    kind: str
    token: str
    user_id: str | None = None
    job_id: str | None = None
    index: int = 1


@dataclass
class ResolvedReferences:
    """消息图、头像、用户本地文件和画廊文件分开保存。

    用户路径先过 ``tool_path_guard``，画廊文件只在作用域通过后标为受信。
    下游应分别读取，不能把两类本地文件放进同一次受信读取。
    """

    message_images: list[str] = field(default_factory=list)
    avatar_reference: list[str] = field(default_factory=list)
    user_files: list[str] = field(default_factory=list)
    gallery_files: list[str] = field(default_factory=list)
    gallery_names: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)

    def as_generation_inputs(self) -> dict[str, Any]:
        return {
            "reference_images": [*self.message_images, *self.user_files],
            "avatar_reference": list(self.avatar_reference),
            "trusted_local_images": list(self.gallery_files),
            "reference_names": list(self.gallery_names),
        }


def parse_references(values: Sequence[str]) -> list[ReferenceSpec]:
    """只识别语法，不读取事件或文件。重复项去重并保持首次出现的顺序。"""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise InternalReferenceError("", "invalid_request", "引用必须是字符串列表")
    if len(values) > _MAX_ITEMS:
        raise InternalReferenceError("", "invalid_request", f"引用最多 {_MAX_ITEMS} 项")
    specs: list[ReferenceSpec] = []
    seen: set[str] = set()
    for position, value in enumerate(values, start=1):
        if not isinstance(value, str):
            _fail("", "invalid_request", f"第 {position} 项必须是字符串")
        spec = _parse_one(value, position)
        if spec.token in seen:
            continue
        seen.add(spec.token)
        specs.append(spec)
    return specs


class ReferenceResolver:
    def __init__(
        self,
        *,
        image_handler: Any,
        avatar_handler: Any,
        generation_tracker: Any,
        settings: Callable[[], Any],
    ) -> None:
        self.image_handler = image_handler
        self.avatar_handler = avatar_handler
        self.generation_tracker = generation_tracker
        self._settings = settings

    async def resolve(
        self, specs: Sequence[ReferenceSpec], event: Any
    ) -> ResolvedReferences:
        resolved = ResolvedReferences()
        for spec in specs:
            if spec.kind == "current":
                await self._resolve_current(spec, event, resolved)
            elif spec.kind.startswith("avatar"):
                await self._resolve_avatar(spec, event, resolved)
            elif spec.kind.startswith("gen"):
                self._resolve_generation(spec, event, resolved)
            elif spec.kind == "local":
                self._resolve_local(spec, resolved)
            else:
                _fail(
                    spec.token, "invalid_request", _item(spec.token, "无法识别的引用")
                )
        self._enforce_limit(resolved)
        return resolved

    async def _resolve_current(
        self, spec: ReferenceSpec, event: Any, resolved: ResolvedReferences
    ) -> None:
        try:
            images, _avatars = await self.image_handler.fetch_images_from_event(
                event, include_at_avatars=False
            )
        except Exception as exc:
            _fail(
                spec.token,
                "reference_resolve_failed",
                _item(spec.token, f"读取当前消息失败：{exc}"),
            )
        images = [str(item) for item in images or [] if item]
        if not images:
            _fail(
                spec.token,
                "reference_resolve_failed",
                _item(spec.token, "当前消息没有图片"),
            )
        resolved.message_images.extend(images)
        resolved.sources.append(
            {"token": spec.token, "kind": "message", "label": "当前消息"}
        )

    async def _resolve_avatar(
        self, spec: ReferenceSpec, event: Any, resolved: ResolvedReferences
    ) -> None:
        mentioned = _mentioned_ids(event)
        if spec.kind == "avatar_sender":
            user_ids = [_sender_id(event, spec.token)]
            label = "发送者头像"
        elif spec.kind == "avatar_mentioned":
            if not mentioned:
                _fail(
                    spec.token,
                    "reference_resolve_failed",
                    _item(spec.token, "当前消息没有手动提及的用户"),
                )
            user_ids = mentioned
            label = "被提及用户头像"
        else:
            allowed = {_sender_id(event, spec.token), *mentioned}
            if spec.user_id not in allowed:
                _fail(
                    spec.token, "reference_access_denied", _item(spec.token, "无权引用")
                )
            user_ids = [str(spec.user_id)]
            label = "指定用户头像"
        images: list[str] = []
        for user_id in user_ids:
            try:
                image = await self.avatar_handler.download_user_avatar(user_id, event)
            except Exception as exc:
                _fail(
                    spec.token,
                    "reference_resolve_failed",
                    _item(spec.token, f"头像获取失败：{exc}"),
                )
            if not image:
                _fail(
                    spec.token,
                    "reference_resolve_failed",
                    _item(spec.token, "头像获取失败"),
                )
            images.append(str(image))
        resolved.avatar_reference.extend(images)
        resolved.sources.append({"token": spec.token, "kind": "avatar", "label": label})

    def _resolve_generation(
        self, spec: ReferenceSpec, event: Any, resolved: ResolvedReferences
    ) -> None:
        settings = self._current_settings()
        if not settings.history_enabled or not getattr(
            self.generation_tracker, "enabled", True
        ):
            _unavailable(spec.token)
        if spec.kind == "gen_last":
            record = self._latest_session_record(_event_umo(event))
            if record is None:
                _fail(
                    spec.token,
                    "reference_resolve_failed",
                    _item(spec.token, "本会话没有可引用的生成"),
                )
        else:
            record = self.generation_tracker.get(str(spec.job_id))
            if not _record_is_visible(
                record, _event_umo(event), settings.cross_session
            ):
                _fail(
                    spec.token, "reference_access_denied", _item(spec.token, "无权引用")
                )
            if not _record_has_images(record):
                _fail(
                    spec.token,
                    "reference_resolve_failed",
                    _item(spec.token, "这张图还不能引用"),
                )
        images = [str(item) for item in record.get("images") or []]
        if spec.index > len(images):
            _fail(
                spec.token,
                "reference_resolve_failed",
                _item(spec.token, "没有对应的图片"),
            )
        name = images[spec.index - 1]
        path = self.generation_tracker.gallery_image_path(name)
        if path is None:
            _unavailable(spec.token)
        resolved.gallery_files.append(str(path))
        resolved.gallery_names.append(name)
        resolved.sources.append(
            {"token": spec.token, "kind": "gallery", "label": "生成记录"}
        )

    def _resolve_local(self, spec: ReferenceSpec, resolved: ResolvedReferences) -> None:
        settings = self._current_settings()
        accepted, rejected = filter_reference_paths(
            [spec.token],
            allowed_dirs=list(settings.allowed_dirs),
            global_mode=settings.global_paths,
        )
        if rejected or not accepted:
            _fail(
                spec.token, "path_policy_rejected", _item(spec.token, "本地路径不可用")
            )
        resolved.user_files.extend(accepted)
        resolved.sources.append(
            {"token": spec.token, "kind": "user_file", "label": "本地文件"}
        )

    def _latest_session_record(self, umo: str) -> dict[str, Any] | None:
        matches = []
        for record in self.generation_tracker.records_snapshot():
            requester = record.get("requester") or {}
            record_umo = str(requester.get("umo") or "")
            if not umo or not record_umo or record_umo != umo:
                continue
            if not _record_has_images(record):
                continue
            matches.append(record)
        if not matches:
            return None
        matches.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return matches[0]

    def _enforce_limit(self, resolved: ResolvedReferences) -> None:
        settings = self._current_settings()
        total = (
            len(resolved.message_images)
            + len(resolved.avatar_reference)
            + len(resolved.user_files)
            + len(resolved.gallery_files)
        )
        if total > settings.max_reference_images:
            _fail(
                "",
                "invalid_request",
                f"参考图共 {total} 张，最多 {settings.max_reference_images} 张",
            )

    def _current_settings(self) -> Any:
        settings = self._settings()
        if isinstance(settings, dict):
            required = (
                "cross_session",
                "history_enabled",
                "max_reference_images",
                "allowed_dirs",
                "global_paths",
            )
            if any(key not in settings for key in required):
                _fail("", "invalid_request", "引用配置不完整")
            return type("Settings", (), settings)()
        return settings


def _parse_one(value: str, position: int) -> ReferenceSpec:
    token = value.strip()
    if len(token) > _MAX_TOKEN_LENGTH:
        _fail(
            token[:_DISPLAY_LENGTH],
            "invalid_request",
            f"第 {position} 项超过 {_MAX_TOKEN_LENGTH} 字符",
        )
    if not token:
        _fail("", "invalid_request", f"第 {position} 项是空引用")
    if token == "current":
        return ReferenceSpec("current", token)
    if token == "avatar:sender":
        return ReferenceSpec("avatar_sender", token)
    if token == "avatar:mentioned":
        return ReferenceSpec("avatar_mentioned", token)
    if token == "gen:last" or token.startswith("gen:last#"):
        if token != "gen:last":
            _fail(token, "invalid_request", _item(token, "gen:last 不带序号"))
        return ReferenceSpec("gen_last", token)
    if token.startswith("avatar:"):
        user_id = token[len("avatar:") :]
        if _USER_ID.fullmatch(user_id) is None:
            _fail(token, "invalid_request", _item(token, "用户 ID 不合法"))
        return ReferenceSpec("avatar_user", token, user_id=user_id)
    matched = _GEN_JOB.fullmatch(token)
    if token.startswith("gen:"):
        if (
            matched is None
            or matched.group(1) == "last"
            or not _SAFE_FILE_NAME.fullmatch(matched.group(1))
        ):
            _fail(token, "invalid_request", _item(token, "生成引用不合法"))
        index = int(matched.group(2) or "1")
        if index > _MAX_INDEX:
            _fail(
                token, "invalid_request", _item(token, f"图片序号不能超过 {_MAX_INDEX}")
            )
        return ReferenceSpec("gen_job", token, job_id=matched.group(1), index=index)
    if _is_windows_path(token) or token.startswith("/") or token.startswith("file://"):
        if raw_has_traversal(token) or token.startswith("~"):
            _fail(token, "invalid_request", _item(token, "本地路径不合法"))
        return ReferenceSpec("local", token)
    if ":" in token or token.startswith("~") or not token.startswith("/"):
        _fail(token, "invalid_request", _item(token, "无法识别的引用"))
    return ReferenceSpec("local", token)


def _record_has_images(record: dict[str, Any]) -> bool:
    return record.get("status") in {"succeeded", "partial_success"} and bool(
        record.get("images")
    )


def _record_is_visible(
    record: dict[str, Any] | None, umo: str, cross_session: bool
) -> bool:
    if not isinstance(record, dict) or not umo:
        return False
    record_umo = str((record.get("requester") or {}).get("umo") or "")
    if not record_umo:
        return False
    if record_umo == umo:
        return True
    return record.get("shared") is True or cross_session


def _mentioned_ids(event: Any) -> list[str]:
    try:
        messages = event.get_messages()
    except Exception:
        messages = []
    self_id = _text_attr(event, "get_self_id")
    found: list[str] = []
    for component in messages or []:
        if _is_auto_at(component):
            continue
        user_id = getattr(component, "qq", None) or getattr(component, "user_id", None)
        if not user_id:
            continue
        text = str(user_id)
        if self_id and text == self_id:
            continue
        if text not in found:
            found.append(text)
    return found


def _sender_id(event: Any, token: str) -> str:
    sender = _text_attr(event, "get_sender_id")
    if sender:
        return sender
    message_obj = getattr(event, "message_obj", None)
    sender_obj = getattr(message_obj, "sender", None)
    user_id = getattr(sender_obj, "user_id", None)
    if user_id:
        return str(user_id)
    _fail(token, "reference_resolve_failed", _item(token, "没有发送者"))
    return ""


def _event_umo(event: Any) -> str:
    try:
        return str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        return ""


def _text_attr(event: Any, name: str) -> str:
    try:
        method = getattr(event, name, None)
        value = method() if callable(method) else None
    except Exception:
        return ""
    return str(value or "")


def _is_auto_at(component: Any) -> bool:
    for name in _AUTO_AT_FLAGS:
        flag = getattr(component, name, None)
        if isinstance(flag, str):
            enabled = flag.lower() in {"true", "1", "yes", "y"}
        else:
            enabled = bool(flag)
        if enabled:
            return True
    return False


def _is_windows_path(value: str) -> bool:
    return (
        len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "\\/"
    )


def _item(token: str, message: str) -> str:
    return f"{message}：{_display(token)}"


def _display(token: str) -> str:
    text = token.replace("\n", " ")
    if len(text) <= _DISPLAY_LENGTH:
        return text
    return text[:_DISPLAY_LENGTH] + "…"


def _unavailable(token: str) -> None:
    _fail(
        token,
        "reference_image_unavailable",
        _item(token, "参考图片不存在或正在清理，请重新选择"),
    )


def _fail(token: str, code: str, message: str) -> None:
    raise InternalReferenceError(token, code, message)
