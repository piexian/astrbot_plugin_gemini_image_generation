"""Shared image-size clamping helpers for provider request building."""

from __future__ import annotations

import math


def clamp_side(value: float, *, min_side: int, max_side: int, align: int = 1) -> int:
    """Round one side to a multiple of align and clamp it into [min_side, max_side]."""
    aligned = round(value / align) * align
    return max(min_side, min(max_side, aligned))


def size_from_budget(
    budget: int,
    ratio_w: int,
    ratio_h: int,
    *,
    min_side: int = 512,
    max_side: int = 4096,
    align: int = 16,
) -> tuple[int, int]:
    """Derive a (width, height) pair from a total-pixel budget and aspect ratio."""
    height = round(math.sqrt(budget / (ratio_w / ratio_h)))
    width = round(height * ratio_w / ratio_h)
    return (
        clamp_side(width, min_side=min_side, max_side=max_side, align=align),
        clamp_side(height, min_side=min_side, max_side=max_side, align=align),
    )
