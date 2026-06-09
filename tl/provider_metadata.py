"""Canonical provider names and capability helpers."""

from __future__ import annotations

from typing import Any, Final

PROVIDER_TYPES: Final[tuple[str, ...]] = (
    "google",
    "openai",
    "openai_images",
    "agnes_ai",
    "xai",
    "minimax",
    "stepfun",
    "sensenova",
    "zai",
    "grok2api",
    "doubao",
)

IMAGE_EDIT_CAPABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "google",
        "openai",
        "openai_images",
        "agnes_ai",
        "xai",
        "minimax",
        "stepfun",
        "zai",
        "grok2api",
        "doubao",
    }
)


def normalize_api_type(api_type: Any) -> str:
    """Normalize an API type to the canonical provider key."""
    return str(api_type or "").strip().lower().replace("-", "_")


def is_known_api_type(api_type: Any) -> bool:
    """Return whether the value is a registered provider key."""
    return normalize_api_type(api_type) in PROVIDER_TYPES


def iter_api_types() -> tuple[str, ...]:
    """Return registered provider keys in schema order."""
    return PROVIDER_TYPES


def supports_image_edit(api_type: Any) -> bool:
    """Return whether the provider can process reference images by default."""
    return normalize_api_type(api_type) in IMAGE_EDIT_CAPABLE_TYPES
