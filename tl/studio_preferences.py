"""沙箱插件页的浏览器偏好存储；浏览器仅持有独立的 Cookie 标识。"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger


class StudioPreferencesStore:
    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "webui_preferences"
        self._lock = asyncio.Lock()

    def _read(self, browser_id: str) -> dict[str, Any]:
        try:
            value = json.loads(
                (self.directory / f"{browser_id}.json").read_text(encoding="utf-8")
            )
            if isinstance(value, dict) and type(value.get("revision")) is int:
                return value
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning(f"[WebUI] 浏览器偏好读取失败: {exc}")
        return {"revision": 0, "preferences": {"selected": None, "candidates": {}}}

    async def load(self, browser_id: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._read, browser_id)

    def _write(self, browser_id: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{browser_id}.json"
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    async def save(
        self, browser_id: str, preferences: dict[str, Any], revision: int
    ) -> dict[str, Any]:
        async with self._lock:
            current = await asyncio.to_thread(self._read, browser_id)
            # 父页桥接请求可以并发完成，旧请求不能覆盖较新的选择。
            if revision < current["revision"]:
                return current
            value = {"revision": revision, "preferences": preferences}
            await asyncio.to_thread(self._write, browser_id, value)
            return value
