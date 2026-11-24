"""AstrBot 版本探测与后台通知能力边界。"""

from importlib.metadata import PackageNotFoundError, version as distribution_version

from packaging.version import InvalidVersion, Version

_MODERN_SESSION_CONFIG = Version("4.30.0a1")
_ASYNC_SESSION_MANAGER = Version("4.11.0")
_BACKGROUND_STOP_REGISTRY = Version("4.29.0b2")


def _detect_astrbot_version() -> Version | None:
    """优先读取安装包版本，源码运行时读取 AstrBot 公开版本号。"""
    try:
        raw_version = distribution_version("astrbot")
    except PackageNotFoundError:
        try:
            from astrbot.cli import __version__ as raw_version
        except ImportError:
            return None
    try:
        return Version(raw_version)
    except InvalidVersion:
        return None


ASTRBOT_VERSION = _detect_astrbot_version()


def has_modern_session_config() -> bool:
    return ASTRBOT_VERSION is not None and ASTRBOT_VERSION >= _MODERN_SESSION_CONFIG


def has_async_session_manager() -> bool:
    return (
        ASTRBOT_VERSION is not None
        and _ASYNC_SESSION_MANAGER <= ASTRBOT_VERSION < _MODERN_SESSION_CONFIG
    )


def has_background_stop_registry() -> bool:
    return ASTRBOT_VERSION is not None and ASTRBOT_VERSION >= _BACKGROUND_STOP_REGISTRY
