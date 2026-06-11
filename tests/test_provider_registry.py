from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from tl.provider_loader import load_callable

_PROVIDER_MODULES = (
    "tl.api.google",
    "tl.api.openai_compat",
    "tl.api.zai",
    "tl.api.grok2api",
    "tl.api.agnes_ai",
    "tl.api.xai",
    "tl.api.minimax",
    "tl.api.stepfun",
    "tl.api.openai_images",
    "tl.api.doubao",
    "tl.api.sensenova",
)


def _schema_template_keys() -> list[str]:
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return list(
        schema["provider_settings"]["items"]["provider_overrides"]["templates"].keys()
    )


def test_provider_metadata_import_is_lightweight() -> None:
    for module_name in _PROVIDER_MODULES:
        sys.modules.pop(module_name, None)
    sys.modules.pop("tl.provider_metadata", None)

    provider_metadata = importlib.import_module("tl.provider_metadata")

    assert provider_metadata.iter_api_types()
    assert all(module_name not in sys.modules for module_name in _PROVIDER_MODULES)


def test_provider_specs_match_schema_templates_in_order() -> None:
    from tl.provider_metadata import iter_api_types

    assert list(iter_api_types()) == _schema_template_keys()


def test_provider_paths_and_hook_paths_are_loadable() -> None:
    from tl.provider_metadata import iter_provider_specs

    for spec in iter_provider_specs():
        provider_class = load_callable(spec.provider_path)
        assert callable(provider_class)
        for path in (
            spec.settings_validator_path,
            spec.settings_normalizer_path,
            spec.edit_capability_path,
            spec.candidate_config_hook_path,
            spec.tool_profile_path,
        ):
            if path:
                assert callable(load_callable(path))


def test_registry_unknown_provider_falls_back_to_openai_compat() -> None:
    from tl.api.registry import get_api_provider
    from tl.api.openai_compat import OpenAICompatProvider

    assert isinstance(get_api_provider("unknown"), OpenAICompatProvider)
