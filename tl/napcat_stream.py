"""NapCat Stream API upload helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger

DEFAULT_STREAM_CHUNK_SIZE = 64 * 1024
DEFAULT_FILE_RETENTION_MS = 30 * 1000


def _get_bot_client(event: Any) -> Any | None:
    return getattr(event, "bot", None) or getattr(event, "_bot", None)


def _supports_call_action(bot_client: Any) -> bool:
    return (
        hasattr(bot_client, "api") and hasattr(bot_client.api, "call_action")
    ) or hasattr(bot_client, "call_action")


async def _call_action(bot_client: Any, action: str, params: dict[str, Any]) -> Any:
    if hasattr(bot_client, "api") and hasattr(bot_client.api, "call_action"):
        return await bot_client.api.call_action(action, **params)
    if hasattr(bot_client, "call_action"):
        return await bot_client.call_action(action, **params)
    return None


def _extract_response_data(response: Any) -> dict[str, Any]:
    if response is None:
        raise RuntimeError("NapCat Stream API 未返回响应")
    if not isinstance(response, dict):
        raise RuntimeError(f"NapCat Stream API 返回格式异常: {type(response).__name__}")

    status = response.get("status")
    if status == "failed":
        message = response.get("message") or response.get("wording") or response
        raise RuntimeError(f"NapCat Stream API 返回失败: {message}")
    retcode = response.get("retcode")
    if retcode not in (None, 0):
        message = response.get("message") or response.get("wording") or response
        raise RuntimeError(f"NapCat Stream API 返回错误: retcode={retcode}, {message}")

    data = response.get("data")
    if isinstance(data, dict):
        return data
    return response


def _calculate_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as file:
        while True:
            chunk = file.read(DEFAULT_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _extract_uploaded_path(response: Any) -> str | None:
    data = _extract_response_data(response)
    for key in ("file_path", "file", "path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def upload_file_stream(
    event: Any,
    file_path: str | Path,
    *,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
    file_retention_ms: int = DEFAULT_FILE_RETENTION_MS,
) -> str | None:
    """Upload a local file to NapCat through the existing OneBot connection."""
    bot_client = _get_bot_client(event)
    if not bot_client:
        logger.debug("[NapCat Stream] 未找到事件 bot 客户端，跳过流式上传")
        return None
    if not _supports_call_action(bot_client):
        logger.debug("[NapCat Stream] bot 客户端不支持 call_action，跳过流式上传")
        return None

    path = Path(file_path)
    if not path.exists() or not path.is_file():
        logger.debug(f"[NapCat Stream] 文件不存在，跳过流式上传: {file_path}")
        return None

    file_size = path.stat().st_size
    if file_size <= 0:
        logger.debug(f"[NapCat Stream] 文件为空，跳过流式上传: {file_path}")
        return None

    chunk_size = max(1, int(chunk_size or DEFAULT_STREAM_CHUNK_SIZE))
    total_chunks = max(1, math.ceil(file_size / chunk_size))
    stream_id = str(uuid.uuid4())

    logger.debug(
        f"[NapCat Stream] 开始上传: file={path.name} size={file_size} chunks={total_chunks}"
    )

    try:
        expected_sha256 = await asyncio.to_thread(_calculate_sha256, path)
        current_size = path.stat().st_size
        if current_size != file_size:
            raise RuntimeError(
                "文件在上传前大小发生变化，无法按声明大小完成上传: "
                f"file={path} expected={file_size} actual={current_size}"
            )

        with path.open("rb") as file:
            for chunk_index in range(total_chunks):
                chunk = file.read(chunk_size)
                if not chunk:
                    raise RuntimeError(
                        "文件在上传过程中提前结束，无法按声明的分块数完成上传: "
                        f"file={path} chunk_index={chunk_index} "
                        f"total_chunks={total_chunks}"
                    )
                expected_chunk_size = (
                    file_size - chunk_size * chunk_index
                    if chunk_index == total_chunks - 1
                    else chunk_size
                )
                if len(chunk) != expected_chunk_size:
                    raise RuntimeError(
                        "文件在上传过程中大小发生变化，无法按声明大小完成上传: "
                        f"file={path} chunk_index={chunk_index} "
                        f"expected={expected_chunk_size} actual={len(chunk)}"
                    )

                response = await _call_action(
                    bot_client,
                    "upload_file_stream",
                    {
                        "stream_id": stream_id,
                        "chunk_data": base64.b64encode(chunk).decode("utf-8"),
                        "chunk_index": chunk_index,
                        "total_chunks": total_chunks,
                        "file_size": file_size,
                        "expected_sha256": expected_sha256,
                        "filename": path.name,
                        "file_retention": file_retention_ms,
                    },
                )
                _extract_response_data(response)

        complete_response = await _call_action(
            bot_client,
            "upload_file_stream",
            {"stream_id": stream_id, "is_complete": True},
        )
        uploaded_path = _extract_uploaded_path(complete_response)
        if uploaded_path:
            logger.debug(f"[NapCat Stream] 上传完成: {uploaded_path}")
        else:
            logger.warning("[NapCat Stream] 上传完成响应缺少 file_path")
        return uploaded_path
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"[NapCat Stream] 上传失败，回退原文件路径: {exc}")
        return None
