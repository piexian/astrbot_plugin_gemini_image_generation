"""图片获取、过滤和转换模块"""

from __future__ import annotations

import base64
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import At, Image, Reply

from .tl_utils import AvatarManager, encode_file_to_base64
from .tl_utils import is_valid_base64_image_str as util_is_valid_base64_image_str

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .tl_api import APIClient


class ImageHandler:
    """图片获取、过滤和转换处理器"""

    def __init__(
        self,
        api_client: APIClient | None = None,
        max_reference_images: int = 6,
        log_debug_fn=None,
    ):
        """
        Args:
            api_client: API 客户端实例（用于图片转换）
            max_reference_images: 最大参考图片数量
            log_debug_fn: 可选的日志函数
        """
        self.api_client = api_client
        self.max_reference_images = max_reference_images
        self.avatar_manager = AvatarManager()
        self._log_debug = log_debug_fn or logger.debug

    def update_config(
        self,
        api_client: APIClient | None = None,
        max_reference_images: int | None = None,
    ):
        """更新配置"""
        if api_client is not None:
            self.api_client = api_client
        if max_reference_images is not None:
            self.max_reference_images = max_reference_images

    @staticmethod
    def is_valid_base64_image_str(value: str) -> bool:
        """委托统一的工具方法判断 base64 图像有效性"""
        return util_is_valid_base64_image_str(value)

    def filter_valid_reference_images(
        self, images: list[str] | None, source: str
    ) -> list[str]:
        """
        过滤出合法的参考图像。

        支持的格式：
        - http(s):// URL（由 tl_api 下载转换）
        - data:image/xxx;base64,... 格式
        - 纯 base64 字符串（需通过魔数校验为图片）

        NapCat 等平台的图片 file_id（例如 D127D0...jpg）会在这里被过滤掉，
        避免传给 Gemini 导致 Base64 解码错误。
        """
        if not images:
            return []

        valid: list[str] = []
        for img in images:
            if not isinstance(img, str) or not img:
                self._log_debug(f"跳过非字符串参考图像({source}): {type(img)}")
                continue

            cleaned = img.strip()

            # URL 形式的图片，交给 tl_api 下载处理
            if cleaned.lower().startswith("http://") or cleaned.lower().startswith(
                "https://"
            ):
                valid.append(cleaned)
                self._log_debug(f"保留 URL 参考图像({source}): {cleaned[:64]}...")
                continue

            if self.is_valid_base64_image_str(cleaned):
                valid.append(cleaned)
            elif cleaned.lower().startswith("data:image/") and ";base64," in cleaned:
                valid.append(cleaned)
            else:
                self._log_debug(f"跳过非支持格式参考图像({source}): {cleaned[:64]}...")

        return valid

    async def download_qq_image(
        self, url: str, event: AstrMessageEvent | None = None
    ) -> str | None:
        """对QQ图床/nt.qq.com做特殊处理，优先通过适配器取二进制，失败再走HTTP"""
        try:
            parsed = urllib.parse.urlparse(url)

            # 优先使用适配器API拉取原始图片，避免直链失效
            try:

                async def _call_get_image(client, **kwargs):
                    # 兼容 client.api.call_action 和 client.call_action 两种写法
                    if hasattr(client, "api") and hasattr(client.api, "call_action"):
                        return await client.api.call_action("get_image", **kwargs)
                    if hasattr(client, "call_action"):
                        return await client.call_action("get_image", **kwargs)
                    return None

                bot_client = getattr(event, "bot", None) if event else None

                if bot_client:
                    # 先尝试完整URL
                    resp = await _call_get_image(bot_client, file=url)
                    if isinstance(resp, dict):
                        if resp.get("base64"):
                            return resp["base64"]
                        if resp.get("url"):
                            return resp["url"]
                        if resp.get("file") and Path(resp["file"]).exists():
                            mime_guess = f"image/{Path(resp['file']).suffix.lstrip('.') or 'png'}"
                            data = encode_file_to_base64(resp["file"])
                            return f"data:{mime_guess};base64,{data}"

                    file_id = None
                    qs = urllib.parse.parse_qs(parsed.query or "")
                    if "fileid" in qs and qs["fileid"]:
                        file_id = qs["fileid"][0]
                    if not file_id:
                        file_id = parsed.path.rsplit("/", 1)[-1]

                    if file_id:
                        resp = await _call_get_image(
                            bot_client, file_id=file_id, file=file_id
                        )
                        if isinstance(resp, dict):
                            if resp.get("base64"):
                                return resp["base64"]
                            if resp.get("url"):
                                return resp["url"]
                            if resp.get("file") and Path(resp["file"]).exists():
                                mime_guess = f"image/{Path(resp['file']).suffix.lstrip('.') or 'png'}"
                                data = encode_file_to_base64(resp["file"])
                                return f"data:{mime_guess};base64,{data}"

            except Exception as e:
                logger.debug(f"适配器获取 nt.qq/qpic 图片失败，回退HTTP: {e}")

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Connection": "keep-alive",
            }
            if parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"
            if "qpic.cn" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
            if "nt.qq.com" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
                headers["Origin"] = "https://qun.qq.com"

            timeout = aiohttp.ClientTimeout(total=12, connect=5)

            async def _http_fetch(target_url: str) -> str | None:
                try:
                    async with aiohttp.ClientSession(
                        headers=headers, trust_env=True
                    ) as session:
                        async with session.get(target_url, timeout=timeout) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    f"QQ图片下载失败: HTTP {resp.status} {resp.reason} | {target_url[:80]}"
                                )
                                return None
                            data = await resp.read()
                            if not data:
                                logger.warning(f"QQ图片为空: {target_url[:80]}")
                                return None
                            mime = resp.headers.get("Content-Type", "image/jpeg")
                            if ";" in mime:
                                mime = mime.split(";", 1)[0]
                            base64_data = base64.b64encode(data).decode("utf-8")
                            return f"data:{mime};base64,{base64_data}"
                except Exception as e:
                    logger.warning(f"QQ图片下载异常: {e} | {target_url[:80]}")
                    return None

            # 先 https，失败再试 http
            data_url = await _http_fetch(url)
            if not data_url and url.startswith("https://"):
                data_url = await _http_fetch("http://" + url[len("https://") :])
            return data_url
        except Exception as e:
            logger.warning(f"QQ图片下载异常: {e} | {url[:80]}")
            return None

    async def fetch_images_from_event(
        self, event: AstrMessageEvent, include_at_avatars: bool = False
    ) -> tuple[list[str], list[str]]:
        """
        综合提取事件中的图片：当前消息、引用消息及手动@用户头像

        返回 (消息/引用图片, 头像图片)
        """
        message_images: list[str] = []
        avatar_images: list[str] = []
        seen_sources: set[str] = set()
        seen_users: set[str] = set()
        conversion_cache: dict[str, str] = {}
        max_images = self.max_reference_images

        if not hasattr(event, "message_obj") or not event.message_obj:
            return message_images, avatar_images

        try:
            message_chain = event.get_messages()
        except Exception:
            message_chain = getattr(event.message_obj, "message", []) or []

        if not message_chain:
            return message_images, avatar_images

        self_id = None
        try:
            self_id = str(event.get_self_id())
        except Exception:
            try:
                self_id = str(getattr(event.message_obj, "self_id", None))
            except Exception:
                self_id = None

        def _is_auto_at(comp: At) -> bool:
            """区分自动@，兼容多种属性命名"""
            flags = [
                getattr(comp, "is_auto", None),
                getattr(comp, "auto", None),
                getattr(comp, "auto_at", None),
                getattr(comp, "autoAt", None),
            ]
            for flag in flags:
                if isinstance(flag, str):
                    flag_val = flag.lower() in {"true", "1", "yes", "y"}
                else:
                    flag_val = bool(flag)
                if flag_val:
                    return True
            return False

        async def convert_image_source(img_source: str, origin: str) -> str | None:
            """
            统一转换图片源为 base64（不再透传 URL）
            """
            if not img_source:
                return None
            if img_source in conversion_cache:
                return conversion_cache[img_source]

            source_str = str(img_source).strip()
            if not source_str:
                return None

            parsed_host = ""
            try:
                parsed_host = urllib.parse.urlparse(source_str).netloc or ""
            except Exception:
                parsed_host = ""

            force_b64 = True

            def _extract_base64_only(val: str) -> str | None:
                """提取纯 base64 数据，剥离 data URL 前缀"""
                try:
                    if ";base64," in val:
                        _, _, b64_part = val.partition(";base64,")
                        base64.b64decode(b64_part, validate=True)
                        return b64_part
                    base64.b64decode(val, validate=True)
                    return val
                except Exception:
                    return None

            # 直接返回已是 base64/data URL 的输入
            if self.is_valid_base64_image_str(source_str):
                b64 = _extract_base64_only(source_str) if force_b64 else source_str
                if b64:
                    conversion_cache[img_source] = b64
                    return b64

            async def to_data_url(candidate: str) -> str | None:
                """统一转为 base64"""
                try:
                    if not self.api_client:
                        logger.warning("API 客户端未初始化，无法转换图片为base64")
                        return None
                    (
                        mime_type,
                        base64_data,
                    ) = await self.api_client._normalize_image_input(
                        candidate, image_input_mode="force_base64"
                    )
                    if base64_data:
                        data_url = (
                            base64_data
                            if force_b64
                            else (
                                f"data:{mime_type};base64,{base64_data}"
                                if mime_type
                                else base64_data
                            )
                        )
                        conversion_cache[img_source] = data_url
                        return data_url
                    logger.debug(
                        f"跳过无法识别的图片源({origin}): {str(candidate)[:80]}..."
                    )
                except Exception as e:
                    logger.warning(
                        f"转换图片为base64失败({origin}): {repr(e)} | Source: {str(candidate)[:80]}"
                    )
                return None

            # QQ 图床优先转 base64，避免直链失效（含 nt.qq.com）
            if parsed_host and ("qpic.cn" in parsed_host or "nt.qq.com" in parsed_host):
                qq_data = await self.download_qq_image(source_str, event=event)
                if qq_data:
                    if force_b64 and ";base64," in qq_data:
                        qq_data = qq_data.split(";base64,", 1)[1]
                    conversion_cache[img_source] = qq_data
                    return qq_data
                logger.warning(f"QQ图片直链处理失败，尝试通用流程: {source_str[:80]}")
                fallback = await to_data_url(source_str)
                if fallback:
                    return fallback
                # force_base64 模式直接放弃
                return None

            # 统一转 base64，所有参考图不再直接透传 URL
            return await to_data_url(source_str)

        async def handle_image_component(component, origin: str):
            if len(message_images) >= max_images:
                return

            force_b64 = True
            img_source = None
            if isinstance(component, Image):
                if getattr(component, "url", None):
                    img_source = component.url
                elif getattr(component, "file", None):
                    img_source = component.file
            else:
                if getattr(component, "url", None):
                    img_source = component.url
                elif getattr(component, "file", None):
                    img_source = component.file

            if not img_source:
                return

            if img_source in seen_sources:
                self._log_debug(f"跳过重复图片源({origin}): {str(img_source)[:120]}")
                return

            seen_sources.add(img_source)

            # 优先使用组件自带的 base64 转换能力，避免依赖直链截断
            if hasattr(component, "convert_to_base64"):
                try:
                    raw_b64 = await component.convert_to_base64()
                    if raw_b64:
                        ref_val = (
                            raw_b64
                            if force_b64
                            else f"data:image/jpeg;base64,{raw_b64}"
                        )
                        check_val = raw_b64 if force_b64 else ref_val
                        if self.is_valid_base64_image_str(check_val):
                            conversion_cache[img_source] = ref_val
                            message_images.append(ref_val)
                            self._log_debug(
                                f"✓ 直接从消息组件转换图片 (当前: {len(message_images)}/{max_images})"
                            )
                            return
                        self._log_debug(
                            f"组件转换结果未通过校验，尝试通用流程({origin})"
                        )
                except Exception as e:
                    logger.debug(f"组件 convert_to_base64 异常，回退通用流程: {e}")

            ref_img = await convert_image_source(str(img_source), origin)
            if ref_img:
                message_images.append(ref_img)
                self._log_debug(
                    f"✓ 从{origin}提取图片 (当前: {len(message_images)}/{max_images})"
                )

        async def handle_at_component(component: At, origin: str):
            if not include_at_avatars:
                return

            if _is_auto_at(component):
                self._log_debug(f"跳过自动@用户（{origin}）")
                return

            user_id = getattr(component, "qq", None) or getattr(
                component, "user_id", None
            )
            if not user_id:
                return

            user_id = str(user_id)
            if self_id and user_id == self_id:
                return
            if user_id in seen_users:
                return

            avatar_b64 = await self.avatar_manager.get_avatar(
                user_id, f"at_{user_id}", event=event
            )
            if avatar_b64:
                avatar_images.append(avatar_b64)
                seen_users.add(user_id)
                self._log_debug(f"✓ 获取@用户头像({origin}): {user_id}")
            else:
                self._log_debug(f"✗ 获取@用户头像失败({origin}): {user_id}")

        # 当前消息体处理
        for component in message_chain:
            try:
                if isinstance(component, Image):
                    await handle_image_component(component, "当前消息")
                elif isinstance(component, At):
                    await handle_at_component(component, "当前消息")
                elif isinstance(component, Reply) and component.chain:
                    for reply_comp in component.chain:
                        if isinstance(reply_comp, Image):
                            await handle_image_component(reply_comp, "引用消息")
                        elif isinstance(reply_comp, At):
                            await handle_at_component(reply_comp, "引用消息")
            except Exception as e:
                logger.warning(f"处理消息组件异常: {e}")

        # 如果需要头像但没有@，尝试回退到发送者头像
        if include_at_avatars and not avatar_images:
            try:
                sender_id = None
                if hasattr(event, "message_obj") and hasattr(
                    event.message_obj, "sender"
                ):
                    sender = event.message_obj.sender
                    sender_id = getattr(sender, "user_id", None) or getattr(
                        sender, "userId", None
                    )
                if sender_id and str(sender_id) not in seen_users:
                    sender_id = str(sender_id)
                    avatar_b64 = await self.avatar_manager.get_avatar(
                        sender_id, f"sender_{sender_id}", event=event
                    )
                    if avatar_b64:
                        avatar_images.append(avatar_b64)
                        seen_users.add(sender_id)
                        self._log_debug(f"✓ 回退获取发送者头像: {sender_id}")
            except Exception as e:
                logger.debug(f"回退获取发送者头像失败: {e}")

        # 截断数量，优先保留消息图片，再补充头像
        if len(message_images) > max_images:
            message_images = message_images[:max_images]
        remaining_slots = max(max_images - len(message_images), 0)
        if len(avatar_images) > remaining_slots:
            avatar_images = avatar_images[:remaining_slots]

        if message_images or avatar_images:
            logger.info(
                f"已收集图片: 消息 {len(message_images)} 张，头像 {len(avatar_images)} 张"
            )
        else:
            logger.info("未收集到有效参考图片，若需参考图可直接发送图片或检查网络权限")

        return message_images, avatar_images

    @staticmethod
    def clean_text_content(text: str) -> str:
        """清理文本内容，移除 markdown 图片链接等不可发送的内容"""
        if not text:
            return text

        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = text.strip()

        return text
