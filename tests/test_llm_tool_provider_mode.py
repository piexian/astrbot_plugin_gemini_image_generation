from __future__ import annotations

import sys
import types
from types import SimpleNamespace

if "mcp" not in sys.modules:
    mcp_module = types.ModuleType("mcp")
    mcp_types_module = types.ModuleType("mcp.types")
    mcp_module.types = mcp_types_module
    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.types"] = mcp_types_module

core_module = types.ModuleType("astrbot.core")
agent_module = types.ModuleType("astrbot.core.agent")
run_context_module = types.ModuleType("astrbot.core.agent.run_context")
tool_module = types.ModuleType("astrbot.core.agent.tool")
context_module = types.ModuleType("astrbot.core.astr_agent_context")


class _ContextWrapper:
    pass


class _AstrAgentContext:
    pass


class _ToolExecResult:
    pass


class _FunctionTool:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        return None


run_context_module.ContextWrapper = _ContextWrapper
tool_module.FunctionTool = _FunctionTool
tool_module.ToolExecResult = _ToolExecResult
context_module.AstrAgentContext = _AstrAgentContext
sys.modules["astrbot.core"] = core_module
sys.modules["astrbot.core.agent"] = agent_module
sys.modules["astrbot.core.agent.run_context"] = run_context_module
sys.modules["astrbot.core.agent.tool"] = tool_module
sys.modules["astrbot.core.astr_agent_context"] = context_module

from tl.llm_tools import (  # noqa: E402
    _build_tool_parameters,
    _is_custom_size_tool_mode,
    _resolve_tool_size_params,
)
from tl.openai_image_size import CUSTOM_SIZE_DEFAULT  # noqa: E402
from tl.provider_settings import provider_tool_profile  # noqa: E402


def _plugin_with_candidates(*candidates):
    return SimpleNamespace(cfg=SimpleNamespace(provider_candidates=list(candidates)))


def _candidate(api_type: str, settings: dict | None = None):
    return SimpleNamespace(api_type=api_type, settings=settings or {})


def test_openai_custom_size_tool_mode_requires_first_candidate() -> None:
    plugin = _plugin_with_candidates(
        _candidate("google", {"resolution": "2K"}),
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        ),
    )

    assert _is_custom_size_tool_mode(plugin) is False

    params = _build_tool_parameters(plugin)

    assert "size" not in params["properties"]
    assert "resolution" in params["properties"]
    assert "aspect_ratio" in params["properties"]


def test_provider_tool_profile_uses_first_matching_candidate() -> None:
    plugin = _plugin_with_candidates(
        _candidate("google", {"resolution": "2K"}),
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        ),
    )

    profile = provider_tool_profile(plugin, "openai_images")

    assert profile["active"] is True
    assert profile["custom_size_mode"] is True
    assert profile["settings"]["custom_size"] == "1536x1024"


def test_openai_custom_size_tool_mode_uses_first_candidate_settings() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        ),
        _candidate("google", {"resolution": "2K"}),
    )

    assert _is_custom_size_tool_mode(plugin) is True

    params = _build_tool_parameters(plugin)

    assert "size" not in params["properties"]
    assert "resolution" in params["properties"]
    assert "aspect_ratio" in params["properties"]


def test_openai_custom_size_tool_params_keep_preset_controls() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        )
    )

    resolution, aspect_ratio, notice = _resolve_tool_size_params(
        plugin,
        resolution="2k",
        aspect_ratio="16:9",
    )

    assert resolution == "2K"
    assert aspect_ratio == "16:9"
    assert notice is None


def test_openai_custom_size_tool_params_fallback_to_default_on_invalid_inputs() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        )
    )

    resolution, aspect_ratio, notice = _resolve_tool_size_params(
        plugin,
        resolution="bad",
        aspect_ratio="bad",
    )

    assert resolution == "1536x1024"
    assert aspect_ratio is None
    assert notice is None


def test_openai_custom_size_tool_params_fallback_when_one_input_is_invalid() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        )
    )

    resolution, aspect_ratio, notice = _resolve_tool_size_params(
        plugin,
        resolution="2K",
        aspect_ratio="bad",
    )

    assert resolution == "1536x1024"
    assert aspect_ratio is None
    assert notice is None


def test_openai_custom_size_tool_params_fallback_when_config_invalid() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "2048x1080"},
        )
    )

    resolution, aspect_ratio, notice = _resolve_tool_size_params(plugin)

    assert resolution == CUSTOM_SIZE_DEFAULT
    assert aspect_ratio is None
    assert notice is None
