"""生成图保留区容量清理的单元测试。"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest


def _load_tl_utils(monkeypatch: pytest.MonkeyPatch):
    """加载真实的 tl_utils 模块（绕过 conftest 的 stub）"""
    root = Path(__file__).resolve().parents[1]
    module_name = "tl._image_cache_cleanup_tl_utils"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "tl" / "tl_utils.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _make_files(directory: Path, sizes: list[int]) -> list[Path]:
    """按顺序创建文件，mtime 递增（最旧的在前）"""
    paths = []
    base = time.time() - 3600
    for idx, size in enumerate(sizes):
        path = directory / f"gemini_image_{idx}.png"
        path.write_bytes(b"x" * size)
        mtime = base + idx
        os.utime(path, (mtime, mtime))
        paths.append(path)
    return paths


def test_below_limit_keeps_everything(tmp_path, monkeypatch) -> None:
    module = _load_tl_utils(monkeypatch)
    files = _make_files(tmp_path, [100, 100])
    module.cleanup_image_cache_by_size(tmp_path, max_size_mb=1)
    assert all(p.exists() for p in files)


def test_over_limit_deletes_oldest_first(tmp_path, monkeypatch) -> None:
    module = _load_tl_utils(monkeypatch)
    files = _make_files(tmp_path, [400, 400, 400, 400, 400])
    # 上限 1KB（0.001MB）：总量 2000 > 1024，释放约 30%（600 字节）→ 删最旧两个
    module.cleanup_image_cache_by_size(tmp_path, max_size_mb=0.001)
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
    assert files[3].exists()
    assert files[4].exists()


def test_zero_limit_disables_cleanup(tmp_path, monkeypatch) -> None:
    module = _load_tl_utils(monkeypatch)
    files = _make_files(tmp_path, [10_000])
    module.cleanup_image_cache_by_size(tmp_path, max_size_mb=0)
    assert all(p.exists() for p in files)


def test_missing_directory_is_noop(tmp_path, monkeypatch) -> None:
    module = _load_tl_utils(monkeypatch)
    module.cleanup_image_cache_by_size(tmp_path / "nonexistent", max_size_mb=1)
