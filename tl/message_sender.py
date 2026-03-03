"""消息格式化和发送模块"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Node, Plain

from .tl_utils import encode_file_to_base64
from .tl_utils import is_valid_base64_image_str as util_is_valid_base64_image_str

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

# 本地图片 base64 编码的默认大小阈值（2MB）
DEFAULT_MAX_INLINE_IMAGE_SIZE_BYTES = 2 * 1024 * 1024
# 超大文件警告阈值（10MB）
LARGE_FILE_WARNING_THRESHOLD_BYTES = 10 * 1024 * 1024


class MessageSender:
    """消息格式化和发送处理器"""

    def __init__(
        self,
        enable_text_response: bool = False,
        max_inline_image_size_mb: float = 2.0,
        log_debug_fn=None,
    ):
        """
        Args:
            enable_text_response: 是否启用文本响应
            max_inline_image_size_mb: 本地图片 base64 编码阈值（MB）
            log_debug_fn: 可选的日志函数
        """
        self.enable_text_response = enable_text_response
        self.max_inline_image_size_bytes = int(max_inline_image_size_mb * 1024 * 1024)
        self._log_debug = log_debug_fn or logger.debug

    def update_config(
        self,
        enable_text_response: bool | None = None,
        max_inline_image_size_mb: float | None = None,
    ):
        """更新配置"""
        if enable_text_response is not None:
            self.enable_text_response = enable_text_response
        if max_inline_image_size_mb is not None:
            self.max_inline_image_size_bytes = int(
                max_inline_image_size_mb * 1024 * 1024
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
        import re

        if not text:
            return text

        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = text.strip()

        return text

    @staticmethod
    def merge_available_images(
        image_urls: list[str] | None, image_paths: list[str] | None
    ) -> list[str]:
        """合并 URL 与路径，URL 优先，保持顺序并去重"""
        merged: list[str] = []
        seen: set[str] = set()

        # URL 优先，paths 作为补充
        for img in (image_urls or []) + (image_paths or []):
            if not img:
                continue
            if img in seen:
                continue
            seen.add(img)
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
    ):
        """
        根据内容数量选择发送模式：
        - 单图：链式富媒体发送（文本+图一起）
        - 总数<=4：链式富媒体发送（文本+多图一起）
        - 总数>4：合并转发
        """

        cleaned_text = self.clean_text_content(text_content) if text_content else ""
        text_to_send = (
            cleaned_text if (self.enable_text_response and cleaned_text) else ""
        )

        # 优先 URL，paths 作为补充（URL 在前，去重）
        available_images = self.merge_available_images(image_urls, image_paths)
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
            if thought_signature:
                logger.debug(f"思维签名: {thought_signature[:50]}...")
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

            if thought_signature:
                logger.debug(f"思维签名: {thought_signature[:50]}...")
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
            if thought_signature:
                logger.debug(f"思维签名: {thought_signature[:50]}...")
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

        if thought_signature:
            logger.debug(f"思维签名: {thought_signature[:50]}...")
