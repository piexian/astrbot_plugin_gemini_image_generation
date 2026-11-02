"""在线模板本地缓存：同步、持久化、检索与 hub: 引用解析。"""

from __future__ import annotations

import json

import pytest

from tl.prompt_templates import (
    CatalogVersion,
    SourceUnavailable,
)
from tl.template_cache import (
    CacheState,
    TemplateHubCache,
    _hash_from,
    initial_delay,
    interval_seconds,
)


class _Source:
    source_name = "image-hub.alxy.app"

    def __init__(
        self, *, hash_value="h1", fail_version=False, fail_download=False
    ) -> None:
        self.hash_value = hash_value
        self.fail_version = fail_version
        self.fail_download = fail_download
        self.calls: list = []

    async def catalog_version(self) -> CatalogVersion:
        self.calls.append("version")
        if self.fail_version:
            raise SourceUnavailable("down")
        return CatalogVersion(hash=self.hash_value, count=1, updated_at="t", size=1)

    async def download_catalog(self, etag=None):
        self.calls.append(("download", etag))
        if self.fail_download:
            raise SourceUnavailable("down")
        return f'W/"{self.hash_value}"', _catalog()


def _catalog(*names):
    items = []
    for index, name in enumerate(names or ("猫", "狗")):
        items.append(
            {
                "id": f"id{index}aaaaaaa",
                "name": name,
                "content": f"画一只 {name} 的 {{input}}",
                "ai_description": f"{name}模板",
                "reference": "none",
                "tags": ["动物"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
    return {"updated_at": "t", "templates": items}


@pytest.mark.asyncio
async def test_first_sync_downloads_without_version_probe(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source)
    state = await cache.sync()
    assert state.hash == "h1"
    assert state.count == 2
    assert source.calls == [("download", None)]


@pytest.mark.asyncio
async def test_sync_skips_download_when_hash_unchanged(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source)
    await cache.sync()
    before = len(cache.all())
    await cache.sync()
    assert len(cache.all()) == before
    assert source.calls[-1] == "version"


@pytest.mark.asyncio
async def test_force_sync_downloads_with_known_etag(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source)
    await cache.sync()
    await cache.sync(force=True)
    assert source.calls[-1] == ("download", "h1")


@pytest.mark.asyncio
async def test_sync_keeps_old_cache_when_source_fails(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source)
    await cache.sync()
    source.hash_value = "h2"
    source.fail_download = True
    state = await cache.sync(force=True)
    assert state.hash == "h1"
    assert len(cache.all()) == 2

    source.fail_download = False
    source.fail_version = True
    assert (await cache.sync()).hash == "h1"


@pytest.mark.asyncio
async def test_cache_survives_reload(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source)
    await cache.sync()
    reloaded = TemplateHubCache(tmp_path, source=_Source())
    assert len(reloaded.all()) == 2
    assert reloaded.state.hash == "h1"
    assert reloaded.get("id0aaaaaaa").name == "猫"


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "template_hub_cache.json").write_text("{not json", encoding="utf-8")
    cache = TemplateHubCache(tmp_path, source=_Source())
    assert cache.all() == []


def test_unknown_cache_version_is_ignored(tmp_path):
    (tmp_path / "template_hub_cache.json").write_text(
        json.dumps({"cache_version": 999, "templates": []}), encoding="utf-8"
    )
    cache = TemplateHubCache(tmp_path, source=_Source())
    assert cache.all() == []


@pytest.mark.asyncio
async def test_clear_resets_state(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    await cache.sync()
    cache.clear()
    assert cache.all() == []
    assert cache.state.hash == ""


def test_disabled_modes(tmp_path):
    off = TemplateHubCache(tmp_path, source=_Source(), mode="off")
    assert not off.enabled
    assert not off.live_search

    online = TemplateHubCache(tmp_path, source=_Source(), mode="online")
    assert online.enabled and online.live_search

    sourceless = TemplateHubCache(tmp_path, source=None)
    assert not sourceless.enabled


@pytest.mark.asyncio
async def test_disabled_sync_is_noop(tmp_path):
    source = _Source()
    cache = TemplateHubCache(tmp_path, source=source, mode="off")
    await cache.sync(force=True)
    assert source.calls == []
    assert cache.all() == []


def test_reconfigure_switches_mode_and_source(tmp_path):
    cache = TemplateHubCache(tmp_path, source=None, mode="off")
    assert not cache.enabled
    cache.reconfigure(source=_Source(), mode="online")
    assert cache.enabled and cache.live_search
    cache.reconfigure(mode="bogus")
    assert cache.mode == "online"


@pytest.mark.asyncio
async def test_search_weights_name_over_content(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    await cache.sync()
    by_name = cache.search("猫")
    assert [item[1].name for item in by_name] == ["猫"]
    assert by_name[0][0] > 0

    both = cache.search("画一只")
    assert len(both) == 2


@pytest.mark.asyncio
async def test_search_filters_by_tag_and_respects_limit(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    await cache.sync()
    assert len(cache.search("", tag="动物")) == 2
    assert cache.search("", tag="植物") == []
    assert len(cache.search("", limit=1)) == 1


@pytest.mark.asyncio
async def test_search_without_query_returns_all(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    await cache.sync()
    assert len(cache.search("")) == 2


@pytest.mark.asyncio
async def test_resolve_hub_reference(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    await cache.sync()
    assert cache.resolve("hub:id0aaaaaaa").name == "猫"
    assert cache.resolve("hub:missing") is None
    assert cache.resolve("poster") is None
    assert cache.resolve("") is None


def test_hash_from_strips_weak_prefix_and_quotes():
    assert _hash_from('W/"abc"') == "abc"
    assert _hash_from('"abc"') == "abc"
    assert _hash_from("") == ""


def test_bad_records_are_skipped(tmp_path):
    payload = {
        "cache_version": 1,
        "state": {"hash": "h", "count": 1, "source": "hub"},
        "templates": [
            {
                "remote_id": "ok-id",
                "template": {
                    "id": "ok-id",
                    "name": "好",
                    "content": "画 {{input}}",
                    "reference": "none",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            },
            {"remote_id": "", "template": {}},
            "not a dict",
        ],
    }
    (tmp_path / "template_hub_cache.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    cache = TemplateHubCache(tmp_path, source=_Source())
    assert len(cache.all()) == 1


def test_delay_and_interval_helpers():
    assert 0.0 <= initial_delay() <= 120.0
    assert interval_seconds(None) == 6 * 3600
    assert interval_seconds("bad") == 6 * 3600
    assert interval_seconds(0) == 6 * 3600
    assert interval_seconds(60) == 60.0


def test_state_as_dict_shape():
    state = CacheState(hash="h", count=2, updated_at="t", synced_at=1.0, source="s")
    assert set(state.as_dict()) == {
        "hash",
        "count",
        "updated_at",
        "synced_at",
        "source",
    }


def test_cache_file_is_written_atomically(tmp_path):
    cache = TemplateHubCache(tmp_path, source=_Source())
    cache._write()
    assert (tmp_path / "template_hub_cache.json").is_file()
    assert not (tmp_path / "template_hub_cache.json.tmp").exists()
