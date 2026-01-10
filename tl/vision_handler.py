"""视觉 LLM 识别和处理模块"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from PIL import Image as PILImage

from .enhanced_prompts import (
    get_grid_detect_prompt,
    get_sticker_bbox_prompt,
    get_vision_crop_system_prompt,
)
from .image_splitter import split_image

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context

    from .tl_api import APIClient


class VisionHandler:
    """视觉 LLM 识别处理器"""

    def __init__(
        self,
        context: Context,
        api_client: APIClient | None = None,
        vision_provider_id: str = "",
        vision_model: str = "",
        enable_llm_crop: bool = True,
        sticker_bbox_rows: int = 6,
        sticker_bbox_cols: int = 4,
    ):
        """
        Args:
            context: AstrBot Context 实例
            api_client: API 客户端实例
            vision_provider_id: 视觉提供商 ID
            vision_model: 视觉模型名称
            enable_llm_crop: 是否启用 LLM 裁剪
            sticker_bbox_rows: 表情包裁剪行数
            sticker_bbox_cols: 表情包裁剪列数
        """
        self.context = context
        self.api_client = api_client
        self.vision_provider_id = vision_provider_id
        self.vision_model = vision_model
        self.enable_llm_crop = enable_llm_crop
        self.sticker_bbox_rows = sticker_bbox_rows
        self.sticker_bbox_cols = sticker_bbox_cols

    def update_config(
        self,
        api_client: APIClient | None = None,
        vision_provider_id: str | None = None,
        vision_model: str | None = None,
        enable_llm_crop: bool | None = None,
        sticker_bbox_rows: int | None = None,
        sticker_bbox_cols: int | None = None,
    ):
        """更新配置"""
        if api_client is not None:
            self.api_client = api_client
        if vision_provider_id is not None:
            self.vision_provider_id = vision_provider_id
        if vision_model is not None:
            self.vision_model = vision_model
        if enable_llm_crop is not None:
            self.enable_llm_crop = enable_llm_crop
        if sticker_bbox_rows is not None:
            self.sticker_bbox_rows = sticker_bbox_rows
        if sticker_bbox_cols is not None:
            self.sticker_bbox_cols = sticker_bbox_cols

    @staticmethod
    def extract_llm_text(resp: Any) -> str:
        """
        兼容 AstrBot LLMResponse 文本提取：
        - 优先 result_chain 中的 Plain 文本
        - 其次 output_text / response
        """
        try:
            if getattr(resp, "result_chain", None):
                chain = getattr(resp.result_chain, "chain", None)
                if isinstance(chain, list):
                    parts: list[str] = []
                    for comp in chain:
                        text_val = getattr(comp, "text", None)
                        if text_val:
                            parts.append(str(text_val))
                    if parts:
                        return " ".join(parts).strip()

            if getattr(resp, "output_text", None):
                return (resp.output_text or "").strip()
            if getattr(resp, "response", None):
                return (resp.response or "").strip()
        except Exception:
            return ""
        return ""

    async def inject_vision_system_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """为视觉裁剪请求注入 system_prompt，提示返回 JSON 裁剪框"""
        extra = get_vision_crop_system_prompt()
        try:
            if req.system_prompt:
                req.system_prompt += "\n" + extra
            else:
                req.system_prompt = extra
        except Exception as e:
            # 若修改 system_prompt 失败，则保留原有请求但记录日志以便排查
            logger.warning(f"[LLM_CROP] 注入视觉裁剪 system_prompt 失败: {e}")

    async def llm_detect_and_split(self, image_path: str) -> list[str]:
        """使用视觉 LLM 识别裁剪框后切割，失败返回空列表"""
        if not self.enable_llm_crop:
            logger.debug("[LLM_CROP] 已关闭视觉裁剪开关，跳过识别")
            return []

        # 若未单独配置视觉识别提供商，则不启用，以免占用生图模型
        if not self.vision_provider_id:
            logger.debug("[LLM_CROP] 未配置 vision_provider_id，跳过视觉裁剪")
            return []

        tmp_path: Path | None = None
        try:
            # 读取图片尺寸用于提示
            with PILImage.open(image_path) as img:
                width, height = img.size
            prompt = get_sticker_bbox_prompt(
                rows=self.sticker_bbox_rows,
                cols=self.sticker_bbox_cols,
            )

            # 若图过大，先生成压缩副本以提升识别成功率
            image_urls: list[str] = []
            vision_input_path = image_path
            scale_ratio = 1.0  # 缩放比例，用于将坐标还原到原始尺寸
            try:
                max_side = max(width, height)
                if max_side > 1200:
                    scale_ratio = max_side / 1200  # 还原时需要乘以这个比例
                    new_w = int(width / scale_ratio)
                    new_h = int(height / scale_ratio)
                    with PILImage.open(image_path) as img_to_resize:
                        resized_img = img_to_resize.resize((new_w, new_h))
                        tmp_file = tempfile.NamedTemporaryFile(
                            prefix=f"vision_crop_{Path(image_path).stem}_",
                            suffix=".png",
                            delete=False,
                        )
                        tmp_path = Path(tmp_file.name)
                        tmp_file.close()
                        resized_img.save(tmp_path, format="PNG")
                    vision_input_path = str(tmp_path)
                    logger.debug(
                        f"[LLM_CROP] 生成压缩副本用于识别: {vision_input_path} ({new_w}x{new_h}), scale_ratio={scale_ratio:.2f}"
                    )
            except Exception as e:
                logger.debug(f"[LLM_CROP] 压缩副本生成失败，使用原图: {e}")
                scale_ratio = 1.0

            image_urls = [vision_input_path] if vision_input_path else []
            logger.debug(
                f"[LLM_CROP] 调用视觉模型裁剪: provider={self.vision_provider_id}"
            )
            resp = await self.context.llm_generate(
                chat_provider_id=self.vision_provider_id,
                prompt=prompt,
                image_urls=image_urls,
                max_output_tokens=600,
                timeout=120,
                system_prompt=get_vision_crop_system_prompt(),
            )
            text = self.extract_llm_text(resp)
            if not text:
                return []

            # 尝试解析 JSON 数组

            match = re.search(r"\[.*\]", text, re.S)
            json_str = match.group(0) if match else text
            json_str = json_str.replace("```json", "").replace("```", "").strip()
            bboxes = json.loads(json_str)
            if not isinstance(bboxes, list):
                return []

            # 过滤有效框并按缩放比例还原到原始尺寸
            clean_boxes = []
            for box in bboxes:
                try:
                    x = int(box.get("x", 0))
                    y = int(box.get("y", 0))
                    w = int(box.get("width", 0))
                    h = int(box.get("height", 0))
                except Exception:
                    continue
                if w > 0 and h > 0:
                    # 将坐标按比例还原到原始图片尺寸
                    clean_boxes.append({
                        "x": int(x * scale_ratio),
                        "y": int(y * scale_ratio),
                        "width": int(w * scale_ratio),
                        "height": int(h * scale_ratio),
                    })

            if not clean_boxes:
                return []

            if scale_ratio != 1.0:
                logger.debug(
                    f"[LLM_CROP] 已将 {len(clean_boxes)} 个边界框坐标还原到原始尺寸 (scale_ratio={scale_ratio:.2f})"
                )

            # 调用裁剪工具
            return await asyncio.to_thread(
                split_image,
                image_path,
                rows=6,
                cols=4,
                bboxes=clean_boxes,
            )
        except Exception as e:
            logger.debug(f"视觉识别裁剪失败: {e}")
            return []
        finally:
            if tmp_path:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    async def detect_grid_rows_cols(self, image_path: str) -> tuple[int, int] | None:
        """使用视觉提供商识别网格行列数；失败返回 None"""
        if not self.vision_provider_id:
            return None

        # 视觉识别前确保拿到可读取的本地文件路径（URL 需要先下载）
        local_path = image_path
        if isinstance(image_path, str) and image_path.startswith(
            ("http://", "https://")
        ):
            try:
                if self.api_client and hasattr(self.api_client, "_get_session"):
                    session = await self.api_client._get_session()
                    _, downloaded = await self.api_client._download_image(
                        image_path, session, use_cache=False
                    )
                    if downloaded and Path(downloaded).exists():
                        local_path = downloaded
            except Exception as e:
                logger.debug(f"[GRID_DETECT] 下载图片失败，回退使用原始URL: {e}")

        tmp_path: Path | None = None
        try:
            with PILImage.open(local_path) as img:
                width, height = img.size
                max_side = max(width, height)
                vision_input_path = local_path
                if max_side > 1200:
                    ratio = 1200 / max_side
                    new_w = int(width * ratio)
                    new_h = int(height * ratio)
                    img = img.resize((new_w, new_h))
                    tmp_file = tempfile.NamedTemporaryFile(
                        prefix=f"grid_detect_{Path(local_path).stem}_",
                        suffix=".png",
                        delete=False,
                    )
                    tmp_path = Path(tmp_file.name)
                    tmp_file.close()
                    img.save(tmp_path, format="PNG")
                    vision_input_path = str(tmp_path)
        except Exception as e:
            logger.debug(f"[GRID_DETECT] 读取/压缩图片失败，使用原图: {e}")
            vision_input_path = local_path

        prompt = get_grid_detect_prompt()

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.vision_provider_id,
                prompt=prompt,
                image_urls=[vision_input_path],
                max_output_tokens=200,
                timeout=60,
            )
            text = self.extract_llm_text(resp)
            if not text:
                return None

            match = re.search(r"\{.*\}", text, re.S)
            json_str = match.group(0) if match else text
            data = json.loads(json_str)
            rows = int(data.get("rows", 0))
            cols = int(data.get("cols", 0))
            if (rows == 0 and cols == 0) or rows < 0 or cols < 0:
                logger.debug("[GRID_DETECT] AI 返回 0x0，使用回退切割")
                return None
            # 兼容 AI 返回字符串如 \"4x4\"
            if rows <= 0 or cols <= 0:
                num_match = re.search(r"(\d{1,2})\s*[xX]\s*(\d{1,2})", text)
                if num_match:
                    cols = int(num_match.group(1))
                    rows = int(num_match.group(2))
            if rows > 0 and cols > 0 and rows <= 20 and cols <= 20:
                logger.debug(f"[GRID_DETECT] AI 行列: {cols} x {rows}")
                return rows, cols
        except Exception as e:
            logger.debug(f"[GRID_DETECT] 视觉识别失败: {e}")

        finally:
            if tmp_path:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        return None
