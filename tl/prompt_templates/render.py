"""模板渲染。占位符只替换一次，用户输入里的同名文本不再展开。"""

from __future__ import annotations

import re

from .models import PromptTemplate

_PLACEHOLDER = re.compile(r"\{\{(input|rows|cols)\}\}")


def render_template(
    template: PromptTemplate, input_text: str = "", *, rows: int = 4, cols: int = 4
) -> str:
    text = str(input_text or "").strip()
    values = {"input": text, "rows": str(int(rows)), "cols": str(int(cols))}
    content = template.content.strip()
    rendered = _PLACEHOLDER.sub(lambda match: values[match.group(1)], content)
    if "{{input}}" not in template.content and text:
        rendered = f"{rendered}\n用户需求：{text}"
    return rendered
