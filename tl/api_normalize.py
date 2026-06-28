"""API provider key normalization helpers."""

from __future__ import annotations

from typing import Any


def normalize_api_type(api_type: Any) -> str:
    """Normalize an API type to the canonical provider key."""
    return str(api_type or "").strip().lower().replace("-", "_")
