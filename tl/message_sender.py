"""消息格式化和发送模块"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Node, Plain

from .napcat_stream import upload_file_stream
from .thought_signature import log_thought_signature_debug
from .tl_utils import encode_file_to_base64
from .tl_utils import is_valid_base64_image_str as util_is_valid_base64_image_str

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

# 本地图片 base64 编码的默认大小阈值（2MB）
DEFAULT_MAX_INLINE_IMAGE_SIZE_BYTES = 2 * 1024 * 1024
# 超大文件警告阈值（10MB）
LARGE_FILE_WARNING_THRESHOLD_BYTES = 10 * 1024 * 1024
AUTH_LIKE_QUERY_KEYS = {
    "key",
    "token",
    "sig",
    "signature",
    "expires",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
}


class MessageSender:
    """消息格式化和发送处理器"""

    def __init__(
        self,
        enable_text_response: bool = False,
        max_inline_image_size_mb: float = 2.0,
        napcat_stream_threshold_mb: float = 2.0,
        log_debug_fn=None,
    ):
        """
        Args:
            enable_text_response: 是否启用文本响应
            max_inline_image_size_mb: 本地图片 base64 编码阈值（MB）
            napcat_stream_threshold_mb: NapCat Stream API 兜底上传阈值（MB）
            log_debug_fn: 可选的日志函数
        """
        self.enable_text_response = enable_text_response
        self.max_inline_image_size_bytes = int(max_inline_image_size_mb * 1024 * 1024)
        self.napcat_stream_threshold_bytes = int(
            max(float(napcat_stream_threshold_mb or 0), 0.0) * 1024 * 1024
        )
        self._log_debug = log_debug_fn or logger.debug

    def update_config(
        self,
        enable_text_response: bool | None = None,
        max_inline_image_size_mb: float | None = None,
        napcat_stream_threshold_mb: float | None = None,
    ):
        """更新配置"""
        if enable_text_response is not None:
            self.enable_text_response = enable_text_response
        if max_inline_image_size_mb is not None:
            self.max_inline_image_size_bytes = int(
                max_inline_image_size_mb * 1024 * 1024
            )
        if napcat_stream_threshold_mb is not None:
            self.napcat_stream_threshold_bytes = int(
                max(float(napcat_stream_threshold_mb or 0), 0.0) * 1024 * 1024
            )

    @staticmethod
    def is_aioqhttp_event(event: AstrMessageEvent) -> bool:
        """判断事件是否来自aiocqhttp平台"""
        try:
            platform_name = event.get_platform_name()
            return platform_name == "aiocqhttp"
        except AttributeError as e:
            logger.debug(f"判断平台类型失败: {e}")
            return False

    async def safe_send(self, event: AstrMessageEvent, payload):
        """包装发送，若平台发送失败则提示用户"""
        try:
            yield payload
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            yield event.plain_result("⚠️ 消息发送失败，请稍后重试或检查网络/权限。")

    def _stream_fallback_candidate(self, image: str) -> str | None:
        if self.napcat_stream_threshold_bytes <= 0 or not image:
            return None
        if image.startswith(("http://", "https://", "data:image/")):
            return None
        if util_is_valid_base64_image_str(image):
            return None

        candidate = image[8:] if image.startswith("file:///") else image
        if not os.path.isfile(candidate):
            return None

        try:
            if os.path.getsize(candidate) < self.napcat_stream_threshold_bytes:
                return None
        except OSError:
            return None
        return candidate

    async def _build_stream_fallback_paths(
        self,
        event: AstrMessageEvent,
        image_paths: list[str] | None,
    ) -> list[str] | None:
        fallback_paths: list[str] = []
        changed = False

        for image in image_paths or []:
            candidate = self._stream_fallback_candidate(image)
            if not candidate:
                if image:
                    fallback_paths.append(image)
                continue

            streamed_path = await upload_file_stream(event, candidate)
            if streamed_path:
                fallback_paths.append(streamed_path)
                changed = True
            else:
                fallback_paths.append(image)

        return fallback_paths if changed else None

    async def _send_dispatch_results(
        self,
        *,
        event: AstrMessageEvent,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        text_content: str | None,
        thought_signature: str | None = None,
        scene: str = "默认",
        force_text_response: bool = False,
        text_content_prepared: bool = False,
    ) -> None:
        if not hasattr(event, "send"):
            raise RuntimeError("当前事件对象不支持直接发送，无法执行 Stream API 兜底")

        async for payload in self.dispatch_send_results(
            event=event,
            image_urls=image_urls,
            image_paths=image_paths,
            text_content=text_content,
            thought_signature=thought_signature,
            scene=scene,
            force_text_response=force_text_response,
            text_content_prepared=text_content_prepared,
        ):
            await event.send(payload)

    async def send_results_with_stream_retry(
        self,
        *,
        event: AstrMessageEvent,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        text_content: str | None,
        thought_signature: str | None = None,
        scene: str = "默认",
        force_text_response: bool = False,
        text_content_prepared: bool = False,
    ) -> None:
        """先按现有逻辑发送；失败后再使用 NapCat Stream API 兜底重试一次。"""
        first_error: Exception | None = None
        try:
            await self._send_dispatch_results(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=text_content,
                thought_signature=thought_signature,
                scene=scene,
                force_text_response=force_text_response,
                text_content_prepared=text_content_prepared,
            )
            return
        except Exception as exc:
            first_error = exc
            logger.warning(
                f"[SEND] {scene} 原始发送失败，准备检查 Stream API 兜底: {exc}"
            )

        fallback_paths = await self._build_stream_fallback_paths(event, image_paths)
        if not fallback_paths:
            raise RuntimeError(
                "原始发送失败，且没有可用于 NapCat Stream API 兜底的本地图片"
            ) from first_error

        logger.warning(f"[SEND] {scene} 已启用 NapCat Stream API 兜底重试")
        await self._send_dispatch_results(
            event=event,
            image_urls=[],
            image_paths=fallback_paths,
            text_content=text_content,
            thought_signature=thought_signature,
            scene=f"{scene}/Stream兜底",
            force_text_response=force_text_response,
            text_content_prepared=text_content_prepared,
        )

    async def send_api_duration(
        self,
        event: AstrMessageEvent,
        api_duration: float,
        send_duration: float | None = None,
    ):
        """
        发送耗时统计消息

        Args:
            event: 消息事件
            api_duration: API 响应耗时（秒）
            send_duration: 消息发送耗时（秒），可选
        """
        try:
            if send_duration is not None:
                msg = f"⏱️ API响应 {api_duration:.1f}s | 发送 {send_duration:.1f}s"
            else:
                msg = f"⏱️ API响应 {api_duration:.1f}s"
            async for res in self.safe_send(event, event.plain_result(msg)):
                yield res
        except Exception as e:
            # 非关键统计信息发送失败时仅记录日志，避免影响主流程
            logger.error(f"发送耗时统计消息失败: {e}")

    @staticmethod
    def clean_text_content(text: str) -> str:
        """清理文本内容，移除 markdown 图片链接等不可发送的内容"""
        if not text:
            return text

        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = text.strip()

        return text

    @staticmethod
    def strip_known_image_refs(text: str, image_refs: list[str] | None) -> str:
        """从文本中移除已识别为图片的 URL/路径，避免图文重复发送。"""
        if not text or not image_refs:
            return text

        cleaned = text
        candidates: list[str] = []
        for ref in image_refs:
            ref_str = str(ref).strip()
            if not ref_str:
                continue
            candidates.append(ref_str)
            if "&" in ref_str:
                candidates.append(ref_str.replace("&", "&amp;"))

        for candidate in sorted(set(candidates), key=len, reverse=True):
            cleaned = re.sub(re.escape(candidate), "", cleaned)

        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return cleaned.strip(" \t\n\r-—:：")

    def prepare_text_content(
        self, text: str | None, image_refs: list[str] | None = None
    ) -> str:
        """统一清理待发送文本，并按需移除已识别图片引用。"""
        cleaned_text = self.clean_text_content(text) if text else ""
        return self.strip_known_image_refs(cleaned_text, image_refs)

    @staticmethod
    def _normalize_image_ref(ref: str) -> str:
        ref_str = str(ref).strip()
        if not ref_str:
            return ""

        if ref_str.startswith("file:///"):
            ref_str = ref_str[8:]

        if os.path.exists(ref_str):
            return os.path.realpath(ref_str)

        if ref_str.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(ref_str)
            query_keys = {
                key.lower()
                for key, _ in urllib.parse.parse_qsl(
                    parsed.query, keep_blank_values=True
                )
            }
            if query_keys and query_keys.issubset(AUTH_LIKE_QUERY_KEYS):
                return urllib.parse.urlunsplit(
                    (
                        parsed.scheme.lower(),
                        parsed.netloc.lower(),
                        parsed.path,
                        "",
                        "",
                    )
                )
            return urllib.parse.urlunsplit(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path,
                    parsed.query,
                    "",
                )
            )

        return ref_str

    @staticmethod
    def merge_available_images(
        image_urls: list[str] | None, image_paths: list[str] | None
    ) -> list[str]:
        """合并 URL 与路径，URL 优先，保持顺序并按规范化引用去重。

        对远程 URL 会去掉鉴权类 query 参数（如 key/token/signature），
        避免同一张图的签名版 URL 和裸 URL 被当成两张图发送。
        """
        merged: list[str] = []
        seen: set[str] = set()

        # URL 优先，paths 作为补充
        for img in (image_urls or []) + (image_paths or []):
            if not img:
                continue
            normalized = MessageSender._normalize_image_ref(img)
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(img)

        return merged

    def build_forward_image_component(self, image: str, *, force_base64: bool = False):
        """根据来源构造合并转发图片组件，优先使用本地文件。

        force_base64=True 时，若图片来源为本地文件/data URL，会强制转换为 base64:// 以适配
        NapCat/OneBotv11 等无法直接访问 AstrBot 宿主文件系统的场景。
        """
        try:
            if not image:
                raise ValueError("空的图片地址")
            if image.startswith("data:image/") and ";base64," in image:
                if force_base64:
                    _, _, b64_part = image.partition(";base64,")
                    cleaned = "".join(b64_part.split())
                    if cleaned:
                        return AstrImage(file=f"base64://{cleaned}")
                return AstrImage(file=image)
            if util_is_valid_base64_image_str(image):
                return AstrImage(file=f"base64://{image}")

            fs_candidate = image
            if image.startswith("file:///"):
                fs_candidate = image[8:]

            if os.path.exists(fs_candidate):
                # force_base64 时始终编码，支持无法访问本地文件系统的平台
                if force_base64:
                    file_size = os.path.getsize(fs_candidate)
                    if file_size > LARGE_FILE_WARNING_THRESHOLD_BYTES:
                        logger.warning(
                            f"强制 base64 编码超大文件 ({file_size / 1024 / 1024:.1f}MB)，"
                            "可能导致内存压力"
                        )
                    b64_data = encode_file_to_base64(fs_candidate)
                    return AstrImage(file=f"base64://{b64_data}")
                file_size = os.path.getsize(fs_candidate)
                if file_size > self.max_inline_image_size_bytes:
                    return AstrImage.fromFileSystem(fs_candidate)
                b64_data = encode_file_to_base64(fs_candidate)
                return AstrImage(file=f"base64://{b64_data}")
            if image.startswith(("http://", "https://")):
                return AstrImage.fromURL(image)

            return AstrImage(file=image)
        except Exception as e:
            logger.warning(f"构造图片组件失败: {e}")
            return Plain(f"[图片不可用: {image[:48]}]")

    async def dispatch_send_results(
        self,
        event: AstrMessageEvent,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        text_content: str | None,
        thought_signature: str | None = None,
        scene: str = "默认",
        force_text_response: bool = False,
        text_content_prepared: bool = False,
    ):
        """
        根据内容数量选择发送模式：
        - 单图：链式富媒体发送（文本+图一起）
        - 总数<=4：链式富媒体发送（文本+多图一起）
        - 总数>4：合并转发
        """

        # 优先 URL，paths 作为补充（URL 在前，去重）
        available_images = self.merge_available_images(image_urls, image_paths)

        cleaned_text = (
            text_content or ""
            if text_content_prepared
            else self.prepare_text_content(text_content, available_images)
        )
        text_to_send = (
            cleaned_text
            if ((self.enable_text_response or force_text_response) and cleaned_text)
            else ""
        )
        total_items = len(available_images) + (1 if text_to_send else 0)
        is_aioqhttp = self.is_aioqhttp_event(event)

        logger.debug(
            f"[SEND] 场景={scene}，图片={len(available_images)}，文本={'1' if text_to_send else '0'}，总计={total_items}"
        )

        if not available_images:
            if cleaned_text:
                async for res in self.safe_send(
                    event,
                    event.plain_result(
                        "⚠️ 当前模型只返回了文本，请检查模型配置或者重试"
                    ),
                ):
                    yield res
                if text_to_send:
                    async for res in self.safe_send(
                        event, event.plain_result(f"📝 {text_to_send}")
                    ):
                        yield res
            else:
                async for res in self.safe_send(
                    event,
                    event.plain_result(
                        "❌ 未能成功生成图像。\n"
                        "🧐 可能原因：模型返回空结果、提示词冲突或参考图处理异常。\n"
                        "✅ 建议：简化描述、减少参考图后重试，或稍后重试。"
                    ),
                ):
                    yield res
            return

        # 单图直发
        if len(available_images) == 1:
            logger.debug("[SEND] 采用单图直发模式")
            if text_to_send:
                async for res in self.safe_send(
                    event,
                    event.chain_result(
                        [
                            Comp.Plain(f"\u200b📝 {text_to_send}"),
                            self.build_forward_image_component(
                                available_images[0], force_base64=is_aioqhttp
                            ),
                        ]
                    ),
                ):
                    yield res
            else:
                if is_aioqhttp:
                    img_component = self.build_forward_image_component(
                        available_images[0], force_base64=True
                    )
                    async for res in self.safe_send(
                        event, event.chain_result([img_component])
                    ):
                        yield res
                else:
                    async for res in self.safe_send(
                        event, event.image_result(available_images[0])
                    ):
                        yield res
            log_thought_signature_debug(thought_signature, scene=f"{scene}/单图发送")
            return

        # AIOCQHTTP 逐图发送（base64）
        if is_aioqhttp:
            logger.debug("[SEND] AIOCQHTTP 平台，采用逐图发送（base64）")
            start_idx = 0
            if text_to_send:
                first_img = self.build_forward_image_component(
                    available_images[0], force_base64=True
                )
                async for res in self.safe_send(
                    event,
                    event.chain_result(
                        [Comp.Plain(f"\u200b📝 {text_to_send}"), first_img]
                    ),
                ):
                    yield res
                start_idx = 1

            for img in available_images[start_idx:]:
                img_component = self.build_forward_image_component(
                    img, force_base64=True
                )
                async for res in self.safe_send(
                    event, event.chain_result([img_component])
                ):
                    yield res

            log_thought_signature_debug(thought_signature, scene=f"{scene}/逐图发送")
            return

        # 短链富媒体发送
        if total_items <= 4:
            logger.debug("[SEND] 采用短链富媒体发送模式")
            chain: list = []
            if text_to_send:
                chain.append(Comp.Plain(f"\u200b📝 {text_to_send}"))
            for img in available_images:
                chain.append(self.build_forward_image_component(img))
            if chain:
                async for res in self.safe_send(event, event.chain_result(chain)):
                    yield res
            log_thought_signature_debug(thought_signature, scene=f"{scene}/短链发送")
            return

        # 合并转发
        logger.debug("[SEND] 采用合并转发模式")

        node_content = []
        if text_to_send:
            node_content.append(Plain(f"📝 {text_to_send}"))

        for idx, img in enumerate(available_images, 1):
            node_content.append(Plain(f"图片 {idx}:"))

            try:
                img_component = self.build_forward_image_component(img)
                node_content.append(img_component)
            except Exception as e:
                logger.warning(f"构造合并转发图片节点失败: {e}")
                node_content.append(Plain(f"[图片不可用: {img[:48]}]"))

        sender_id = "0"
        sender_name = "Gemini图像生成"
        try:
            if hasattr(event, "message_obj") and getattr(event, "message_obj", None):
                sender_id = getattr(event.message_obj, "self_id", "0")
        except Exception as e:
            logger.debug(f"获取 sender_id 失败，使用默认值 '0'：{e}")

        node = Node(uin=sender_id, name=sender_name, content=node_content)

        async for res in self.safe_send(event, event.chain_result([node])):
            yield res

        log_thought_signature_debug(thought_signature, scene=f"{scene}/合并转发")
