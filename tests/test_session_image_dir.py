from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tl import session_image_dir as sid
from tl.api_types import APIError

_UMO = "platform:GroupMessage:123"


def _context_with_runtime(runtime: str):
    return SimpleNamespace(
        get_config=lambda umo=None: {
            "provider_settings": {"computer_use_runtime": runtime}
        }
    )


@pytest.mark.asyncio
async def test_bind_defaults_to_session_workspace(monkeypatch, tmp_path) -> None:
    def fake_default(umo):
        assert umo == _UMO
        return tmp_path

    async def forbidden(umo):
        raise AssertionError("非 local 运行时不应调用项目感知解析")

    monkeypatch.setattr(sid, "default_workspace_root", fake_default)
    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", forbidden)
    token = await sid.bind_session_image_dir(SimpleNamespace(unified_msg_origin=_UMO))
    try:
        assert sid.current_session_image_dir() == tmp_path / sid.SESSION_IMAGE_SUBDIR
    finally:
        sid.reset_session_image_dir(token)
    assert sid.current_session_image_dir() is None


@pytest.mark.asyncio
async def test_bind_sandbox_runtime_uses_default_root(monkeypatch, tmp_path) -> None:
    async def forbidden(umo):
        raise AssertionError("sandbox 运行时不应调用项目感知解析")

    monkeypatch.setattr(sid, "default_workspace_root", lambda umo: tmp_path)
    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", forbidden)
    token = await sid.bind_session_image_dir(
        SimpleNamespace(unified_msg_origin=_UMO), _context_with_runtime("sandbox")
    )
    try:
        assert sid.current_session_image_dir() == tmp_path / sid.SESSION_IMAGE_SUBDIR
    finally:
        sid.reset_session_image_dir(token)


@pytest.mark.asyncio
async def test_bind_local_runtime_uses_project_aware_root(
    monkeypatch, tmp_path
) -> None:
    async def fake_resolve(umo):
        assert umo == _UMO
        return tmp_path / "project_ws"

    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", fake_resolve)
    monkeypatch.setattr(sid, "default_workspace_root", lambda umo: tmp_path / "legacy")
    token = await sid.bind_session_image_dir(
        SimpleNamespace(unified_msg_origin=_UMO), _context_with_runtime("local")
    )
    try:
        assert (
            sid.current_session_image_dir()
            == tmp_path / "project_ws" / sid.SESSION_IMAGE_SUBDIR
        )
    finally:
        sid.reset_session_image_dir(token)


@pytest.mark.asyncio
async def test_bind_failure_raises_on_use(monkeypatch) -> None:
    def boom(umo):
        raise RuntimeError("db down")

    monkeypatch.setattr(sid, "default_workspace_root", boom)
    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", object())
    token = await sid.bind_session_image_dir(SimpleNamespace(unified_msg_origin=_UMO))
    try:
        with pytest.raises(APIError, match="工作区不可用"):
            sid.current_session_image_dir()
    finally:
        sid.reset_session_image_dir(token)
    assert sid.current_session_image_dir() is None


@pytest.mark.asyncio
async def test_bind_skips_without_event_or_framework_support(monkeypatch) -> None:
    assert await sid.bind_session_image_dir(None) is None
    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", None)
    assert (
        await sid.bind_session_image_dir(SimpleNamespace(unified_msg_origin=_UMO))
        is None
    )
    assert sid.current_session_image_dir() is None


@pytest.mark.asyncio
async def test_binding_propagates_to_background_task(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sid, "default_workspace_root", lambda umo: tmp_path)
    monkeypatch.setattr(sid, "resolve_workspace_root_for_umo", object())
    token = await sid.bind_session_image_dir(SimpleNamespace(unified_msg_origin=_UMO))
    try:

        async def _read():
            return sid.current_session_image_dir()

        assert await asyncio.create_task(_read()) == tmp_path / sid.SESSION_IMAGE_SUBDIR
    finally:
        sid.reset_session_image_dir(token)
    assert sid.current_session_image_dir() is None


def _write_image(path: Path, content: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def test_cleanup_counts_all_session_dirs(tmp_path) -> None:
    old = tmp_path / "s1" / sid.SESSION_IMAGE_SUBDIR / "old.png"
    new = tmp_path / "s2" / sid.SESSION_IMAGE_SUBDIR / "new.png"
    now = time.time()
    _write_image(old, b"o" * 600, now - 3600)
    _write_image(new, b"n" * 600, now)
    token = sid._session_image_dir.set(tmp_path / "s2" / sid.SESSION_IMAGE_SUBDIR)
    try:
        sid.cleanup_session_images_by_size(0.001)  # 上限约 1KB，总量 1200B 超限
    finally:
        sid._session_image_dir.reset(token)
    assert not old.exists()  # 跨会话最旧文件被清理
    assert new.exists()


def test_cleanup_noop_when_unbound_or_under_limit(tmp_path) -> None:
    only = tmp_path / "s1" / sid.SESSION_IMAGE_SUBDIR / "a.png"
    _write_image(only, b"a" * 100, time.time())
    sid.cleanup_session_images_by_size(0.0001)  # 未绑定会话：不清理
    assert only.exists()
    token = sid._session_image_dir.set(tmp_path / "s1" / sid.SESSION_IMAGE_SUBDIR)
    try:
        sid.cleanup_session_images_by_size(512)  # 未超限：不清理
    finally:
        sid._session_image_dir.reset(token)
    assert only.exists()
