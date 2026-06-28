"""Lazy import helpers for provider specs."""

from __future__ import annotations

import importlib
from functools import cache
from typing import Any

# This module is loaded as ``<pkg>.tl.provider_loader`` (e.g.
# ``data.plugins.astrbot_plugin_gemini_image_generation.tl.provider_loader``
# under AstrBot, or just ``tl.provider_loader`` in dev). ``__package__`` is
# therefore the real, fully-qualified name of what the codebase calls ``tl``.
# Spec dotted paths are written as ``tl.<module>.<attr>``; rewrite their
# leading ``tl`` to that real name so they resolve both when ``tl`` is a
# top-level package (dev) and when it is a plugin submodule (AstrBot).
_TL_QUALIFIED = __package__ or __name__.rpartition(".")[0]


@cache
def load_callable(path: str) -> Any:
    """Load a callable/class from a ``tl.<module>.<attr>`` dotted path.

    The leading ``tl`` is resolved against this package's real import name, so
    the same path works whether ``tl`` is a top-level package (dev) or a
    submodule of the plugin package (e.g. ``data.plugins.<plugin>.tl``).
    """
    module_name, separator, attr_name = path.rpartition(".")
    if not separator or not module_name or not attr_name:
        raise ValueError(f"Invalid callable path: {path!r}")
    if module_name == "tl" or module_name.startswith("tl."):
        module_name = _TL_QUALIFIED + module_name[2:]
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)
