"""Shared helpers for reading provider candidate settings."""

from __future__ import annotations

from typing import Any

from .api_normalize import normalize_api_type
from .provider_loader import load_callable
from .provider_metadata import get_provider_spec


def _cfg(obj: Any) -> Any:
    return getattr(obj, "cfg", obj)


def first_provider_candidate(config: Any, api_type: str | None = None) -> Any | None:
    """Return the first configured candidate, optionally constrained by api_type."""
    cfg = _cfg(config)
    target = normalize_api_type(api_type) if api_type else ""
    for candidate in getattr(cfg, "provider_candidates", []) or []:
        candidate_type = normalize_api_type(getattr(candidate, "api_type", ""))
        if not target or candidate_type == target:
            return candidate
    return None


def first_provider_settings(config: Any, api_type: str) -> dict[str, Any]:
    """Return first settings for a provider without hard-coding legacy fields."""
    cfg = _cfg(config)
    target = normalize_api_type(api_type)

    candidate = first_provider_candidate(cfg, target)
    if candidate is not None:
        settings = getattr(candidate, "settings", None)
        return settings if isinstance(settings, dict) else {}

    settings_by_type = getattr(cfg, "provider_settings_by_type", None) or {}
    values = settings_by_type.get(target) or []
    if values:
        settings = values[0]
        return settings if isinstance(settings, dict) else {}

    spec = get_provider_spec(target)
    if spec and spec.settings_attr:
        settings = getattr(cfg, spec.settings_attr, None)
        if isinstance(settings, dict) and settings:
            return settings

    overrides = getattr(cfg, "provider_overrides", None) or {}
    if isinstance(overrides, dict):
        for key, settings in overrides.items():
            key_type = normalize_api_type(str(key).split("#", 1)[0])
            if key_type == target and isinstance(settings, dict):
                return settings

    return {}


def provider_tool_profile(config: Any, api_type: str) -> dict[str, Any]:
    """Return tool behavior profile for the active first provider candidate."""
    target = normalize_api_type(api_type)
    candidate = first_provider_candidate(config)
    if normalize_api_type(getattr(candidate, "api_type", "")) != target:
        return {"active": False, "settings": {}}

    settings = getattr(candidate, "settings", None)
    if not isinstance(settings, dict):
        settings = first_provider_settings(config, target)

    spec = get_provider_spec(target)
    if spec and spec.tool_profile_path:
        profile = load_callable(spec.tool_profile_path)(config, settings)
        if isinstance(profile, dict):
            profile.setdefault("active", True)
            profile.setdefault("settings", settings)
            return profile

    return {"active": True, "settings": settings}


def first_provider_tool_profile(config: Any) -> dict[str, Any]:
    """Return tool profile for the active first provider candidate."""
    candidate = first_provider_candidate(config)
    if candidate is None:
        return {"active": False, "settings": {}}

    api_type = normalize_api_type(getattr(candidate, "api_type", ""))
    settings = getattr(candidate, "settings", None)
    if not isinstance(settings, dict):
        settings = first_provider_settings(config, api_type)

    spec = get_provider_spec(api_type)
    if spec and spec.tool_profile_path:
        profile = load_callable(spec.tool_profile_path)(config, settings)
        if isinstance(profile, dict):
            profile.setdefault("active", True)
            profile.setdefault("settings", settings)
            return profile

    return {"active": False, "settings": settings}
