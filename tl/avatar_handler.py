"""头像获取和管理模块"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger

from .tl_utils import AvatarManager, download_qq_avatar

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


# 头像相关关键词
AVATAR_KEYWORDS = (
    "头像",
    "按照我",
    "根据我",
    "基于我",
    "参考我",
    "我的",
    "avatar",
    "my face",
    "my photo",
    "像我",
    "本人",
)


class AvatarHandler:
    """头像获取和管理器"""

    def __init__(self, auto_avatar_reference: bool = False, log_debug_fn=None):
        """
        Args:
            auto_avatar_reference: 是否启用自动头像参考
            log_debug_fn: 可选的日志函数，用于调试输出
        """
        self.auto_avatar_reference = auto_avatar_reference
        self.avatar_manager = AvatarManager()
        self._log_debug = log_debug_fn or logger.debug

    def update_config(self, auto_avatar_reference: bool):
        """更新配置"""
        self.auto_avatar_reference = auto_avatar_reference

    async def get_avatar_reference(self, event: AstrMessageEvent) -> list[str]:
        """
        获取头像作为参考图像，支持用户头像（直接HTTP下载）

        Returns:
            头像图片的 base64 字符串列表
        """
        avatar_images: list[str] = []
        download_tasks: list[asyncio.Task | asyncio.Future] = []

        # 仅包裹群头像关键词解析，避免小错误影响后续头像获取
        if hasattr(event, "group_id") and event.group_id:
            try:
                group_id = str(event.group_id)
                prompt = (getattr(event, "message_str", "") or "").lower()

                group_avatar_keywords = [
                    "群头像",
                    "本群",
                    "我们的群",
                    "这个群",
                    "群标志",
                    "群图标",
                ]
                explicit_group_request = any(
                    keyword in prompt for keyword in group_avatar_keywords
                )

                should_get_group_avatar = explicit_group_request or (
                    self.auto_avatar_reference
                    and any(
                        keyword in prompt
                        for keyword in [
                            "生图",
                            "绘图",
                            "画图",
                            "生成图片",
                            "制作图片",
                            "改图",
                            "修改",
                        ]
                    )
                )

                if should_get_group_avatar:
                    if explicit_group_request:
                        logger.debug(
                            f"检测到明确的群头像关键词，准备获取群 {group_id} 的头像"
                        )
                    else:
                        logger.debug(
                            f"群聊中生图指令触发，自动获取群 {group_id} 的头像作为参考"
                        )
                    logger.debug("群头像功能暂未实现，跳过")
            except Exception as e:
                logger.debug(f"群头像关键词解析失败: {e}")

        # 获取头像：优先获取@用户头像，如果无@用户则获取发送者头像
        mentioned_users = await self.parse_mentions(event)

        if mentioned_users:
            for user_id in mentioned_users:
                logger.debug(f"获取@用户头像: {user_id}")
                download_tasks.append(
                    download_qq_avatar(
                        str(user_id), f"mentioned_{user_id}", event=event
                    )
                )
        else:
            if (
                hasattr(event, "message_obj")
                and hasattr(event.message_obj, "sender")
                and hasattr(event.message_obj.sender, "user_id")
            ):
                sender_id = str(event.message_obj.sender.user_id)
                logger.debug(f"获取发送者头像: {sender_id}")
                download_tasks.append(
                    download_qq_avatar(sender_id, f"sender_{sender_id}", event=event)
                )

        if download_tasks:
            logger.debug(f"开始并发下载 {len(download_tasks)} 个头像...")
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*download_tasks, return_exceptions=True),
                    timeout=8.0,
                )
                for result in results:
                    if isinstance(result, str) and result:
                        avatar_images.append(result)
                    elif isinstance(result, Exception):
                        logger.warning(f"头像下载任务失败: {result}")
                logger.debug(
                    f"头像下载完成，成功获取 {len(avatar_images)} 个头像，即将返回"
                )
            except asyncio.TimeoutError:
                logger.warning("头像下载总体超时，跳过剩余头像下载")
            except Exception as e:
                logger.warning(f"并发下载头像时发生错误: {e}")

        return avatar_images

    async def should_use_avatar(self, event: AstrMessageEvent) -> bool:
        """判断是否应该使用头像作为参考（只有在有@用户时才使用）"""
        logger.debug(f"检查 auto_avatar_reference: {self.auto_avatar_reference}")
        if not self.auto_avatar_reference:
            return False

        # 检查是否有@用户
        mentioned_users = await self.parse_mentions(event)
        logger.debug(f"@用户数量: {len(mentioned_users)}")

        # 只有当有@用户时才获取头像
        return len(mentioned_users) > 0

    async def should_use_avatar_for_prompt(
        self, event: AstrMessageEvent, prompt: str
    ) -> bool:
        """
        根据提示词判断是否应该使用头像作为参考
        只有当提示词明确包含头像相关关键词或有@用户时才获取头像
        """
        if not self.auto_avatar_reference:
            return False

        # 检查是否有@用户
        mentioned_users = await self.parse_mentions(event)
        if mentioned_users:
            logger.info(f"检测到@用户，启用头像参考: {len(mentioned_users)} 人")
            return True

        # 检查提示词是否包含头像关键词
        if self.prompt_contains_avatar_keywords(prompt):
            logger.info("提示词包含头像关键词，启用头像参考")
            return True

        logger.info("提示词不含头像关键词且无@用户，跳过头像获取")
        return False

    @staticmethod
    def prompt_contains_avatar_keywords(prompt: str) -> bool:
        """检查提示词中是否包含头像相关关键词"""
        if not prompt:
            return False
        prompt_lower = prompt.lower()
        return any(kw in prompt_lower for kw in AVATAR_KEYWORDS)

    async def parse_mentions(self, event: AstrMessageEvent) -> list[int]:
        """解析消息中的@用户，返回用户ID列表"""
        mentioned_users = []

        try:
            # 使用框架提供的方法获取消息组件
            messages = event.get_messages()

            for msg_component in messages:
                # 检查是否是@组件
                if hasattr(msg_component, "qq") and str(msg_component.qq) != str(
                    event.get_self_id()
                ):
                    mentioned_users.append(int(msg_component.qq))
                    self._log_debug(f"解析到@用户: {msg_component.qq}")

        except Exception as e:
            logger.warning(f"解析@用户失败: {e}")

        return mentioned_users
