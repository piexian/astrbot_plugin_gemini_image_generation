"""Lazy import helpers for provider specs."""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=None)
def load_callable(path: str) -> Any:
    """Load a callable/class from a fully-qualified dotted path."""
    module_name, separator, attr_name = path.rpartition(".")
    if not separator or not module_name or not attr_name:
        raise ValueError(f"Invalid callable path: {path!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)
