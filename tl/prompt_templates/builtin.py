"""内置快速模式模板。正文来自原有提示词，用户输入统一走渲染规则。"""

from __future__ import annotations

from .models import PromptTemplate, validate_template_data

BUILTIN_TIMESTAMP = "2026-01-01T00:00:00Z"

_PVC_CONTENT = """Create a high-quality 1/7 scale PVC figure based on the following description.
The figure should be positioned on a circular plastic display base with a collectible box in the background.
The box should feature a large clear window displaying the main artwork, product name, brand logo, barcode, and specifications panel.
Include a small price tag on the corner of the box.
Place a computer monitor in the background showing the ZBrush modeling process of this figure.

Figure requirements:
- Realistic 3D appearance with proper PVC texture detail
- Natural pose with accurate body proportions
- High-quality sculpting with no execution errors
- If original is not full body, extend to full figure version
- Expressions and poses must match the description exactly
- Head should not be oversized, legs should not be too short
- Avoid cartoonish proportions unless specified as chibi style
- For animal figures, reduce fur texture realism to look more like collectible figures
- No outline strokes, must be fully 3D dimensional
- Proper perspective with foreground/background relationship

Subject: {{input}}

Technical specifications:
- Professional photography lighting setup
- High-resolution rendering with detailed textures
- Realistic material properties and reflections
- Collectible figure presentation style"""

_STICKER_CONTENT = """为我生成图中角色的绘制 Q 版的，LINE 风格的半身像表情包，注意头饰要正确
彩色手绘风格，严格按照{{rows}}*{{cols}}布局，均匀分布，白色背景，涵盖各种各样的常用聊天语句，或是一些有关的娱乐 meme
其他需求：不要原图复制，高清修复，高质量。所有标注为手写的简体中文。"""

_STICKER_SIMPLE_CONTENT = """Generate a Q version drawing of the characters in the image, in LINE style, with half-body expressions, ensuring the headgear is correct.

Color hand-drawn style, strictly following a {{rows}}*{{cols}} layout with uniform spacing and even distribution. White background. Cover a variety of commonly used chat phrases and related entertainment memes.

Layout requirements:
- Ensure each sticker is evenly spaced across the grid
- Do not crowd or overlap expressions
- Maintain consistent margins between each cell
- Leave adequate padding around each character

Other requirements: Do not copy the original image, high-definition restoration, high quality. All annotations should be simple symbols or in English."""

_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "id": "avatar",
        "name": "头像",
        "ai_description": "生成适合圆形裁剪的社交媒体头像，主体居中、背景简洁。",
        "content": "设计一个高质量的社交媒体头像，构图居中，主体清晰，背景简洁大方，适合圆形裁剪，具有极高的辨识度。",
        "defaults": {"resolution": "1K", "aspect_ratio": "1:1"},
        "reference": "optional",
    },
    {
        "id": "poster",
        "name": "海报",
        "ai_description": "生成电影级宣传海报，强调排版、构图和色彩。",
        "content": "设计一张具有视觉冲击力的电影级宣传海报，排版专业，构图富有张力，色彩搭配具有艺术感，细节丰富。",
        "defaults": {"resolution": "2K", "aspect_ratio": "16:9"},
        "reference": "optional",
    },
    {
        "id": "wallpaper",
        "name": "壁纸",
        "ai_description": "生成超高清电脑桌面壁纸，画面开阔、光影细腻。",
        "content": "创作一张 4K 超高清电脑桌面壁纸，画面细腻，视野开阔，光影效果绝佳，令人赏心悦目，无噪点。",
        "defaults": {"resolution": "4K", "aspect_ratio": "16:9"},
        "reference": "optional",
    },
    {
        "id": "card",
        "name": "卡片",
        "ai_description": "生成适合明信片或收藏卡的精致插画。",
        "content": "设计一张精致的卡片插画，风格唯美，色彩清新，适合作为明信片或收藏卡，画面具有故事感。",
        "defaults": {"resolution": "1K", "aspect_ratio": "3:2"},
        "reference": "optional",
    },
    {
        "id": "mobile",
        "name": "手机壁纸",
        "ai_description": "生成竖屏手机壁纸，主体避开时间显示区域。",
        "content": "设计一张竖屏手机壁纸，构图完美适配手机屏幕，主体位置合理不遮挡时间显示，视觉效果惊艳。",
        "defaults": {"resolution": "2K", "aspect_ratio": "9:16"},
        "reference": "optional",
    },
    {
        "id": "figure-pvc",
        "name": "手办化（PVC）",
        "ai_description": "按描述生成 1/7 比例的 PVC 收藏级手办展示图。",
        "content": _PVC_CONTENT,
        "defaults": {"resolution": "2K", "aspect_ratio": "3:2"},
        "reference": "optional",
    },
    {
        "id": "figure-gk",
        "name": "手办化（树脂 GK）",
        "ai_description": "把角色重塑为收藏级树脂手办，强调材质、光影和摄影质感。",
        "content": "将画面中的角色重塑为顶级收藏级树脂手办，全身动态姿势，置于角色主题底座，高精度材质，手工涂装，肌肤纹理与服装材质真实分明。戏剧性硬光为主光源，凸显立体感，无过曝；强效补光消除死黑，细节完整可见。背景为窗边景深模糊，侧后方隐约可见产品包装盒。博物馆级摄影质感，全身细节无损，面部结构精准。禁止：任何2D元素或照搬原图、塑料感、面部模糊、五官错位、细节丢失。",
        "defaults": {"resolution": "2K", "aspect_ratio": "3:2"},
        "reference": "optional",
    },
    {
        "id": "sticker",
        "name": "表情包（中文 Q 版）",
        "ai_description": "按参考图生成中文 Q 版网格表情包，需要角色参考图。",
        "content": _STICKER_CONTENT,
        "defaults": {"resolution": "4K", "aspect_ratio": "16:9"},
        "reference": "required",
    },
    {
        "id": "sticker-simple",
        "name": "表情包（英文 Q 版）",
        "ai_description": "按参考图生成英文标注的 Q 版网格表情包，需要角色参考图。",
        "content": _STICKER_SIMPLE_CONTENT,
        "defaults": {"resolution": "4K", "aspect_ratio": "16:9"},
        "reference": "required",
    },
)


def _build(definition: dict[str, object]) -> PromptTemplate:
    payload = {
        **definition,
        "origin": "builtin",
        "ai_enabled": True,
        "created_at": BUILTIN_TIMESTAMP,
        "updated_at": BUILTIN_TIMESTAMP,
    }
    return validate_template_data(payload, origin="builtin")


_BUILTIN = tuple(_build(item) for item in _DEFINITIONS)
_BY_ID = {item.id: item for item in _BUILTIN}


def builtin_templates() -> tuple[PromptTemplate, ...]:
    return _BUILTIN


def builtin_template(template_id: str) -> PromptTemplate:
    try:
        return _BY_ID[template_id]
    except KeyError:
        raise KeyError(template_id) from None


def is_builtin_id(template_id: str) -> bool:
    return template_id in _BY_ID
