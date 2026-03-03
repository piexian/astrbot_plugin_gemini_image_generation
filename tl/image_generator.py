"""核心图像生成逻辑模块"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from .tl_api import APIError, ApiRequestConfig
from .tl_utils import send_file

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context

    from .tl_api import APIClient


class ImageGenerator:
    """核心图像生成处理器"""

    def __init__(
        self,
        context: Context,
        api_client: APIClient | None = None,
        model: str = "",
        api_type: str = "",
        api_base: str = "",
        resolution: str = "1K",
        aspect_ratio: str = "1:1",
        enable_grounding: bool = False,
        enable_smart_retry: bool = True,
        enable_text_response: bool = False,
        force_resolution: bool = False,
        resolution_param_name: str = "image_size",
        aspect_ratio_param_name: str = "aspect_ratio",
        max_reference_images: int = 6,
        total_timeout: int = 120,
        max_attempts_per_key: int = 3,
        nap_server_address: str = "localhost",
        nap_server_port: int = 3658,
        filter_valid_fn=None,
        get_tool_timeout_fn=None,
    ):
        """
        Args:
            context: AstrBot Context 实例
            api_client: API 客户端实例
            model: 模型名称
            api_type: API 类型
            api_base: API 基础地址
            resolution: 分辨率
            aspect_ratio: 宽高比
            enable_grounding: 是否启用 grounding
            enable_smart_retry: 是否启用智能重试
            enable_text_response: 是否启用文本响应
            force_resolution: 是否强制分辨率
            resolution_param_name: 分辨率参数名
            aspect_ratio_param_name: 宽高比参数名
            max_reference_images: 最大参考图片数
            total_timeout: 总超时时间
            max_attempts_per_key: 每个密钥最大尝试次数
            nap_server_address: NAP 服务器地址
            nap_server_port: NAP 服务器端口
            filter_valid_fn: 过滤有效参考图片的函数
            get_tool_timeout_fn: 获取工具超时的函数
        """
        self.context = context
        self.api_client = api_client
        self.model = model
        self.api_type = api_type
        self.api_base = api_base
        self.resolution = resolution
        self.aspect_ratio = aspect_ratio
        self.enable_grounding = enable_grounding
        self.enable_smart_retry = enable_smart_retry
        self.enable_text_response = enable_text_response
        self.force_resolution = force_resolution
        self.resolution_param_name = resolution_param_name
        self.aspect_ratio_param_name = aspect_ratio_param_name
        self.max_reference_images = max_reference_images
        self.total_timeout = total_timeout
        self.max_attempts_per_key = max_attempts_per_key
        self.nap_server_address = nap_server_address
        self.nap_server_port = nap_server_port
        self._filter_valid_fn = filter_valid_fn
        self._get_tool_timeout_fn = get_tool_timeout_fn

    def update_config(self, **kwargs):
        """更新配置"""
        for key, value in kwargs.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)

    def _filter_valid_reference_images(
        self, images: list[str] | None, source: str
    ) -> list[str]:
        """过滤有效参考图片"""
        if self._filter_valid_fn:
            return self._filter_valid_fn(images, source)
        return images or []

    def _get_tool_timeout(self, event: AstrMessageEvent | None = None) -> int:
        """获取工具超时"""
        if self._get_tool_timeout_fn:
            return self._get_tool_timeout_fn(event)
        return 60

    async def generate_image_core(
        self,
        event: AstrMessageEvent,
        prompt: str,
        reference_images: list[str],
        avatar_reference: list[str],
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
        is_tool_call: bool = False,
    ) -> tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
        """
        内部核心图像生成方法，不发送消息，只返回结果

        Returns:
            tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
            (是否成功, (图片URL列表, 图片路径列表, 文本内容, 思维签名) 或错误消息)
        """
        if not self.api_client:
            return False, (
                "❌ 无法生成图像：API 客户端尚未初始化。\n"
                "🧐 可能原因：服务启动过快，提供商尚未加载或 API 配置/密钥缺失。\n"
                "✅ 建议：先在配置文件中填写有效的 API 密钥并重启服务。"
            )

        valid_msg_images = self._filter_valid_reference_images(
            reference_images, source="消息图片"
        )
        valid_avatar_images = self._filter_valid_reference_images(
            avatar_reference, source="头像"
        )
        all_reference_images = valid_msg_images + valid_avatar_images

        if (
            all_reference_images
            and len(all_reference_images) > self.max_reference_images
        ):
            logger.warning(
                f"参考图片数量 ({len(all_reference_images)}) 超过限制 ({self.max_reference_images})，将截取前 {self.max_reference_images} 张"
            )
            all_reference_images = all_reference_images[: self.max_reference_images]

        # 计算截断后的数量
        final_msg_count = min(len(valid_msg_images), len(all_reference_images))
        final_avatar_count = len(all_reference_images) - final_msg_count

        if final_avatar_count > 0:
            prompt += f"""

[System Note]
The last {final_avatar_count} image(s) provided are User Avatars (marked as optional reference). You may use them for character consistency if needed, but they are NOT mandatory if they conflict with the requested style."""

        response_modalities = "TEXT_IMAGE" if self.enable_text_response else "IMAGE"
        effective_resolution = (
            override_resolution if override_resolution is not None else self.resolution
        )
        effective_aspect_ratio = (
            override_aspect_ratio
            if override_aspect_ratio is not None
            else self.aspect_ratio
        )
        request_config = ApiRequestConfig(
            model=self.model,
            prompt=prompt,
            api_type=self.api_type,
            api_base=self.api_base,
            resolution=effective_resolution,
            aspect_ratio=effective_aspect_ratio,
            enable_grounding=self.enable_grounding,
            response_modalities=response_modalities,
            reference_images=all_reference_images if all_reference_images else None,
            enable_smart_retry=self.enable_smart_retry,
            enable_text_response=self.enable_text_response,
            force_resolution=self.force_resolution,
            image_input_mode="force_base64",
            resolution_param_name=self.resolution_param_name,
            aspect_ratio_param_name=self.aspect_ratio_param_name,
        )

        logger.info("图像生成请求:")
        logger.info(f"  模型: {self.model}")
        logger.info(f"  API 类型: {self.api_type}")
        logger.info(
            f"  参考图片: {len(all_reference_images) if all_reference_images else 0} 张"
        )

        try:
            logger.info("开始调用API生成图像...")
            start_time = asyncio.get_running_loop().time()

            if is_tool_call:
                tool_timeout = self._get_tool_timeout(event)
                per_retry_timeout = min(self.total_timeout, tool_timeout)
                max_total_time = tool_timeout
            else:
                per_retry_timeout = self.total_timeout
                max_total_time = self.total_timeout
            logger.debug(
                f"超时配置: is_tool_call={is_tool_call}, per_retry_timeout={per_retry_timeout}s, max_retries={self.max_attempts_per_key}, max_total_time={max_total_time}s"
            )

            (
                image_urls,
                image_paths,
                text_content,
                thought_signature,
            ) = await self.api_client.generate_image(
                config=request_config,
                max_retries=self.max_attempts_per_key,
                per_retry_timeout=per_retry_timeout,
                max_total_time=max_total_time,
            )

            end_time = asyncio.get_running_loop().time()
            api_duration = end_time - start_time
            logger.info(f"API调用完成，耗时: {api_duration:.2f}秒")
            logger.info(
                f"🖼️ API 返回图片数量: {len(image_paths)}, URL 数量: {len(image_urls)}"
            )

            if thought_signature:
                logger.debug(f"思维签名: {thought_signature[:50]}...")

            resolved_paths: list[str] = []
            for idx, img_path in enumerate(image_paths):
                if not img_path:
                    continue
                if Path(img_path).exists():
                    resolved_path = img_path
                    if (
                        self.nap_server_address
                        and self.nap_server_address != "localhost"
                    ):
                        logger.debug(f"开始传输第 {idx + 1} 张图片到远程服务器...")
                        try:
                            remote_path = await asyncio.wait_for(
                                send_file(
                                    img_path,
                                    host=self.nap_server_address,
                                    port=self.nap_server_port,
                                ),
                                timeout=10.0,
                            )
                            if remote_path:
                                resolved_path = remote_path
                        except asyncio.TimeoutError:
                            logger.warning("文件传输超时，使用本地文件")
                        except Exception as e:
                            logger.warning(f"文件传输失败: {e}，将使用本地文件")
                    resolved_paths.append(resolved_path)
                else:
                    logger.warning(f"图像文件不存在或不可访问: {img_path}")
                    resolved_paths.append(img_path)

            image_paths = resolved_paths

            available_paths = [p for p in image_paths if p]
            available_urls = [u for u in image_urls if u]
            if available_paths or available_urls:
                logger.debug(
                    f"图像生成完成，准备返回结果，文件路径 {len(available_paths)} 张，URL {len(available_urls)} 张"
                )
                return True, (
                    image_urls,
                    image_paths,
                    text_content,
                    thought_signature,
                )

            error_msg = (
                "❌ 图像文件未找到，无法返回结果。\n"
                "🧐 可能原因：生成后保存文件失败，或远程传输路径无效。\n"
                "✅ 建议：检查临时目录写入权限与磁盘空间，必要时重试。"
            )
            logger.error(error_msg)
            return False, error_msg

        except APIError as e:
            status_part = (
                f"（状态码 {e.status_code}）" if e.status_code is not None else ""
            )
            error_msg = f"❌ 图像生成失败{status_part}：{e.message}"
            message_lower = (e.message or "").lower()
            api_base_lower = (self.api_base or "").lower()
            if e.error_type in ("timeout", "cancelled"):
                if is_tool_call:
                    error_msg += "\n🧐 可能原因：图像生成耗时超出框架工具调用限制。\n✅ 建议：在框架配置中增加 tool_call_timeout 到 90-120 秒，或简化提示词。"
                else:
                    error_msg += (
                        f"\n🧐 可能原因：图像生成耗时超出插件超时限制（当前 {self.total_timeout} 秒）。"
                        "\n✅ 建议：在插件配置中增加 total_timeout，或简化提示词/减少参考图。"
                    )
            elif e.error_type == "network":
                error_msg += "\n🧐 可能原因：网络连接异常或上游服务不可达。\n✅ 建议：检查网络连接和 API 地址配置，稍后重试。"
            elif e.status_code == 429:
                error_msg += "\n🧐 可能原因：请求过于频繁或额度已用完。\n✅ 建议：稍等片刻再试，或在配置中增加可用额度/开启智能重试。"
            elif e.status_code == 402:
                error_msg += "\n🧐 可能原因：账户余额不足或套餐到期。\n✅ 建议：充值或更换一组可用的 API 密钥后再试。"
            elif e.status_code == 403:
                error_msg += "\n🧐 可能原因：API 密钥无效、权限不足或访问受限。\n✅ 建议：核对密钥权限、检查 IP 白名单，必要时重新生成密钥。"
            elif e.status_code and 500 <= e.status_code < 600:
                error_msg += "\n🧐 可能原因：上游服务暂时不可用。\n✅ 建议：稍后重试，若频繁出现请联系服务提供方确认故障。"
                # t2i 公共服务繁忙提示
                if ("t2i" in message_lower) or ("t2i" in api_base_lower):
                    error_msg += (
                        "\n⚠️ t2i 公共服务器当前可能繁忙，建议稍后再试；"
                        "如需稳定产能可参考 https://docs.astrbot.app/others/self-host-t2i.html 自建。"
                    )
            else:
                error_msg += "\n🧐 可能原因：请求参数异常或服务返回未知错误。\n✅ 建议：简化提示词/减少参考图后重试，并查看日志获取更多细节。"
            logger.error(error_msg)
            return False, error_msg

        except Exception as e:
            logger.error(f"生成图像时发生未预期的错误: {e}", exc_info=True)
            return False, f"❌ 生成图像时发生错误: {str(e)}"
