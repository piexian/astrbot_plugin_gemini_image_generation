from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

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
    _build_batch_tool_parameters,
    _build_tool_parameters,
    _first_provider_candidate,
    _is_custom_size_tool_mode,
    _resolve_tool_size_params,
)
from tl.plugin_config import ProviderCandidate  # noqa: E402
from tl.provider_settings import provider_tool_profile  # noqa: E402


def _plugin_with_candidates(*candidates):
    return SimpleNamespace(cfg=SimpleNamespace(provider_candidates=list(candidates)))


def _candidate(api_type: str, settings: dict | None = None):
    return SimpleNamespace(api_type=api_type, settings=settings or {})


@pytest.mark.parametrize("placement", ["single", "batch_defaults", "batch_override"])
@pytest.mark.parametrize(
    "options,valid",
    [
        ({}, True),
        ({"quality": "high", "negative_prompt": "noise", "watermark": True}, True),
        ({"quality": "", "negative_prompt": "", "watermark": False}, True),
        ({"quality": None, "negative_prompt": None, "watermark": None}, True),
        ({"quality": True}, False),
        ({"quality": 1}, False),
        ({"quality": {}}, False),
        ({"negative_prompt": False}, False),
        ({"negative_prompt": 1}, False),
        ({"negative_prompt": []}, False),
        ({"watermark": "false"}, False),
        ({"watermark": 0}, False),
        ({"watermark": {}}, False),
        ({"watermark": []}, False),
    ],
)
def test_optional_options_schema_validates_nullable_types(placement, options, valid):
    candidates = [
        ProviderCandidate(id=api_type, api_type=api_type, settings={"model": model})
        for api_type, model in [
            ("openai_images", "gpt-image-1"),
            ("stepfun", "step-image-edit"),
            ("minimax", "image-01"),
        ]
    ]
    plugin = _plugin_with_candidates(*candidates)
    if placement == "single":
        schema = _build_tool_parameters(plugin)
        payload = {"prompt": "draw", "options": options}
    else:
        schema = _build_batch_tool_parameters(plugin)
        task = {"name": "one", "prompt": "draw", "image_count": 1}
        payload = {"tasks": [task]}
        if placement == "batch_defaults":
            payload["options"] = options
        else:
            task["options"] = options

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    if valid:
        validator.validate(payload)
    else:
        with pytest.raises(ValidationError):
            validator.validate(payload)


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


def test_explicit_unmatched_route_does_not_fallback_to_first_candidate() -> None:
    first = _candidate(
        "openai_images",
        {"size_mode": "custom", "custom_size": "1536x1024"},
    )
    plugin = _plugin_with_candidates(first, _candidate("google"))

    assert _first_provider_candidate(plugin) is first
    assert _first_provider_candidate(plugin, provider="missing") is None
    assert _first_provider_candidate(plugin, model="missing") is None
    assert _resolve_tool_size_params(
        plugin,
        provider="missing",
    ) == (None, None, None)


def test_tool_schema_omits_legacy_size() -> None:
    plugin = _plugin_with_candidates()
    params = _build_batch_tool_parameters(plugin)
    batch_properties = params["properties"]["tasks"]["items"]["properties"]
    description = (
        params["properties"]["resolution"]["description"]
        + params["properties"]["aspect_ratio"]["description"]
    )

    assert "size" not in params["properties"]
    assert "template" in params["properties"]
    assert "references" in params["properties"]
    assert "size" not in batch_properties
    assert "size" not in description
    assert "分辨率" in description
    assert "长宽比" in description


def test_custom_size_mode_keeps_resolution_and_configured_default() -> None:
    plugin = _plugin_with_candidates(
        _candidate(
            "openai_images",
            {"size_mode": "custom", "custom_size": "1536x1024"},
        ),
    )

    assert _resolve_tool_size_params(plugin) == ("1536x1024", None, None)
    assert _resolve_tool_size_params(plugin, resolution="2K", aspect_ratio="16:9") == (
        "2K",
        "16:9",
        None,
    )
