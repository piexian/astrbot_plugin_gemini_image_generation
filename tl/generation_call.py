"""Compatibility wrapper for the plugin's image generation entrypoint."""

from __future__ import annotations

import inspect
from typing import Any


async def invoke_generation_core(plugin: Any, **kwargs: Any):
    """Call the current entrypoint while tolerating older compatibility shims."""
    method = plugin._generate_image_core_internal
    signature = inspect.signature(method)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    call_kwargs = (
        kwargs
        if accepts_kwargs
        else {
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
        }
    )
    return await method(**call_kwargs)
