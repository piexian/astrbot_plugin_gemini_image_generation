"""ReferenceService：把参考图输入统一解析为落盘的缓存 Artifact。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from astrbot.api import logger

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - 取决于运行环境可选依赖
    PILImage = None

from ..api.reference_pipeline import transcode_to_supported_mime
from ..api_types import APIError
from ..file_uri import file_uri_to_path
from ..reference_image import (
    REFERENCE_IMAGE_CACHE_DIR,
    build_reference_image_headers,
    check_reference_image_cache,
    detect_reference_image_mime,
    is_qq_image_host,
    save_reference_image_cache,
)
from .errors import (
    PathPolicyError,
    ReferenceResolveError,
    ReferenceTimeoutError,
    ReferenceTooLargeError,
    RemoteFetchError,
    RemoteFetchTimeoutError,
    redact_url,
)
from .models import Artifact
from .path_policy import PathPolicy
from .protocols import ReferenceStoreProtocol
from .reference_store import TMP_REFERENCE_KIND, TmpReferenceStore
from .remote_fetch import RemoteFetchService, _remaining

# 本地路径优先判定的输入长度上限，与 tl/api/reference_pipeline 对齐。
_LOCAL_PATH_MAX_CHARS = 1024
_DATA_URI_PREFIX = "data:image/"
# 句柄严格形态：与 reference_descriptor 的 ref:<32hex>（单冒号）不同，
# 双冒号形态不会被误判为描述符。
_HANDLE_RE = re.compile(r"^ref:([a-z]+):([0-9a-f]{32})$")

# 透传白名单：这些魔数格式原样落盘，HEIC/HEIF 的像素校验豁免见 _check_pixels。
_PASSTHROUGH_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
)
# 需转码为 PNG 的附加魔数格式；其余文件头仍拒绝（比旧管道更严格）。
_TRANSCODE_MAGIC_PREFIXES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("image/gif", (b"GIF87a", b"GIF89a")),
    ("image/bmp", (b"BM",)),
    ("image/tiff", (b"II*\x00", b"MM\x00*")),
)


def _ensure_deadline(deadline_at: datetime | None) -> None:
    """与 remote_fetch._remaining 相同口径：naive 抛 ValueError，耗尽抛超时。"""

    remaining = _remaining(deadline_at, None)
    if remaining is not None and remaining <= 0:
        raise ReferenceTimeoutError("参考图解析 deadline 已耗尽")


def _decode_base64(payload: str, label: str, max_bytes: int | None = None) -> bytes:
    """严格解码 base64 文本；失败按解析失败处理。"""
    if max_bytes is not None:
        padding = len(payload) - len(payload.rstrip("="))
        estimated = (len(payload) // 4) * 3 - padding
        if estimated > max_bytes:
            raise ReferenceTooLargeError(f"参考图超过 {max_bytes} 字节上限")

    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.warning("参考图 base64 解码失败（%s）", label)
        raise ReferenceResolveError(f"参考图 base64 解码失败（{label}）") from exc


def _looks_like_local_file(text: str) -> bool:
    """短、无 scheme、非 data URI 且真实存在的路径按本地文件处理。"""

    return (
        "://" not in text
        and not text.lower().startswith("data:")
        and len(text) <= _LOCAL_PATH_MAX_CHARS
        and Path(text).is_file()
    )


def classify_reference(
    text: str,
) -> Literal["handle", "inline", "file", "url", "local"]:
    """判定参考图输入形态；解析与 ingest 共用，避免两处判定漂移。"""

    lowered = text.lower()
    if _HANDLE_RE.match(text):
        return "handle"
    if lowered.startswith("data:"):
        return "inline"
    if lowered.startswith("file:"):
        return "file"
    if lowered.startswith(("http://", "https://")):
        return "url"
    if _looks_like_local_file(text):
        return "local"
    # 歧义输入与共享管道一致：非本地文件时按裸 base64 处理。
    return "inline"


def _trusted_local_path(text: str) -> Path:
    """local / file: 形态的参考图转本地绝对路径；file URI 复用 file_uri_to_path。"""

    if text.lower().startswith("file:"):
        path = file_uri_to_path(text)
        if path is None:
            raise ReferenceResolveError("本地参考图 file URI 不是本机路径")
    else:
        path = Path(text)
    if not path.is_absolute():
        raise ReferenceResolveError("本地参考图必须是绝对路径")
    return path


def _detect_transcodable_mime(raw: bytes) -> str | None:
    """识别需转码的 GIF/BMP/TIFF 魔数。"""
    for mime_type, prefixes in _TRANSCODE_MAGIC_PREFIXES:
        if raw.startswith(prefixes):
            return mime_type
    return None


class ReferenceService:
    """按 PathPolicy 与大小/像素边界解析参考图输入，产出缓存 Artifact。"""

    def __init__(
        self,
        policy: PathPolicy,
        fetcher: RemoteFetchService,
        cache_dir: Path = REFERENCE_IMAGE_CACHE_DIR,
        *,
        max_pixels: int = 4096 * 4096,
        max_reference_bytes: int = 16 * 1024 * 1024,
        stores: Sequence[ReferenceStoreProtocol] = (),
    ) -> None:
        self.policy = policy
        self.fetcher = fetcher
        self.cache_dir = Path(cache_dir)
        self.max_pixels = int(max_pixels)
        self.max_reference_bytes = int(max_reference_bytes)
        registry: dict[str, ReferenceStoreProtocol] = {}
        for store in stores:
            if store.kind in registry:
                raise ValueError(f"重复的参考图句柄存储 kind: {store.kind}")
            registry[store.kind] = store
        tmp_store = registry.get(TMP_REFERENCE_KIND)
        if tmp_store is None:
            tmp_store = TmpReferenceStore(self._write_cache)
            registry[TMP_REFERENCE_KIND] = tmp_store
        if not isinstance(tmp_store, TmpReferenceStore):
            # ingest 依赖 TmpReferenceStore.put 登记句柄，替换实现须另行扩展协议。
            raise ValueError("tmp 句柄存储必须是 TmpReferenceStore 实例")
        self._tmp_store = tmp_store
        self._stores = registry
        self._url_locks: dict[str, asyncio.Lock] = {}
        self._url_lock_users: dict[str, int] = {}

    async def resolve(
        self,
        references: tuple[str, ...],
        *,
        deadline_at: datetime | None = None,
        requester: Mapping[str, str] | None = None,
    ) -> AsyncIterator[Artifact]:
        """按输入顺序逐个解析；任一 reference 失败立即抛错。"""

        for item in references:
            _ensure_deadline(deadline_at)
            text = str(item or "").strip()
            if not text:
                raise ReferenceResolveError("参考图输入为空")
            yield await self._resolve_one(text, deadline_at, requester)

    async def _resolve_one(
        self,
        text: str,
        deadline_at: datetime | None,
        requester: Mapping[str, str] | None = None,
    ) -> Artifact:
        shape = classify_reference(text)
        if shape == "handle":
            # classify 已按严格正则校验，split 恰得 ref/kind/id 三段。
            _, kind, ref_id = text.split(":")
            store = self._stores.get(kind)
            if store is None:
                raise ReferenceResolveError(f"未知参考图句柄类型: {kind}")
            return await store.resolve(
                ref_id,
                requester=dict(requester or {}),
                deadline_at=deadline_at,
            )
        if shape == "inline":
            lowered = text.lower()
            if lowered.startswith("data:"):
                marker = ";base64,"
                if not (lowered.startswith(_DATA_URI_PREFIX) and marker in lowered):
                    logger.warning("不支持的参考图 data URI 形态")
                    raise ReferenceResolveError(
                        "参考图 data URI 仅支持 data:image/*;base64 形态"
                    )
                raw = _decode_base64(
                    text[lowered.index(marker) + len(marker) :],
                    "data URI",
                    self.max_reference_bytes,
                )
                return self._materialize(raw, source="inline")
            return self._materialize(
                _decode_base64(text, "裸 base64", self.max_reference_bytes),
                source="inline",
            )
        if shape == "file":
            raw, source = self._load_local(text)
            return self._materialize(raw, source=source)
        if shape == "url":
            return await self._load_url(text, deadline_at)
        raw, source = self._load_local(text)
        return self._materialize(raw, source=source)

    async def ingest(self, data: str | bytes, *, requester: Mapping[str, str]) -> str:
        """把内联图（data URI / 裸 base64 / 原始字节）校验落盘，返回 tmp 句柄。"""

        if isinstance(data, str) and not data.strip():
            raise ReferenceResolveError("参考图输入为空")
        if not isinstance(data, (str, bytes)):
            raise ReferenceResolveError("参考图输入必须是 data URI、base64 文本或字节")
        # 解码与 Pillow 校验/转码是同步 CPU 工作，放线程避免卡事件循环。
        payload, mime_type = await asyncio.to_thread(self._prepare_ingest, data)
        ref_id = await self._tmp_store.put(payload, mime_type, requester=requester)
        return f"ref:{self._tmp_store.kind}:{ref_id}"

    def _prepare_ingest(self, data: str | bytes) -> tuple[bytes, str]:
        if isinstance(data, bytes):
            return self._check_bytes(data)
        text = data.strip()
        lowered = text.lower()
        if lowered.startswith("data:"):
            marker = ";base64,"
            if not (lowered.startswith(_DATA_URI_PREFIX) and marker in lowered):
                logger.warning("不支持的参考图 data URI 形态")
                raise ReferenceResolveError(
                    "参考图 data URI 仅支持 data:image/*;base64 形态"
                )
            return self._check_bytes(
                _decode_base64(
                    text[lowered.index(marker) + len(marker) :],
                    "data URI",
                    self.max_reference_bytes,
                )
            )
        return self._check_bytes(
            _decode_base64(text, "裸 base64", self.max_reference_bytes)
        )

    def retain_reference(self, reference: str) -> bool:
        """固定 tmp 句柄；非 tmp 引用返回 False。"""

        match = _HANDLE_RE.match(reference)
        if match is None or match.group(1) != TMP_REFERENCE_KIND:
            return False
        self._tmp_store.retain(match.group(2))
        return True

    def release_reference(self, reference: str) -> None:
        """释放 retain_reference 固定的一个 tmp 句柄 lease。"""

        match = _HANDLE_RE.match(reference)
        if match is not None and match.group(1) == TMP_REFERENCE_KIND:
            self._tmp_store.release(match.group(2))

    async def ingest_inline(
        self,
        references: Sequence[str],
        *,
        requester: Mapping[str, str],
        trusted_local: bool = False,
    ) -> tuple[str, ...]:
        """内联形态转句柄、其余原样保留并保持顺序；失败立即抛出。

        ``trusted_local`` 表示调用方已按自己的规则校验过 local / file: 形态的
        本地路径（如 LLM 工具的 tool_path_guard）：这些文件在此读取并换成句柄，
        只套用大小、格式与像素边界；PathPolicy 的本地根不因此放宽，默认 False
        时本地路径仍原样保留，留给执行阶段按 PathPolicy 校验。

        已落盘的前几张句柄不回滚，留给 tmp store 的 TTL/容量清理。
        """

        prepared: list[str] = []
        for item in references:
            text = str(item or "").strip()
            shape = classify_reference(text)
            if shape == "inline":
                prepared.append(await self.ingest(text, requester=requester))
            elif trusted_local and shape in ("local", "file"):
                raw = await asyncio.to_thread(self._read_trusted_local, text)
                prepared.append(await self.ingest(raw, requester=requester))
            else:
                prepared.append(text)
        return tuple(prepared)

    def _read_trusted_local(self, text: str) -> bytes:
        """读取调用方已校验过的本地文件；先 stat 判大小，读取量也以上限封顶。"""

        path = _trusted_local_path(text)
        limit = self.max_reference_bytes
        try:
            if path.stat().st_size > limit:
                raise ReferenceTooLargeError(f"参考图超过 {limit} 字节上限")
            with path.open("rb") as handle:
                raw = handle.read(limit + 1)
        except (OSError, ValueError) as exc:
            logger.error("读取本地参考图失败: %s", exc)
            raise ReferenceResolveError(f"读取本地参考图失败: {path.name}") from exc
        if len(raw) > limit:
            # stat 之后文件又变大：与 stat 判定同口径拒绝。
            raise ReferenceTooLargeError(f"参考图超过 {limit} 字节上限")
        return raw

    def _load_local(self, text: str) -> tuple[bytes, str]:
        try:
            resolved = self.policy.validate_local_path(text)
        except PathPolicyError as exc:
            logger.warning("本地参考图未通过安全校验: %s", exc)
            raise
        try:
            if resolved.stat().st_size > self.max_reference_bytes:
                raise ReferenceTooLargeError(
                    f"参考图超过 {self.max_reference_bytes} 字节上限"
                )
            raw = resolved.read_bytes()
        except OSError as exc:
            logger.error("读取本地参考图失败: %s", exc)
            raise ReferenceResolveError(f"读取本地参考图失败: {resolved.name}") from exc
        return raw, resolved.as_uri()

    async def _load_url(self, url: str, deadline_at: datetime | None) -> Artifact:
        try:
            await self.fetcher.check_url(url, deadline_at)
        except RemoteFetchTimeoutError as exc:
            raise ReferenceTimeoutError("参考图 URL 安全校验超时") from exc
        host = urlsplit(url).hostname or ""
        headers = (
            build_reference_image_headers(host, for_qq=True)
            if is_qq_image_host(host)
            else build_reference_image_headers(host)
        )
        lock = self._url_locks.get(url)
        if lock is None:
            lock = asyncio.Lock()
            self._url_locks[url] = lock
        self._url_lock_users[url] = self._url_lock_users.get(url, 0) + 1
        try:
            async with lock:
                cached = check_reference_image_cache(url, self.cache_dir)
                if cached is not None:
                    try:
                        raw = cached.read_bytes()
                    except OSError as exc:
                        logger.error("读取参考图缓存失败: %s", redact_url(url))
                        raise ReferenceResolveError(
                            f"读取参考图缓存失败: {redact_url(url)}"
                        ) from exc
                    return self._materialize(raw, source=redact_url(url))
                try:
                    raw = await self.fetcher.fetch(
                        url, deadline_at=deadline_at, headers=headers
                    )
                except RemoteFetchTimeoutError as exc:
                    logger.error("参考图抓取超时: %s", redact_url(url))
                    raise ReferenceTimeoutError(
                        f"参考图抓取超时: {redact_url(url)}"
                    ) from exc
                except PathPolicyError:
                    logger.warning("参考图抓取目标被安全策略拒绝: %s", redact_url(url))
                    raise
                except RemoteFetchError as exc:
                    logger.error("参考图抓取失败: %s", redact_url(url))
                    raise ReferenceResolveError(
                        f"参考图抓取失败: {redact_url(url)}"
                    ) from exc
                payload, mime_type = self._check_bytes(raw)
                save_reference_image_cache(url, payload, mime_type, self.cache_dir)
                return self._finish(payload, mime_type, source=redact_url(url))
        finally:
            users = self._url_lock_users[url] - 1
            if users == 0:
                self._url_lock_users.pop(url, None)
                if self._url_locks.get(url) is lock:
                    self._url_locks.pop(url, None)
            else:
                self._url_lock_users[url] = users

    def _materialize(self, raw: bytes, *, source: str) -> Artifact:
        payload, mime_type = self._check_bytes(raw)
        return self._finish(payload, mime_type, source=source)

    def _finish(self, raw: bytes, mime_type: str, *, source: str) -> Artifact:
        path = self._write_cache(raw, mime_type)
        return Artifact(
            artifact_id=f"ref-{uuid.uuid4().hex}",
            kind="reference",
            location=str(path),
            mime_type=mime_type,
            size_bytes=len(raw),
            metadata={"source": source},
        )

    def _check_bytes(self, raw: bytes) -> tuple[bytes, str]:
        """大小 → 魔数 → 像素数 → 按需转码，返回落盘字节与 MIME。"""
        if not raw:
            raise ReferenceResolveError("参考图内容为空")
        if len(raw) > self.max_reference_bytes:
            raise ReferenceTooLargeError(
                f"参考图超过 {self.max_reference_bytes} 字节上限"
            )
        mime_type = detect_reference_image_mime(raw)
        if mime_type:
            # 透传格式不做转码，像素校验后原样落盘。
            self._check_pixels(raw, mime_type)
            return raw, mime_type
        transcode_mime = _detect_transcodable_mime(raw)
        if not transcode_mime:
            logger.warning("参考图格式无法识别（文件头不匹配）")
            raise ReferenceResolveError(
                "无法识别参考图格式（仅支持 PNG/JPEG/WebP/HEIC/HEIF/GIF/BMP/TIFF）"
            )
        # 像素数在解码前读取：超限的 GIF/BMP/TIFF 不进入转码。
        self._check_pixels(raw, transcode_mime)
        return self._transcode(raw, transcode_mime)

    def _transcode(self, raw: bytes, mime_type: str) -> tuple[bytes, str]:
        """GIF/BMP/TIFF 复用共享管道转 PNG（动图取首帧），不在本地另写转码。"""
        try:
            encoded, out_mime = transcode_to_supported_mime(
                raw,
                mime_type,
                error_label="scheduler_v2",
                supported=_PASSTHROUGH_MIMES,
                max_bytes=self.max_reference_bytes,
            )
        except APIError as exc:
            if exc.error_type == "reference_image_too_large":
                raise ReferenceTooLargeError(
                    f"参考图转码后超过 {self.max_reference_bytes} 字节上限"
                ) from exc
            logger.warning("参考图转码失败（%s）: %s", mime_type, exc.message)
            raise ReferenceResolveError(f"参考图转码失败（{mime_type}）") from exc
        return base64.b64decode(encoded), out_mime

    def _check_pixels(self, raw: bytes, mime_type: str) -> None:
        if PILImage is None:  # pragma: no cover - Pillow 缺失时拒绝而非放行
            raise ReferenceResolveError("Pillow 不可用，无法校验参考图像素数")
        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                width, height = img.size
        except PILImage.DecompressionBombError as exc:
            raise ReferenceTooLargeError(
                f"参考图像素数超过 PIL 安全上限（{mime_type}）"
            ) from exc
        except PILImage.UnidentifiedImageError as exc:
            if mime_type in {"image/heic", "image/heif"}:
                # Pillow 缺 libheif 解不开 HEIC/HEIF；文件头已确认格式，
                # 与旧管道一致透传给供应商解码，字节上限仍然生效。
                logger.warning("参考图 %s 无法本地解码，跳过像素校验", mime_type)
                return
            logger.warning("参考图解码失败（%s）", mime_type)
            raise ReferenceResolveError(f"参考图解码失败（{mime_type}）") from exc
        except Exception as exc:
            logger.warning("参考图解码失败（%s）: %s", mime_type, exc)
            raise ReferenceResolveError(f"参考图解码失败（{mime_type}）") from exc
        if width * height > self.max_pixels:
            raise ReferenceTooLargeError(
                f"参考图像素数超限: {width}x{height} > {self.max_pixels}"
            )

    def _write_cache(self, raw: bytes, mime_type: str) -> Path:
        digest = hashlib.sha256(raw).hexdigest()
        suffix = mime_type.split("/")[-1] if "/" in mime_type else "png"
        path = self.cache_dir / f"{digest}.{suffix}"
        if not path.exists() or path.stat().st_size != len(raw):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return path
