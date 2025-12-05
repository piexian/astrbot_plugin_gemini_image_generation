"""
帮助页面渲染模块
支持三种渲染模式：html (t2i)、local (Pillow)、text (纯文本)
"""

import io
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger


def get_template_path(
    templates_dir: str | Path,
    theme_settings: dict,
    extension: str = ".html",
) -> Path:
    """根据主题配置获取模板路径"""
    mode = theme_settings.get("mode", "cycle")
    cycle_config = theme_settings.get("cycle_config", {})
    single_config = theme_settings.get("single_config", {})

    template_filename = "help_template_light"

    if mode == "single":
        template_filename = single_config.get("template_name", "help_template_light")
    else:
        day_start = cycle_config.get("day_start", 6)
        day_end = cycle_config.get("day_end", 18)
        day_template = cycle_config.get("day_template", "help_template_light")
        night_template = cycle_config.get("night_template", "help_template_dark")

        current_hour = datetime.now().hour
        if day_start <= current_hour < day_end:
            template_filename = day_template
        else:
            template_filename = night_template

    if not template_filename.endswith(extension):
        template_filename += extension

    template_path = Path(templates_dir) / template_filename

    if not template_path.exists():
        logger.warning(f"模板文件不存在: {template_path}，回退到默认模板")
        template_filename = f"help_template_light{extension}"
        template_path = Path(templates_dir) / template_filename

    return template_path


def render_text(template_data: dict) -> str:
    """纯文本渲染"""
    return f"""🎨 {template_data.get("title", "Gemini 图像生成插件")}

基础指令:
• /生图 [描述] - 生成图像
• /快速 [预设] [描述] - 快速模式
• /改图 [描述] - 修改图像
• /换风格 [风格] - 风格转换
• /生图帮助 - 显示帮助

预设选项: 头像/海报/壁纸/卡片/手机/手办化

当前配置:
• 模型: {template_data.get("model", "N/A")}
• 分辨率: {template_data.get("resolution", "N/A")}
• API密钥: {template_data.get("api_keys_count", 0)}个
• LLM工具超时: {template_data.get("tool_timeout", 60)}秒

系统状态:
• 搜索接地: {template_data.get("grounding_status", "✗ 禁用")}
• 自动头像: {template_data.get("avatar_status", "✗ 禁用")}
• 智能重试: {template_data.get("smart_retry_status", "✗ 禁用")}"""


def render_local_pillow(
    templates_dir: str | Path,
    theme_settings: dict,
    template_data: dict,
) -> bytes:
    """使用 Pillow 本地渲染 Markdown 模板为图片"""
    template_path = get_template_path(templates_dir, theme_settings, ".md")

    if not template_path.exists():
        # 回退到默认 md 模板
        template_path = Path(templates_dir) / "help_template.md"

    if template_path.exists():
        with open(template_path, encoding="utf-8") as f:
            md_content = f.read()
        # 简单模板变量替换
        for key, value in template_data.items():
            md_content = md_content.replace("{{ " + key + " }}", str(value))
            md_content = md_content.replace("{{" + key + "}}", str(value))
    else:
        md_content = render_text(template_data)

    # 判断深色/浅色主题
    is_dark = "dark" in str(template_path).lower()
    bg_color = (30, 30, 30) if is_dark else (255, 255, 255)
    text_color = (220, 220, 220) if is_dark else (30, 30, 30)
    heading_color = (100, 180, 255) if is_dark else (0, 100, 200)

    # 渲染参数
    width = 600
    padding = 30
    line_height = 28
    heading_height = 36

    # 计算行数
    lines = md_content.strip().split("\n")
    total_height = padding * 2

    for line in lines:
        if line.startswith("#"):
            total_height += heading_height
        else:
            total_height += line_height

    total_height = max(total_height, 400)

    # 创建图片
    img = Image.new("RGB", (width, total_height), bg_color)
    draw = ImageDraw.Draw(img)

    # 尝试加载字体
    font_size = 16
    heading_font_size = 20
    try:
        # 尝试常见中文字体路径
        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
        ]
        font = None
        heading_font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, font_size)
                heading_font = ImageFont.truetype(fp, heading_font_size)
                break
        if font is None:
            font = ImageFont.load_default()
            heading_font = font
    except Exception:
        font = ImageFont.load_default()
        heading_font = font

    y = padding
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            # 二级标题
            text = stripped.lstrip("#").strip()
            draw.text((padding, y), text, font=heading_font, fill=heading_color)
            y += heading_height
        elif stripped.startswith("#"):
            # 一级标题
            text = stripped.lstrip("#").strip()
            draw.text((padding, y), text, font=heading_font, fill=heading_color)
            y += heading_height
        elif stripped.startswith("-") or stripped.startswith("•"):
            # 列表项
            text = "• " + stripped.lstrip("-•").strip()
            draw.text((padding + 10, y), text, font=font, fill=text_color)
            y += line_height
        elif stripped.startswith(">"):
            # 引用
            text = stripped.lstrip(">").strip()
            draw.text((padding + 20, y), text, font=font, fill=(200, 150, 50))
            y += line_height
        elif stripped.startswith("{%") or stripped.startswith("{{"):
            # 跳过 Jinja2 控制语句
            continue
        elif stripped:
            draw.text((padding, y), stripped, font=font, fill=text_color)
            y += line_height
        else:
            y += line_height // 2

    # 输出为 PNG bytes
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()
