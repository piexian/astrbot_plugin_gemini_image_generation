"""函数工具权限默认值辅助测试。"""

from __future__ import annotations

from tl.tool_permission import (
    TOOL_PERMISSION_KEY,
    TOOL_PERMISSION_SCOPE,
    TOOL_PERMISSION_SCOPE_ID,
    ensure_admin_default_tool_permission,
)


class _FakeSharedPreferences:
    def __init__(self, initial=None):
        self.store = initial if initial is not None else {}
        self.put_calls = []

    def get(self, key, default=None, scope=None, scope_id=None):
        assert key == TOOL_PERMISSION_KEY
        assert scope == TOOL_PERMISSION_SCOPE
        assert scope_id == TOOL_PERMISSION_SCOPE_ID
        return self.store or default

    def put(self, key, value, scope=None, scope_id=None):
        assert key == TOOL_PERMISSION_KEY
        assert scope == TOOL_PERMISSION_SCOPE
        assert scope_id == TOOL_PERMISSION_SCOPE_ID
        self.store = value
        self.put_calls.append(value)


def test_ensure_admin_default_tool_permission_sets_admin_when_missing():
    sp = _FakeSharedPreferences({})

    changed = ensure_admin_default_tool_permission("gemini_image_generation", sp_obj=sp)

    assert changed is True
    assert sp.store["_default"]["gemini_image_generation"] == "admin"
    assert len(sp.put_calls) == 1


def test_ensure_admin_default_tool_permission_does_not_override_existing_member():
    sp = _FakeSharedPreferences({"_default": {"gemini_image_generation": "member"}})

    changed = ensure_admin_default_tool_permission("gemini_image_generation", sp_obj=sp)

    assert changed is False
    assert sp.store["_default"]["gemini_image_generation"] == "member"
    assert sp.put_calls == []


def test_ensure_admin_default_tool_permission_handles_invalid_store_shape():
    sp = _FakeSharedPreferences([])

    changed = ensure_admin_default_tool_permission("gemini_image_generation", sp_obj=sp)

    assert changed is True
    assert sp.store["_default"]["gemini_image_generation"] == "admin"
