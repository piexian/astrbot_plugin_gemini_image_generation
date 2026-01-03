"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mcp.types
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

if TYPE_CHECKING:
    from ..main import GeminiImageGenerationPlugin


def _make_text_result(text: str) -> mcp.types.CallToolResult:
    """构造文本结果"""
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)]
    )


def _read_image_as_base64(path: str) -> str | None:
    """读取图片文件并返回 base64 编码"""
    try:
        with open(path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.warning(f"读取图片文件失败: {path}, {e}")
        return None


def _get_mime_type(path: str) -> str:
    """根据文件扩展名获取 MIME 类型"""
    ext = Path(path).suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mime_map.get(ext, "image/png")


@dataclass
class GeminiImageGenerationTool(FunctionTool[AstrAgentContext]):
    """
    Gemini 图像生成工具

    当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。
    """

    name: str = "gemini_image_generation"
    description: str = (
        "使用 Gemini 模型生成或修改图像。"
        "当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "判断逻辑：用户说'改成'、'变成'、'基于'、'修改'、'改图'等词时，"
        "设置 use_reference_images=true；用户说'根据我'、'我的头像'或@某人时，"
        "设置 use_reference_images=true 和 include_user_avatar=true。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图像生成或修改的详细描述",
                },
                "use_reference_images": {
                    "type": "string",
                    "description": (
                        "是否使用上下文中的参考图片，true或false。"
                        "当用户意图是修改、变换或基于现有图片时设置为true"
                    ),
                    "default": "false",
                },
                "include_user_avatar": {
                    "type": "string",
                    "description": (
                        "是否包含用户头像作为参考图像，true或false。"
                        "当用户说'根据我'、'我的头像'或@某人时设置为true"
                    ),
                    "default": "false",
                },
            },
            "required": ["prompt"],
        }
    )

    # 插件实例引用（在创建时设置）
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """
        执行图像生成工具

        返回 mcp.types.CallToolResult，支持返回图片
        """
        prompt = kwargs.get("prompt", "")
        use_reference_images = kwargs.get("use_reference_images", "false")
        include_user_avatar = kwargs.get("include_user_avatar", "false")

        # 获取事件上下文
        event = context.context.event
        plugin = self.plugin

        if not plugin:
            return _make_text_result("❌ 工具未正确初始化，缺少插件实例引用。")

        # 检查限流
        allowed, limit_message = await plugin._check_and_consume_limit(event)
        if not allowed:
            return _make_text_result(limit_message or "请求过于频繁，请稍后再试。")

        if not plugin.api_client:
            return _make_text_result(
                "❌ 无法生成图像：API 客户端尚未初始化。\n"
                "🧐 可能原因：API 密钥未配置或加载失败。\n"
                "✅ 建议：在插件配置中填写有效密钥并重启服务。"
            )

        # 解析参数
        avatar_value = str(include_user_avatar).lower()
        logger.debug(f"include_user_avatar 参数: {avatar_value}")
        include_avatar = avatar_value in {"true", "1", "yes", "y", "是"}
        include_ref_images = str(use_reference_images).lower() in {
            "true",
            "1",
            "yes",
            "y",
            "是",
        }

        # 获取参考图片
        reference_images, avatar_reference = await plugin._fetch_images_from_event(
            event, include_at_avatars=include_avatar
        )

        if not include_ref_images:
            reference_images = []
        if not include_avatar:
            avatar_reference = []

        logger.info(
            f"[TOOL] 收集到参考图: 消息 {len(reference_images)} 张，"
            f"头像 {len(avatar_reference)} 张"
        )

        # 调用核心生成逻辑
        success, result_data = await plugin._generate_image_core_internal(
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
        )

        # 清理缓存
        try:
            await plugin.avatar_manager.cleanup_cache()
        except Exception as e:
            logger.warning(f"清理头像缓存失败: {e}")

        if success and isinstance(result_data, tuple):
            image_urls, image_paths, text_content, thought_signature = result_data

            # 构建返回内容
            contents: list[mcp.types.TextContent | mcp.types.ImageContent] = []

            # 添加文本内容
            text_parts = []
            if text_content:
                text_parts.append(text_content)
            if thought_signature:
                text_parts.append(thought_signature)
            if text_parts:
                contents.append(
                    mcp.types.TextContent(type="text", text="\n".join(text_parts))
                )

            # 添加图片内容 - 优先使用本地路径
            image_count = 0
            for img_path in image_paths or []:
                if not img_path:
                    continue
                # 处理本地文件
                if Path(img_path).exists():
                    b64_data = _read_image_as_base64(img_path)
                    if b64_data:
                        mime_type = _get_mime_type(img_path)
                        contents.append(
                            mcp.types.ImageContent(
                                type="image",
                                data=b64_data,
                                mimeType=mime_type,
                            )
                        )
                        image_count += 1

            # 如果没有从路径获取到图片，尝试使用 URL
            if image_count == 0 and image_urls:
                # URL 无法直接转为 ImageContent，返回文本提示
                url_text = "生成的图片:\n" + "\n".join(image_urls)
                contents.append(mcp.types.TextContent(type="text", text=url_text))

            if not contents:
                contents.append(
                    mcp.types.TextContent(type="text", text="✅ 图片已生成")
                )

            logger.info(f"[TOOL] 返回 {image_count} 张图片")
            return mcp.types.CallToolResult(content=contents)

        # 失败情况
        error_msg = result_data if isinstance(result_data, str) else "图像生成失败"
        return _make_text_result(error_msg)


# 保留旧的辅助函数以保持向后兼容（可在后续版本移除）
async def execute_image_generation_tool(
    plugin: GeminiImageGenerationPlugin,
    event: Any,
    prompt: str,
    use_reference_images: str = "false",
    include_user_avatar: str = "false",
) -> list[Any]:
    """
    执行图像生成工具的辅助函数

    已弃用：请使用 GeminiImageGenerationTool 类代替。
    此函数保留用于向后兼容 @filter.llm_tool 装饰器方式。
    """
    from astrbot.api.message_components import Image as AstrImage

    # 检查限流
    allowed, limit_message = await plugin._check_and_consume_limit(event)
    if not allowed:
        return [limit_message or "请求过于频繁，请稍后再试。"]

    if not plugin.api_client:
        return [
            "❌ 无法生成图像：API 客户端尚未初始化。\n"
            "🧐 可能原因：API 密钥未配置或加载失败。\n"
            "✅ 建议：在插件配置中填写有效密钥并重启服务。"
        ]

    # 解析参数
    avatar_value = str(include_user_avatar).lower()
    logger.debug(f"include_user_avatar 参数: {avatar_value}")
    include_avatar = avatar_value in {"true", "1", "yes", "y", "是"}
    include_ref_images = str(use_reference_images).lower() in {
        "true",
        "1",
        "yes",
        "y",
        "是",
    }

    # 获取参考图片
    reference_images, avatar_reference = await plugin._fetch_images_from_event(
        event, include_at_avatars=include_avatar
    )

    if not include_ref_images:
        reference_images = []
    if not include_avatar:
        avatar_reference = []

    logger.info(
        f"[TOOL] 收集到参考图: 消息 {len(reference_images)} 张，"
        f"头像 {len(avatar_reference)} 张"
    )

    # 调用核心生成逻辑
    success, result_data = await plugin._generate_image_core_internal(
        event=event,
        prompt=prompt,
        reference_images=reference_images,
        avatar_reference=avatar_reference,
    )

    # 清理缓存
    try:
        await plugin.avatar_manager.cleanup_cache()
    except Exception as e:
        logger.warning(f"清理头像缓存失败: {e}")

    if success and isinstance(result_data, tuple):
        image_urls, image_paths, text_content, thought_signature = result_data

        results: list[Any] = []
        if text_content:
            results.append(text_content)
        if thought_signature:
            results.append(thought_signature)

        # 添加图片
        for img_path in image_paths or []:
            if img_path and Path(img_path).exists():
                results.append(AstrImage.fromFileSystem(img_path))

        # 如果没有本地图片，使用 URL
        if not any(isinstance(r, AstrImage) for r in results):
            for url in image_urls or []:
                if url:
                    results.append(AstrImage(file=url))

        return results if results else ["✅ 图片已生成"]

    # 失败情况
    error_msg = result_data if isinstance(result_data, str) else "图像生成失败"
    return [error_msg]
