import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _module(name, *, package=False, **attrs):
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_background_notify(monkeypatch, *, modern):
    package = "_background_notify_import_test"
    plugin_tl = Path(__file__).resolve().parents[1] / "tl"
    modules = {}

    def add(name, module):
        modules[name] = module
        if "." in name:
            parent_name, attribute = name.rsplit(".", 1)
            parent = modules.get(parent_name) or sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attribute, module)

    add("astrbot", _module("astrbot", package=True))
    add(
        "astrbot.api",
        _module("astrbot.api", logger=SimpleNamespace(warning=lambda *args: None)),
    )
    add("astrbot.core", _module("astrbot.core", package=True))
    add(package, _module(package, package=True))
    add(f"{package}.core", _module(f"{package}.core", package=True))
    add(
        f"{package}.core.errors",
        _module(f"{package}.core.errors", redact_text=lambda value: str(value)),
    )
    add(
        f"{package}.astrbot_compat",
        _module(f"{package}.astrbot_compat", ASTRBOT_VERSION=None),
    )
    add(
        f"{package}.llm_output",
        _module(f"{package}.llm_output", sanitize_task_result=lambda value: value),
    )
    add(
        f"{package}.plugin_config",
        _module(
            f"{package}.plugin_config",
            get_session_tool_timeout=lambda context, umo: 30,
        ),
    )

    add("astrbot.core.agent", _module("astrbot.core.agent", package=True))
    if modern:
        add(
            "astrbot.core.agent.input",
            _module("astrbot.core.agent.input", PlainInput=object()),
        )
        modules["astrbot.core.astr_main_agent"] = None
    else:
        add(
            "astrbot.core.agent.tool",
            _module("astrbot.core.agent.tool", ToolSet=object()),
        )
        add(
            "astrbot.core.astr_main_agent",
            _module(
                "astrbot.core.astr_main_agent",
                MainAgentBuildConfig=object(),
                _get_session_conv=object(),
                build_main_agent=object(),
            ),
        )
        add(
            "astrbot.core.astr_main_agent_resources",
            _module(
                "astrbot.core.astr_main_agent_resources",
                BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT="{background_task_result}",
            ),
        )
        for parent in ("cron", "platform", "provider", "tools", "utils"):
            name = f"astrbot.core.{parent}"
            add(name, _module(name, package=True))
        add(
            "astrbot.core.cron.events",
            _module("astrbot.core.cron.events", CronMessageEvent=object()),
        )
        add(
            "astrbot.core.platform.message_session",
            _module("astrbot.core.platform.message_session", MessageSession=object()),
        )
        add(
            "astrbot.core.provider.entites",
            _module("astrbot.core.provider.entites", ProviderRequest=object()),
        )
        add(
            "astrbot.core.tools.message_tools",
            _module("astrbot.core.tools.message_tools", SendMessageToUserTool=object()),
        )
        add(
            "astrbot.core.utils.history_saver",
            _module("astrbot.core.utils.history_saver", persist_agent_history=object()),
        )

    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    module_name = f"{package}.background_notify"
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_tl / "background_notify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_import_selects_astrbot_430_agent_service(monkeypatch):
    module = _load_background_notify(monkeypatch, modern=True)

    assert module._OFFICIAL_API_READY is True
    assert module.PlainInput is not None
    assert module.build_main_agent is None


def test_import_falls_back_to_astrbot_429_api(monkeypatch):
    module = _load_background_notify(monkeypatch, modern=False)

    assert module._OFFICIAL_API_READY is True
    assert module.PlainInput is None
    assert module.build_main_agent is not None
