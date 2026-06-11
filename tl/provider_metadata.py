"""Canonical provider specs and capability helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .api_normalize import normalize_api_type


@dataclass(frozen=True)
class ProviderSpec:
    """Lightweight provider registration metadata."""

    api_type: str
    provider_path: str
    supports_image_edit: bool = True
    settings_attr: str | None = None
    model_field: str = "model"
    settings_validator_path: str | None = None
    settings_normalizer_path: str | None = None
    edit_capability_path: str | None = None
    candidate_config_hook_path: str | None = None
    tool_profile_path: str | None = None
    rebuild_on_retry: bool = False
    retry_error_arg: bool = False
    parse_errors_with_provider: bool = False


_PROVIDER_SPECS: Final[tuple[ProviderSpec, ...]] = (
    ProviderSpec("google", "tl.api.google.GoogleProvider"),
    ProviderSpec("openai", "tl.api.openai_compat.OpenAICompatProvider"),
    ProviderSpec("zai", "tl.api.zai.ZaiProvider"),
    ProviderSpec("grok2api", "tl.api.grok2api.Grok2ApiProvider"),
    ProviderSpec(
        "agnes_ai",
        "tl.api.agnes_ai.AgnesAIProvider",
        settings_attr="agnes_ai_settings",
    ),
    ProviderSpec("xai", "tl.api.xai.XAIProvider", settings_attr="xai_settings"),
    ProviderSpec(
        "minimax",
        "tl.api.minimax.MiniMaxProvider",
        settings_attr="minimax_settings",
        rebuild_on_retry=True,
        retry_error_arg=True,
    ),
    ProviderSpec(
        "stepfun",
        "tl.api.stepfun.StepfunProvider",
        settings_attr="stepfun_settings",
    ),
    ProviderSpec(
        "openai_images",
        "tl.api.openai_images.OpenAIImagesProvider",
        settings_attr="openai_images_settings",
        settings_validator_path="tl.provider_hooks.validate_openai_images_settings",
        edit_capability_path="tl.provider_hooks.openai_images_edit_capability",
        candidate_config_hook_path="tl.provider_hooks.openai_images_candidate_config",
        tool_profile_path="tl.provider_hooks.openai_images_tool_profile",
    ),
    ProviderSpec(
        "doubao",
        "tl.api.doubao.DoubaoProvider",
        settings_attr="doubao_settings",
        model_field="endpoint_id",
        settings_normalizer_path="tl.provider_hooks.normalize_doubao_settings",
        rebuild_on_retry=True,
        parse_errors_with_provider=True,
    ),
    ProviderSpec(
        "sensenova",
        "tl.api.sensenova.SenseNovaProvider",
        supports_image_edit=False,
        settings_attr="sensenova_settings",
    ),
)

PROVIDER_TYPES: Final[tuple[str, ...]] = tuple(
    spec.api_type for spec in _PROVIDER_SPECS
)
IMAGE_EDIT_CAPABLE_TYPES: Final[frozenset[str]] = frozenset(
    spec.api_type for spec in _PROVIDER_SPECS if spec.supports_image_edit
)
_PROVIDER_SPEC_BY_TYPE: Final[dict[str, ProviderSpec]] = {
    spec.api_type: spec for spec in _PROVIDER_SPECS
}


def iter_provider_specs() -> tuple[ProviderSpec, ...]:
    """Return registered provider specs in schema order."""
    return _PROVIDER_SPECS


def get_provider_spec(api_type: Any) -> ProviderSpec | None:
    """Return provider spec for a canonical or user-provided provider key."""
    return _PROVIDER_SPEC_BY_TYPE.get(normalize_api_type(api_type))


def is_known_api_type(api_type: Any) -> bool:
    """Return whether the value is a registered provider key."""
    return get_provider_spec(api_type) is not None


def iter_api_types() -> tuple[str, ...]:
    """Return registered provider keys in schema order."""
    return PROVIDER_TYPES


def supports_image_edit(api_type: Any) -> bool:
    """Return whether the provider can process reference images by default."""
    spec = get_provider_spec(api_type)
    return bool(spec and spec.supports_image_edit)
