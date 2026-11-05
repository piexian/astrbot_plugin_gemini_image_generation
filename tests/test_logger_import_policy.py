"""守门：非测试源码里的 logger 只能来自 `from astrbot.api import logger`。

纯 AST 静态检查，不导入被测模块，astrbot 未安装也可运行。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 允许直接使用标准库 logging 的例外白名单：文件名 -> 理由。
# 例："tl/foo.py": "宿主日志装配 API，不创建插件记录器"
# 只豁免标准库检查，logger 仍必须由 astrbot 提供。
LOGGING_WHITELIST: dict[str, str] = {}

ALLOWED_MODULE = "astrbot.api"

# 自建记录器或装配宿主日志的调用签名，命中即违规。
_LOGGING_CALLEES = frozenset(
    {
        "getLogger",
        "getLoggerAdapter",
        "basicConfig",
        "dictConfig",
        "fileConfig",
        "logging",
    }
)


def _source_files() -> list[Path]:
    files = {ROOT / "main.py", *(ROOT / "tl").rglob("*.py")}
    return sorted(
        (path for path in files if path.is_file()), key=lambda path: path.as_posix()
    )


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _imports_logging(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == "logging" or alias.name.startswith("logging.")
            for alias in node.names
        )
    module = node.module or ""
    return module == "logging" or module.startswith("logging.")


def _scan(path: Path, *, allow_logging: bool) -> list[str]:
    """返回该文件的违规描述；allow_logging 为白名单文件的豁免开关。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    astrbot_logger = False
    uses_logger = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if _imports_logging(node) and not allow_logging:
                name = getattr(node, "module", None) or node.names[0].name
                problems.append(
                    f"第 {node.lineno} 行导入标准库 {name}：禁止自建 logger"
                )
                continue
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "logger" for alias in node.names
            ):
                if node.module != ALLOWED_MODULE:
                    problems.append(
                        f"第 {node.lineno} 行从 {node.module or ''} 导入 logger："
                        f"只能 from {ALLOWED_MODULE} import logger"
                    )
                elif any(
                    alias.name == "logger" and alias.asname for alias in node.names
                ):
                    problems.append(f"第 {node.lineno} 行改名导入 logger：禁止 as 别名")
                else:
                    astrbot_logger = True
        elif isinstance(node, ast.Call):
            callee = _dotted(node.func)
            parts = callee.split(".")
            if not allow_logging and (
                parts[-1] in _LOGGING_CALLEES or parts[0] == "logging"
            ):
                problems.append(f"第 {node.lineno} 行调用 {callee}：禁止自建 logger")
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "logger"
            for target in node.targets
        ):
            problems.append(
                f"第 {node.lineno} 行给 logger 赋值：只能 from astrbot.api 导入"
            )
        elif (
            isinstance(node, ast.NamedExpr)
            and isinstance(node.target, ast.Name)
            and node.target.id == "logger"
        ):
            problems.append(f"第 {node.lineno} 行用海象运算符绑定 logger：禁止自建")
        elif isinstance(node, ast.arg) and node.arg == "logger":
            problems.append(
                f"第 {node.lineno} 行把 logger 作为参数传入：禁止注入自建记录器"
            )
        elif isinstance(node, ast.Name) and node.id == "logger":
            if isinstance(node.ctx, ast.Load):
                uses_logger = True

    if uses_logger and not astrbot_logger:
        problems.append("使用 logger 但缺少 from astrbot.api import logger")
    return problems


SOURCE_FILES = _source_files()
SOURCE_IDS = [_rel(path) for path in SOURCE_FILES]


def test_source_files_are_scanned() -> None:
    """扫描范围必须覆盖 main.py 与 tl/ 下全部模块，避免静默漏检。"""

    assert SOURCE_FILES, "未找到待检查的源码文件"
    assert _rel(ROOT / "main.py") in SOURCE_IDS
    scanned = {_rel(path) for path in SOURCE_FILES}
    for expected in ("tl/web_api.py", "tl/image_generator.py", "tl/core/router.py"):
        assert expected in scanned, f"{expected} 不在扫描范围内"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=SOURCE_IDS)
def test_logger_only_from_astrbot(path: Path) -> None:
    problems = _scan(path, allow_logging=_rel(path) in LOGGING_WHITELIST)
    assert not problems, "\n".join(problems)


def test_logging_whitelist_entries_are_justified_and_live() -> None:
    """白名单条目必须存在、有理由，且确实用到标准库 logging，否则删除。"""

    for rel, reason in LOGGING_WHITELIST.items():
        path = ROOT / rel
        assert path.is_file(), f"白名单条目 {rel} 指向的文件不存在"
        assert reason.strip(), f"白名单条目 {rel} 缺少豁免理由"
        assert _scan(path, allow_logging=False), (
            f"白名单条目 {rel} 已不使用标准库 logging，请从白名单删除"
        )
