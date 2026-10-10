"""ReferenceService：把参考图输入统一解析为落盘的缓存 Artifact。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - 取决于运行环境可选依赖
    PILImage = None

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
from .remote_fetch import RemoteFetchService, _remaining

# 本地路径优先判定的输入长度上限，与 tl/api/reference_pipeline 对齐。
_LOCAL_PATH_MAX_CHARS = 1024
_DATA_URI_PREFIX = "data:image/"


def _ensure_deadline(deadline_at: datetime | None) -> None:
    """与 remote_fetch._remaining 相同口径：naive 抛 ValueError，耗尽抛超时。"""

    remaining = _remaining(deadline_at, None)
    if remaining is not None and remaining <= 0:
        raise ReferenceTimeoutError("参考图解析 deadline 已耗尽")


def _decode_base64(payload: str, label: str) -> bytes:
    """严格解码 base64 文本；失败按解析失败处理。"""

    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.warning("参考图 base64 解码失败（%s）", label)
        raise ReferenceResolveError(f"参考图 base64 解码失败（{label}）") from exc


def _looks_like_local_file(text: str) -> bool:
    """短、无 scheme、非 data URI 且真实存在的路径按本地文件处理。"""

    return (
        "://" not in text
        and not text.startswith("data:")
        and len(text) <= _LOCAL_PATH_MAX_CHARS
        and Path(text).is_file()
    )


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
    ) -> None:
        self.policy = policy
        self.fetcher = fetcher
        self.cache_dir = Path(cache_dir)
        self.max_pixels = int(max_pixels)
        self.max_reference_bytes = int(max_reference_bytes)

    async def resolve(
        self,
        references: tuple[str, ...],
        *,
        deadline_at: datetime | None = None,
    ) -> AsyncIterator[Artifact]:
        """按输入顺序逐个解析；任一 reference 失败立即抛错。"""

        for item in references:
            _ensure_deadline(deadline_at)
            text = str(item or "").strip()
            if not text:
                raise ReferenceResolveError("参考图输入为空")
            yield await self._resolve_one(text, deadline_at)

    async def _resolve_one(self, text: str, deadline_at: datetime | None) -> Artifact:
        if text.startswith("data:"):
            if not (text.startswith(_DATA_URI_PREFIX) and ";base64," in text):
                logger.warning("不支持的参考图 data URI 形态")
                raise ReferenceResolveError(
                    "参考图 data URI 仅支持 data:image/*;base64 形态"
                )
            raw = _decode_base64(text.split(";base64,", 1)[1], "data URI")
            return self._materialize(raw, source="inline")
        if text.startswith("file:"):
            raw, source = self._load_local(text)
            return self._materialize(raw, source=source)
        if text.startswith(("http://", "https://")):
            return await self._load_url(text, deadline_at)
        # 歧义判定与共享管道一致：先按本地文件尝试，失败再走 base64。
        if _looks_like_local_file(text):
            raw, source = self._load_local(text)
            return self._materialize(raw, source=source)
        return self._materialize(_decode_base64(text, "裸 base64"), source="inline")

    def _load_local(self, text: str) -> tuple[bytes, str]:
        try:
            resolved = self.policy.validate_local_path(text)
        except PathPolicyError as exc:
            logger.warning("本地参考图未通过安全校验: %s", exc)
            raise
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            logger.error("读取本地参考图失败: %s", exc)
            raise ReferenceResolveError(f"读取本地参考图失败: {resolved.name}") from exc
        return raw, resolved.as_uri()

    async def _load_url(self, url: str, deadline_at: datetime | None) -> Artifact:
        host = urlsplit(url).hostname or ""
        headers = (
            build_reference_image_headers(host, for_qq=True)
            if is_qq_image_host(host)
            else build_reference_image_headers(host)
        )
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
            raise ReferenceTimeoutError(f"参考图抓取超时: {redact_url(url)}") from exc
        except PathPolicyError:
            logger.warning("参考图抓取目标被安全策略拒绝: %s", redact_url(url))
            raise
        except RemoteFetchError as exc:
            logger.error("参考图抓取失败: %s", redact_url(url))
            raise ReferenceResolveError(f"参考图抓取失败: {redact_url(url)}") from exc
        mime_type = self._check_bytes(raw)
        save_reference_image_cache(url, raw, mime_type, self.cache_dir)
        return self._finish(raw, mime_type, source=redact_url(url))

    def _materialize(self, raw: bytes, *, source: str) -> Artifact:
        return self._finish(raw, self._check_bytes(raw), source=source)

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

    def _check_bytes(self, raw: bytes) -> str:
        if not raw:
            raise ReferenceResolveError("参考图内容为空")
        if len(raw) > self.max_reference_bytes:
            raise ReferenceTooLargeError(
                f"参考图超过 {self.max_reference_bytes} 字节上限"
            )
        mime_type = detect_reference_image_mime(raw)
        if not mime_type:
            logger.warning("参考图格式无法识别（文件头不匹配）")
            raise ReferenceResolveError(
                "无法识别参考图格式（仅支持 PNG/JPEG/WebP/HEIC/HEIF）"
            )
        self._check_pixels(raw, mime_type)
        return mime_type

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
