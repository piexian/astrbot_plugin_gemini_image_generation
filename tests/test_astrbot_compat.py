import sys
import types
from importlib.metadata import PackageNotFoundError

from packaging.version import Version
import pytest

from tl import astrbot_compat


@pytest.mark.parametrize(
    ("raw_version", "modern_config", "async_manager", "stop_registry"),
    [
        ("4.10.4", False, False, False),
        ("4.10.6", False, False, False),
        ("4.11.0", False, True, False),
        ("4.28.2", False, True, False),
        ("4.29.0b1", False, True, False),
        ("4.29.0b2", False, True, True),
        ("4.29.0", False, True, True),
        ("4.29.4", False, True, True),
        ("4.30.0a1", True, False, True),
        ("4.30.0-alpha.1", True, False, True),
        ("4.30.0b1", True, False, True),
        ("4.30.0-beta.1", True, False, True),
    ],
)
def test_feature_boundaries(
    monkeypatch, raw_version, modern_config, async_manager, stop_registry
):
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version(raw_version))

    assert astrbot_compat.has_modern_session_config() is modern_config
    assert astrbot_compat.has_async_session_manager() is async_manager
    assert astrbot_compat.has_background_stop_registry() is stop_registry


def test_unknown_version_has_no_capabilities(monkeypatch):
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", None)

    assert not astrbot_compat.has_modern_session_config()
    assert not astrbot_compat.has_async_session_manager()
    assert not astrbot_compat.has_background_stop_registry()


def test_detect_version_uses_distribution_metadata(monkeypatch):
    monkeypatch.setattr(
        astrbot_compat, "distribution_version", lambda name: "4.30.0-beta.1"
    )

    assert astrbot_compat._detect_astrbot_version() == Version("4.30.0b1")


def test_detect_version_uses_public_cli_without_metadata(monkeypatch):
    def missing_distribution(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(astrbot_compat, "distribution_version", missing_distribution)
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.__path__ = []
    cli_module = types.ModuleType("astrbot.cli")
    cli_module.__version__ = "4.29.0"
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_module)
    monkeypatch.setitem(sys.modules, "astrbot.cli", cli_module)

    assert astrbot_compat._detect_astrbot_version() == Version("4.29.0")
