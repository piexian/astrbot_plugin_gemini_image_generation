"""
工具函数模块
提供头像管理、文件传输和图像处理功能
"""

import asyncio
import base64
import binascii
import hashlib
import io
import os
import re
import struct
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiohttp
import cv2
from PIL import Image as PILImage

from astrbot.api import logger

SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
)

# QQ 域名集合，用于识别 QQ 图片服务器
QQ_IMAGE_HOSTS = frozenset({"qpic.cn", "qq.com", "nt.qq.com", "gchat.qpic.cn"})


def _is_qq_host(host: str) -> bool:
    """检查是否为 QQ 图片服务器域名"""
    if not host:
        return False
    host_lower = host.lower()
    return any(qq_host in host_lower for qq_host in QQ_IMAGE_HOSTS)


def _build_http_headers(host: str = "", for_qq: bool = False) -> dict[str, str]:
    """
    构建 HTTP 请求头

    Args:
        host: 目标主机名
        for_qq: 是否为 QQ 图片服务器优化
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if host:
        scheme = "https" if not for_qq else "http"
        headers["Referer"] = f"{scheme}://{host}"

    if for_qq or _is_qq_host(host):
        headers["Referer"] = "https://qun.qq.com"
        headers["Origin"] = "https://qun.qq.com"
        if ",image/png" not in headers["Accept"]:
            headers["Accept"] += ",image/png"

    return headers


def _check_image_cache(url: str, cache_dir: Path) -> Path | None:
    """
    检查 URL 对应的本地缓存是否存在

    Args:
        url: 图片 URL
        cache_dir: 缓存目录

    Returns:
        缓存文件路径，不存在返回 None
    """
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = next(
            (
                p
                for p in cache_dir.glob(f"{cache_key}.*")
                if p.exists() and p.stat().st_size > 0
            ),
            None,
        )
        return cached
    except Exception as e:
        logger.debug(f"检查缓存失败: {e}")
        return None


def _save_to_cache(
    url: str, data: bytes, mime_type: str, cache_dir: Path
) -> Path | None:
    """
    将图片数据保存到缓存

    Args:
        url: 图片 URL
        data: 图片数据
        mime_type: MIME 类型
        cache_dir: 缓存目录

    Returns:
        缓存文件路径，失败返回 None
    """
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        suffix = mime_type.split("/")[-1] if "/" in mime_type else "png"
        cache_file = cache_dir / f"{cache_key}.{suffix}"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        return cache_file
    except Exception as e:
        logger.debug(f"写入缓存失败: {e}")
        return None


def _decode_base64_to_temp_file(
    b64_data: str, *, verify_image: bool = True, logger_obj=logger
) -> str | None:
    """
    将 base64 数据解码并保存到临时文件

    Args:
        b64_data: base64 数据（可以是 data URI 或纯 base64）
        verify_image: 是否验证图片可读性
        logger_obj: 日志对象

    Returns:
        临时文件路径，失败返回 None
    """
    try:
        data = b64_data
        if ";base64," in data:
            _, _, data = data.partition(";base64,")
        raw_bytes = base64.b64decode(data)
        tmp_path = Path("/tmp") / f"cut_{int(time.time() * 1000)}.png"
        tmp_path.write_bytes(raw_bytes)

        if verify_image and cv2.imread(str(tmp_path)) is None:
            logger_obj.debug("base64 解码后图片不可读")
            tmp_path.unlink(missing_ok=True)
            return None

        return str(tmp_path)
    except Exception as e:
        logger_obj.debug(f"base64 解码到临时文件失败: {e}")
        return None


def get_plugin_data_dir() -> Path:
    """获取插件数据目录"""
    # 使用AstrBot的StarTools获取标准数据目录
    from astrbot.api.star import StarTools

    return StarTools.get_data_dir("astrbot_plugin_gemini_image_generation")


# 下载缓存目录
IMAGE_CACHE_DIR = get_plugin_data_dir() / "images" / "download_cache"


def _build_image_path(
    image_format: str = "png", prefix: str = "gemini_advanced_image"
) -> Path:
    """生成规范的图片路径，避免重复逻辑"""
    images_dir = get_plugin_data_dir() / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    unique_suffix = uuid4().hex[:6]
    filename = f"{prefix}_{timestamp}_{unique_suffix}.{image_format}"
    return images_dir / filename


def _pick_avatar_url(data: dict | None) -> str | None:
    """尝试从不同字段中提取头像URL"""
    if not isinstance(data, dict):
        return None

    candidate_keys = [
        "avatar",
        "avatar_url",
        "user_avatar",
        "head_image",
        "tiny_avatar",
        "thumb_avatar",
        "url",
    ]
    for key in candidate_keys:
        url = data.get(key)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url

    inner = data.get("data")
    if isinstance(inner, dict):
        return _pick_avatar_url(inner)

    return None


def _encode_file_to_base64(file_path: Path, chunk_size: int = 65536) -> str:
    """流式编码文件为base64
    注意: chunk_size 必须是 3 的倍数，否则 base64 编码会出错
    """
    # 确保 chunk_size 是 3 的倍数
    chunk_size = (chunk_size // 3) * 3
    if chunk_size == 0:
        chunk_size = 3

    encoded_parts: list[str] = []
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            encoded_parts.append(base64.b64encode(chunk).decode("utf-8"))
    return "".join(encoded_parts)


def encode_file_to_base64(file_path: str | Path, chunk_size: int = 65536) -> str:
    """对外暴露的编码方法，兼容字符串路径"""
    return _encode_file_to_base64(Path(file_path), chunk_size)


async def save_image_stream(
    stream_reader,
    image_format: str = "png",
    target_path: Path | None = None,
) -> str | None:
    """
    将异步流式读取到的图片保存到文件，避免一次性加载到内存

    Args:
        stream_reader: aiohttp.StreamReader 或任意异步可迭代的字节流
        image_format: 图片格式
        target_path: 指定文件路径，便于缓存复用
    """
    file_path = target_path or _build_image_path(image_format)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb") as f:
            if hasattr(stream_reader, "iter_chunked"):
                async for chunk in stream_reader.iter_chunked(8192):
                    if chunk:
                        f.write(chunk)
            else:
                async for chunk in stream_reader:
                    if chunk:
                        f.write(chunk)

        logger.debug(f"图像已流式保存: {file_path}")
        return str(file_path)
    except Exception as e:
        logger.error(f"流式保存图像失败: {e}")
        return None


# base64 图片去重缓存（base64_hash -> file_path）
_base64_image_cache: dict[str, str] = {}


async def save_base64_image(base64_data: str, image_format: str = "png") -> str | None:
    """
    保存base64图像数据到文件

    Args:
        base64_data: base64编码的图像数据
        image_format: 图像格式 (png, jpg, jpeg等)

    Returns:
        保存的文件路径，失败返回None；若已保存过相同数据则返回现有路径
    """

    # 去掉空白后计算哈希，用于去重
    cleaned_data = "".join(base64_data.split())
    data_hash = hashlib.md5(cleaned_data.encode()).hexdigest()

    # 检查是否已保存过相同的数据
    if data_hash in _base64_image_cache:
        existing_path = _base64_image_cache[data_hash]
        # 检查文件是否还存在
        if Path(existing_path).exists():
            logger.debug(f"复用已保存的图片: {existing_path}")
            return existing_path
        else:
            # 文件已被删除，从缓存中移除
            del _base64_image_cache[data_hash]

    try:
        file_path = _build_image_path(image_format)

        # 去掉空白并按块解码，避免一次性占用过大内存
        # 一次性解码完整数据，若失败则宽松清洗后再试
        try:
            raw = base64.b64decode(cleaned_data, validate=False)
        except Exception:
            import re

            relaxed = re.sub(r"[^A-Za-z0-9+/=_-]", "", cleaned_data)
            pad_len = (-len(relaxed)) % 4
            if pad_len:
                relaxed += "=" * pad_len
            raw = base64.b64decode(relaxed, validate=False)

        with open(file_path, "wb") as f:
            f.write(raw)

        # 加入缓存，避免重复保存相同数据
        _base64_image_cache[data_hash] = str(file_path)

        logger.debug(f"图像已保存: {file_path}")
        return str(file_path)

    except Exception as e:
        logger.error(f"保存图像失败: {e}")
        return None


async def save_image_data(image_data: bytes, image_format: str = "png") -> str | None:
    """
    保存图像字节数据到文件

    Args:
        image_data: 图像字节数据
        image_format: 图像格式

    Returns:
        保存的文件路径，失败返回None
    """
    try:
        file_path = _build_image_path(image_format)
        with open(file_path, "wb") as f:
            f.write(image_data)

        logger.debug(f"图像已保存: {file_path}")
        return str(file_path)

    except Exception as e:
        logger.error(f"保存图像失败: {e}")
        return None


async def cleanup_old_images(
    images_dir: Path | None = None, ttl_minutes: int = 5, max_files: int = 100
):
    """
    清理超过指定时间的图像文件和缓存，并限制文件数量

    Args:
        images_dir (Path): images 目录路径，如果为None则使用默认路径
        ttl_minutes (int): 缓存保留时间（分钟），默认 5 分钟，设为 0 表示不按时间清理
        max_files (int): 各目录文件数量上限，默认 100，设为 0 表示不限制数量
    """
    # 如果 TTL 和数量限制都为 0，不执行清理
    if ttl_minutes <= 0 and max_files <= 0:
        return

    try:
        plugin_data_dir = get_plugin_data_dir()
        if images_dir is None:
            images_dir = plugin_data_dir / "images"

        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=ttl_minutes)
        cleaned_count = 0

        # 清理 images 目录
        if images_dir.exists():
            image_patterns = [
                "gemini_image_*.*",
                "gemini_advanced_image_*.*",
                "help_*.png",
            ]
            all_files: list[Path] = []
            for pattern in image_patterns:
                all_files.extend(images_dir.glob(pattern))

            # 按修改时间排序（最旧的在前面）
            all_files.sort(key=lambda f: f.stat().st_mtime)

            for idx, file_path in enumerate(all_files):
                try:
                    should_delete = False
                    # 按时间清理
                    if ttl_minutes > 0:
                        if (
                            datetime.fromtimestamp(file_path.stat().st_mtime)
                            < cutoff_time
                        ):
                            should_delete = True
                    # 按数量清理（保留最新的 max_files 个）
                    if max_files > 0 and len(all_files) - idx > max_files:
                        should_delete = True

                    if should_delete:
                        file_path.unlink()
                        cleaned_count += 1
                except Exception as e:
                    logger.warning(f"清理文件 {file_path} 时出错: {e}")

        # 清理 download_cache 目录
        cache_dir = (
            images_dir / "download_cache"
            if images_dir
            else plugin_data_dir / "images" / "download_cache"
        )
        if cache_dir.exists():
            cache_files = [f for f in cache_dir.glob("*") if f.is_file()]
            cache_files.sort(key=lambda f: f.stat().st_mtime)

            for idx, file_path in enumerate(cache_files):
                try:
                    should_delete = False
                    if ttl_minutes > 0:
                        if (
                            datetime.fromtimestamp(file_path.stat().st_mtime)
                            < cutoff_time
                        ):
                            should_delete = True
                    if max_files > 0 and len(cache_files) - idx > max_files:
                        should_delete = True

                    if should_delete:
                        file_path.unlink()
                        cleaned_count += 1
                except Exception as e:
                    logger.warning(f"清理缓存 {file_path} 时出错: {e}")

        # 清理 split_output 目录
        split_dir = plugin_data_dir / "split_output"
        if split_dir.exists():
            split_files = [f for f in split_dir.glob("*") if f.is_file()]
            split_files.sort(key=lambda f: f.stat().st_mtime)

            for idx, file_path in enumerate(split_files):
                try:
                    should_delete = False
                    if ttl_minutes > 0:
                        if (
                            datetime.fromtimestamp(file_path.stat().st_mtime)
                            < cutoff_time
                        ):
                            should_delete = True
                    if max_files > 0 and len(split_files) - idx > max_files:
                        should_delete = True

                    if should_delete:
                        file_path.unlink()
                        cleaned_count += 1
                except Exception as e:
                    logger.warning(f"清理切图 {file_path} 时出错: {e}")

        if cleaned_count > 0:
            logger.debug(f"共清理 {cleaned_count} 个过期文件")

    except Exception as e:
        logger.error(f"清理过程出错: {e}")


async def download_qq_avatar(
    user_id: str,
    cache_name: str,
    images_dir: Path | None = None,
    event=None,
) -> str | None:
    """
    下载QQ头像并转换为base64格式，优先使用NapCat事件系统获取头像URL

    Args:
        user_id (str): QQ用户ID
        cache_name (str): 缓存文件名前缀
        images_dir (Path): images目录路径，如果为None则使用默认路径
        event: AstrMessageEvent，便于通过事件携带的 bot/原始消息提取头像URL

    Returns:
        str: base64格式的头像数据，失败返回None
    """

    async def _resolve_avatar_url() -> str | None:
        """通过事件上下文或NapCat接口解析头像URL"""
        # 1. 原始消息
        try:
            raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
            sender_raw = getattr(raw_msg, "sender", None) or (
                raw_msg.get("sender") if isinstance(raw_msg, dict) else None
            )
            url = _pick_avatar_url(sender_raw)
            if url:
                logger.debug(f"从原始消息获取到头像URL: {url}")
                return url
        except Exception as e:
            logger.debug(f"从原始消息提取头像失败: {e}")

        # 2. sender 对象
        try:
            sender_obj = getattr(getattr(event, "message_obj", None), "sender", None)
            url = _pick_avatar_url(sender_obj.__dict__ if sender_obj else None)
            if url:
                logger.debug("从 sender 对象提取到头像URL")
                return url
        except Exception:
            pass

        # 3. NapCat / OneBot API
        bot = getattr(event, "bot", None) or getattr(event, "_bot", None)
        if bot:
            group_id = None
            try:
                raw_group_id = (
                    getattr(event, "group_id", None)
                    or getattr(event, "get_group_id", lambda: None)()
                )
                group_id = int(raw_group_id) if raw_group_id else None
            except Exception:
                group_id = None

            actions: list[tuple[str, dict]] = []
            actions.append(("get_avatar", {"user_id": int(user_id), "img_type": "640"}))
            actions.append(
                ("get_user_avatar", {"user_id": int(user_id), "img_type": "640"})
            )
            if group_id:
                actions.append(
                    (
                        "get_group_member_info",
                        {
                            "group_id": group_id,
                            "user_id": int(user_id),
                            "no_cache": False,
                        },
                    )
                )
            actions.append(
                ("get_stranger_info", {"user_id": int(user_id), "no_cache": False})
            )

            napcat_unsupported = False

            for action, payload in actions:
                if napcat_unsupported:
                    break
                try:
                    resp = await bot.call_action(action, **payload)
                    url = None
                    if isinstance(resp, str) and resp.startswith(
                        ("http://", "https://")
                    ):
                        url = resp
                    elif isinstance(resp, dict):
                        url = _pick_avatar_url(resp)
                    if url:
                        logger.debug(f"通过 {action} 获取头像URL成功: {url}")
                        return url
                except Exception as e:
                    err_text = str(e)
                    logger.debug(f"调用 {action} 获取头像失败: {err_text}")
                    # 对于不支持的接口，避免继续尝试其他 NapCat API，直接走直链回退
                    if getattr(e, "retcode", None) == 1404 or "不支持的Api" in err_text:
                        napcat_unsupported = True

        logger.warning(f"无法通过事件系统获取用户 {user_id} 的头像URL")
        return None

    try:
        if images_dir is None:
            images_dir = get_plugin_data_dir() / "images"

        avatar_url = await _resolve_avatar_url()
        if not avatar_url:
            # 回退使用 qlogo 直链（使用 HTTP 协议，更稳定）
            avatar_url = f"http://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
            logger.debug(f"未从事件获取头像URL，回退 qlogo: {avatar_url}")
        else:
            # 将获取到的 URL 转为 HTTP 协议
            avatar_url = avatar_url.replace("https://", "http://")

        timeout = aiohttp.ClientTimeout(total=12, connect=5)
        max_retries = 3
        retry_interval = 1.0
        async with aiohttp.ClientSession() as session:
            for attempt in range(1, max_retries + 1):
                try:
                    async with session.get(
                        avatar_url,
                        timeout=timeout,
                    ) as response:
                        if response.status != 200:
                            logger.warning(
                                f"下载头像失败: HTTP {response.status} "
                                f"{response.reason} (尝试 {attempt}/{max_retries})"
                            )
                            if attempt < max_retries:
                                await asyncio.sleep(retry_interval * attempt)
                                continue
                            return None

                        data = await response.read()
                        if not data or len(data) <= 1000:
                            logger.warning(
                                f"用户 {user_id} 头像可能为空或默认头像，文件过小，放弃"
                            )
                            return None

                        # 尝试从响应头猜测 mime
                        mime_type = (
                            (response.headers.get("Content-Type") or "")
                            .split(";")[0]
                            .lower()
                        )
                        if not mime_type or "/" not in mime_type:
                            mime_type = "image/jpeg"

                        encoded = base64.b64encode(data).decode()
                        base64_data = f"data:{mime_type};base64,{encoded}"
                        logger.debug(f"头像下载成功(仅内存使用): {cache_name}")
                        return base64_data
                except Exception as e:
                    logger.warning(f"下载头像异常: {e} (尝试 {attempt}/{max_retries})")
                    if attempt < max_retries:
                        await asyncio.sleep(retry_interval * attempt)
                        continue
                    return None

    except Exception as e:
        logger.error(f"下载头像 {cache_name} 失败: {e}")
        return None


async def send_file(filename: str, host: str, port: int):
    """
    发送文件到远程服务器

    Args:
        filename: 要发送的文件路径
        host: 远程主机地址
        port: 远程主机端口

    Returns:
        str: 远程文件路径，失败返回None
    """
    reader = None
    writer = None

    async def recv_all(reader, size):
        """接收指定大小的数据"""
        data = b""
        while len(data) < size:
            chunk = await reader.read(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    try:
        # 添加连接超时控制
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=5.0,  # 5秒连接超时
        )

        file_name = os.path.basename(filename)
        file_name_bytes = file_name.encode("utf-8")

        # 发送文件名长度和文件名
        writer.write(struct.pack(">I", len(file_name_bytes)))
        writer.write(file_name_bytes)

        # 发送文件大小
        file_size = os.path.getsize(filename)
        writer.write(struct.pack(">Q", file_size))

        # 发送文件内容，添加总体超时控制
        await writer.drain()
        with open(filename, "rb") as f:
            while True:
                data = f.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        logger.debug(f"文件 {file_name} 发送成功")

        # 接收接收端发送的文件绝对路径
        try:
            file_abs_path_len_data = await recv_all(reader, 4)
            if not file_abs_path_len_data:
                logger.error("无法接收文件绝对路径长度")
                return None
            file_abs_path_len = struct.unpack(">I", file_abs_path_len_data)[0]

            file_abs_path_data = await recv_all(reader, file_abs_path_len)
            if not file_abs_path_data:
                logger.error("无法接收文件绝对路径")
                return None

            file_abs_path = file_abs_path_data.decode("utf-8")
            logger.debug(f"文件在远程服务器保存为: {file_abs_path}")
            return file_abs_path

        except Exception as e:
            logger.error(f"接收远程文件路径失败: {e}")
            return None

    except asyncio.TimeoutError:
        logger.error(f"连接 {host}:{port} 超时")
        return None
    except Exception as e:
        logger.error(f"发送文件失败: {e}")
        return None
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        if reader:
            try:
                reader.close()
            except Exception:
                pass


def is_valid_base64_image_str(value: str) -> bool:
    """粗略判断字符串是否为有效的 base64 图像数据或 data URL"""
    if not value:
        return False

    def _looks_like_image(raw: bytes) -> bool:
        """通过魔数快速判断是否为常见图片格式"""
        if not raw or len(raw) < 4:
            return False
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        if raw.startswith(b"\xff\xd8"):  # JPEG
            return True
        if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
            return True
        if raw.startswith((b"GIF87a", b"GIF89a")):  # GIF
            return True
        if len(raw) >= 12 and raw[4:12] in {
            b"ftypheic",
            b"ftypheif",
            b"ftypmif1",
            b"ftypmsf1",
            b"ftyphevc",
        }:
            return True
        return False

    cleaned = value.strip()
    if not cleaned:
        return False
    # 明确包含 URL 特征时直接排除，避免误判
    if "://" in cleaned:
        return False

    if cleaned.startswith("data:image/"):
        _, _, cleaned = cleaned.partition(";base64,")
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        return False

    return _looks_like_image(raw)


async def collect_image_sources(event, log_debug=logger.debug) -> list[str]:
    """
    从消息/引用/合并转发/群文件中收集图片源

    Args:
        event: AstrMessageEvent
        log_debug: 日志函数
    """
    sources: list[str] = []
    seen: set[str] = set()

    def add_source(val: str, origin: str):
        if not val:
            return
        if val in seen:
            return
        seen.add(val)
        sources.append(val)
        log_debug(f"✓ 收集图片源({origin}): {str(val)[:80]}")

    def extract_from_components(components, origin: str):
        if not components:
            return
        for comp in components:
            try:
                # 图片组件
                if comp.__class__.__name__ == "Image":
                    if getattr(comp, "url", None):
                        add_source(comp.url, origin)
                    elif getattr(comp, "file", None):
                        add_source(comp.file, origin)
                    continue

                # 文件组件（尝试按图片处理）
                if comp.__class__.__name__ == "File":
                    file_val = getattr(comp, "file", None) or getattr(comp, "url", None)
                    add_source(file_val, origin)
                    continue

                # 引用消息
                if comp.__class__.__name__ == "Reply" and getattr(comp, "chain", None):
                    extract_from_components(comp.chain, "引用消息")
                    continue

                # 合并转发节点
                if comp.__class__.__name__ == "Node":
                    node_content = getattr(comp, "content", None)
                    extract_from_components(node_content, "合并转发")
                    continue

                # Nodes（多个节点）
                if comp.__class__.__name__ == "Nodes":
                    nodes = getattr(comp, "nodes", None) or getattr(comp, "list", None)
                    extract_from_components(nodes, "合并转发")
                    continue
            except Exception as e:
                log_debug(f"提取图片源异常: {e}")

    try:
        message_chain = event.get_messages()
    except Exception:
        message_chain = getattr(event.message_obj, "message", []) or []

    extract_from_components(message_chain, "当前消息")

    return sources


def coerce_supported_image_bytes(
    mime_type: str | None, raw_bytes: bytes
) -> tuple[str | None, str | None]:
    """
    将输入图片转换为 Gemini 支持的 MIME。
    - 支持: PNG/JPEG/WEBP/HEIC/HEIF
    - 不支持的格式尝试用 Pillow 转为 PNG
    """
    normalized_mime = (mime_type or "").lower()
    target_mime = (
        normalized_mime
        if normalized_mime in SUPPORTED_IMAGE_MIME_TYPES
        else "image/png"
    )
    try:
        with PILImage.open(io.BytesIO(raw_bytes)) as img:
            if target_mime == "image/png":
                save_format = "PNG"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            elif target_mime == "image/jpeg":
                save_format = "JPEG"
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
            elif target_mime == "image/webp":
                save_format = "WEBP"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            else:
                save_format = "PNG"
                target_mime = "image/png"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")

            buffer = io.BytesIO()
            img.save(buffer, format=save_format)
            buffer.seek(0)
            encoded = base64.b64encode(buffer.read()).decode("utf-8")
            return target_mime, encoded
    except Exception as e:
        logger.warning(f"参考图格式不受支持且转换失败: mime={mime_type}, err={e}")
        return None, None


def coerce_supported_image(
    mime_type: str | None, base64_data: str
) -> tuple[str | None, str | None]:
    """兼容旧调用：先尝试解码 base64，再调用字节级转换"""
    try:
        raw = base64.b64decode(base64_data, validate=False)
    except Exception as e:
        logger.warning(f"base64 解码失败，无法转换为受支持格式: {e}")
        return None, None
    return coerce_supported_image_bytes(mime_type, raw)


async def normalize_image_input(
    image_input: any,
    *,
    image_cache_dir: Path | None = None,
    image_input_mode: str = "force_base64",
) -> tuple[str | None, str | None]:
    """
    将参考图像输入规范化为 (mime_type, base64_data)。
    支持 data URI、纯/宽松 base64 字符串、本地文件路径、file://、http/https URL。
    """
    try:
        if image_input is None:
            return None, None

        image_str = str(image_input).strip()
        # 若是 Markdown 图片语法，先提取括号里的 URL/data URI
        md_match = re.search(r"!\[[^\]]*\]\(\s*([^)]+)\s*\)", image_str)
        if md_match:
            image_str = md_match.group(1).strip()

        if "&amp;" in image_str:
            image_str = image_str.replace("&amp;", "&")
        if not image_str:
            return None, None

        cache_dir = image_cache_dir or IMAGE_CACHE_DIR
        logger.debug(
            f"规范化参考图输入: len={len(image_str)} "
            f"type={type(image_input)} mode={image_input_mode}"
        )

        # data URI
        if image_str.startswith("data:image/") and ";base64," in image_str:
            header, data = image_str.split(";base64,", 1)
            mime_type = header.replace("data:", "")
            logger.debug(f"检测到 data URI，mime={mime_type}")
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:
                logger.warning("data URL base64 解码失败")
                return None, None
            return coerce_supported_image_bytes(mime_type, raw)

        # file:// 路径
        if image_str.startswith("file://"):
            parsed = urllib.parse.urlparse(image_str)
            image_path = Path(parsed.path)
            if image_path.exists() and image_path.is_file():
                suffix = image_path.suffix.lower().lstrip(".") or "png"
                mime_type = f"image/{suffix}"
                logger.debug(f"使用 file:// 路径: {image_path}")
                try:
                    data_bytes = image_path.read_bytes()
                    return coerce_supported_image_bytes(mime_type, data_bytes)
                except Exception as e:
                    logger.warning(f"读取 file:// 路径失败: {e}")
            else:
                logger.warning(f"file:// 路径不存在: {image_str}")

        # http(s) URL -> 下载并转base64（带重试和详细日志）
        if image_str.startswith("http://") or image_str.startswith("https://"):
            cleaned_url = image_str.replace("&amp;", "&")
            parsed_url = urllib.parse.urlparse(cleaned_url)
            parsed_host = parsed_url.netloc or ""
            logger.debug(f"下载 http(s) 参考图: {cleaned_url}")

            # 缓存命中直接读取
            cached = _check_image_cache(cleaned_url, cache_dir)
            if cached:
                mime_guess = f"image/{cached.suffix.lstrip('.') or 'png'}"
                data = encode_file_to_base64(cached)
                logger.debug(f"参考图命中缓存: {cleaned_url}")
                return mime_guess, data

            # 使用统一的请求头构建
            is_qq = _is_qq_host(parsed_host)
            headers = _build_http_headers(parsed_host, for_qq=is_qq)

            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            trust_env = not is_qq

            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=trust_env
            ) as session:
                fallback_reason = None

                try:
                    async with session.get(cleaned_url, headers=headers) as resp:
                        if resp.status == 200:
                            content_type = resp.headers.get("Content-Type", "image/png")
                            mime_type = (
                                content_type.split(";")[0]
                                if content_type
                                else "image/png"
                            )
                            try:
                                data_bytes = await resp.read()
                            except Exception as e:
                                fallback_reason = f"读取响应体失败: {e}"
                            else:
                                # 缓存到本地文件
                                _save_to_cache(
                                    cleaned_url, data_bytes, mime_type, cache_dir
                                )
                                return coerce_supported_image_bytes(
                                    mime_type, data_bytes
                                )
                        else:
                            fallback_reason = f"HTTP {resp.status} {resp.reason}"
                except Exception as e:
                    fallback_reason = str(e)
                    logger.warning(
                        f"下载参考图失败: {cleaned_url}，原因: {fallback_reason}"
                    )

        # 纯 base64（宽松校验），尝试自动补齐/过滤非法字符
        logger.debug("尝试将输入视为纯 base64")
        try:
            base64.b64decode(image_str, validate=False)
            return coerce_supported_image(None, image_str)
        except binascii.Error:
            cleaned = re.sub(r"[^A-Za-z0-9+/=_-]", "", image_str)
            pad_len = (-len(cleaned)) % 4
            if pad_len:
                cleaned += "=" * pad_len
            try:
                base64.b64decode(cleaned, validate=False)
                return coerce_supported_image(None, cleaned)
            except Exception:
                return None, None

    except Exception as e:
        logger.error(f"规范化参考图输入失败: {e}")
        return None, None


async def resolve_image_source_to_path(
    source: str,
    *,
    image_input_mode: str = "force_base64",
    api_client=None,
    download_qq_image_fn=None,
    event=None,
    is_valid_checker=is_valid_base64_image_str,
    logger_obj=logger,
) -> str | None:
    """
    将图片源转换为本地文件路径以便切割

    Args:
        source: 图片源（URL/文件/base64/data URL）
        image_input_mode: 参考图处理模式（统一 base64）
        api_client: 用于 normalize 的 API 客户端（可选）
        download_qq_image_fn: 处理 qpic 链接的下载函数（可选，需为 async）
        is_valid_checker: base64 校验函数
        logger_obj: 日志对象
    """
    if not source:
        return None

    src = str(source).strip()
    # 修正 HTML 转义的参数
    if "&amp;" in src:
        src = src.replace("&amp;", "&")
    if not src:
        return None

    # 本地文件或 file://
    if src.startswith("file:///"):
        fs_path = src[8:]
        if os.path.exists(fs_path):
            return fs_path
    if os.path.exists(src):
        return src

    # base64/data URL - 使用统一的解码函数
    if is_valid_checker(src):
        result = _decode_base64_to_temp_file(
            src, verify_image=True, logger_obj=logger_obj
        )
        if result:
            return result

    # http(s) 下载
    if src.startswith(("http://", "https://")):
        parsed_host = ""
        try:
            parsed_host = urllib.parse.urlparse(src).netloc or ""
        except Exception:
            parsed_host = ""

        try:
            # 命中下载缓存直接返回文件
            cached = _check_image_cache(src, IMAGE_CACHE_DIR)
            if cached:
                return str(cached)

            data_url = None
            is_qq = _is_qq_host(parsed_host)

            if download_qq_image_fn and is_qq:
                try:
                    data_url = await download_qq_image_fn(src, event=event)
                except Exception:
                    data_url = await download_qq_image_fn(src)

            if not data_url and api_client:
                mime_type, b64 = await api_client._normalize_image_input(
                    src, image_input_mode=image_input_mode
                )
                if b64:
                    data_url = (
                        b64
                        if image_input_mode == "force_base64"
                        else f"data:{mime_type};base64,{b64}"
                    )

            if not data_url:
                timeout = aiohttp.ClientTimeout(total=12, connect=5)
                headers = _build_http_headers(parsed_host, for_qq=is_qq)
                trust_env_flag = not is_qq
                async with aiohttp.ClientSession(
                    headers=headers, trust_env=trust_env_flag
                ) as session:
                    async with session.get(src, timeout=timeout) as resp:
                        if resp.status == 200:
                            mime = resp.headers.get("Content-Type", "image/png")
                            content = await resp.read()
                            b64 = base64.b64encode(content).decode()
                            data_url = f"data:{mime};base64,{b64}"

            # 使用统一的解码函数
            if data_url and is_valid_checker(data_url):
                result = _decode_base64_to_temp_file(
                    data_url, verify_image=True, logger_obj=logger_obj
                )
                if result:
                    return result
        except Exception as e:
            logger_obj.warning(f"下载图片失败: {e} | {src[:80]}")

    # 其他字符串尝试当作 base64
    result = _decode_base64_to_temp_file(src, verify_image=True, logger_obj=logger_obj)
    return result


class AvatarManager:
    """头像管理器"""

    def __init__(self, images_dir: Path | None = None):
        self.images_dir = images_dir

    async def get_avatar(self, user_id: str, cache_name: str, event=None) -> str | None:
        """
        获取用户头像

        Args:
            user_id: 用户ID
            cache_name: 缓存名称
            event: AstrMessageEvent，用于调用NapCat API

        Returns:
            base64格式的头像数据
        """
        return await download_qq_avatar(user_id, cache_name, self.images_dir, event)

    async def cleanup_cache(self):
        """清理头像缓存"""
        if self.images_dir is None:
            self.images_dir = get_plugin_data_dir() / "images"

        cache_dir = self.images_dir / "avatar_cache"
        if cache_dir.exists():
            # 不再使用头像缓存，直接清空目录
            try:
                for avatar_file in cache_dir.glob("*"):
                    avatar_file.unlink(missing_ok=True)
                cache_dir.rmdir()
                logger.debug("已清空头像缓存目录")
            except Exception as e:
                logger.warning(f"清理头像缓存目录失败: {e}")

    async def cleanup_used_avatars(self):
        """清理已使用的头像缓存（别名方法）"""
        await self.cleanup_cache()


# 为了向后兼容，提供一些旧名称的别名
def download_qq_avatar_legacy(user_id: str, cache_name: str, event=None) -> str | None:
    """
    下载QQ头像的兼容函数

    Args:
        user_id: QQ用户ID
        cache_name: 缓存文件名

    Returns:
        base64格式的头像数据，失败返回None
    """
    # 使用asyncio运行同步调用
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            download_qq_avatar(user_id, cache_name, event=event)
        )
    finally:
        loop.close()
