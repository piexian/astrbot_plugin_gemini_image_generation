from __future__ import annotations

import ast
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from tl import enhanced_prompts
from tl.enhanced_prompts import get_figure_prompt


def figure_handler(method_name="quick_figure"):
    """Exercise the real command method without registering a plugin instance."""
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(
        node
        for node in plugin.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name
    )
    method.decorator_list = []
    tree.body = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        ),
        method,
    ]
    namespace = {"shlex": shlex, "get_figure_prompt": get_figure_prompt}
    namespace.update(
        {
            name: getattr(enhanced_prompts, name)
            for name in (
                "get_avatar_prompt",
                "get_poster_prompt",
                "get_wallpaper_prompt",
                "get_card_prompt",
                "get_mobile_prompt",
            )
        }
    )
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace[method_name]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argument, style, description",
    [(None, 1, ""), ("2", 2, ""), ("GK 戴眼镜", 2, "戴眼镜")],
)
async def test_figure_accepts_image_without_prompt_and_keeps_optional_style(
    argument, style, description
):
    event = SimpleNamespace(images=["attached-image.png"])
    received = []

    async def handle(*args, **kwargs):
        received.append((args, kwargs))
        yield "generated"

    plugin = SimpleNamespace(
        _extract_prompt_from_message=lambda event, prompt, *commands: prompt,
        _parse_generation_route=lambda prompt: (prompt, None, None),
        _handle_quick_mode=handle,
    )
    handler = figure_handler()
    results = (
        handler(plugin, event) if argument is None else handler(plugin, event, argument)
    )
    assert [result async for result in results] == ["generated"]
    args, _ = received[0]
    assert args[0] is event
    assert args[0].images == ["attached-image.png"]
    assert args[1] == get_figure_prompt(description, style)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["avatar", "poster", "wallpaper", "card", "mobile"])
@pytest.mark.parametrize("description", [None, "可爱风格"])
async def test_other_presets_accept_optional_description(mode, description):
    event = SimpleNamespace(images=["attached-image.png"])
    received = []

    async def handle(*args, **kwargs):
        received.append(args)
        yield "generated"

    plugin = SimpleNamespace(
        _extract_prompt_from_message=lambda event, prompt, *commands: prompt,
        _handle_quick_mode=handle,
    )
    handler = figure_handler("quick_" + mode)
    results = (
        handler(plugin, event)
        if description is None
        else handler(plugin, event, description)
    )
    assert [result async for result in results] == ["generated"]
    args = received[0]
    assert args[0] is event
    assert args[1] == (description or "")
    prompt = args[6](args[1])
    assert prompt == getattr(enhanced_prompts, "get_" + mode + "_prompt")(
        description or ""
    )
    assert prompt.strip()
