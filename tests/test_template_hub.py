"""在线模板源客户端：URL 拼接、错误分类、字段映射与出站检查。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tl.template_hub as hub
from tl.prompt_templates import (
    PromptTemplate,
    SourceAuthError,
    SourceRateLimited,
    SourceRejected,
    SourceUnavailable,
)


class _Response:
    def __init__(self, status=200, payload=None, headers=None, raw=b"") -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self._raw = raw

    async def read(self):
        if self._raw:
            return self._raw
        return json.dumps(self._payload, ensure_ascii=False).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)

    async def close(self):
        self.closed = True


def _source(handler, **overrides):
    session = _Session(handler)
    source = hub.HubTemplateSource(session=session, **overrides)
    return source, session


def _ok(payload, headers=None):
    def handler(method, url, kwargs):
        return _Response(200, payload, headers or {})

    return handler


def _template(**overrides):
    payload = {
        "id": "abcdefghijk1",
        "name": "测试模板",
        "content": "画一只 {{input}}",
        "ai_description": "说明",
        "reference": "none",
        "tags": ["测试"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_join_requires_https_and_builds_query():
    assert (
        hub._join("https://example.test/", "/v1/status")
        == "https://example.test/v1/status"
    )
    assert (
        hub._join("https://example.test/base", "/v1/templates", {"q": "猫", "limit": 5})
        == "https://example.test/base/v1/templates?q=%E7%8C%AB&limit=5"
    )
    with pytest.raises(SourceUnavailable):
        hub._join("http://example.test", "/v1/status")
    with pytest.raises(SourceUnavailable):
        hub._join("file:///etc/passwd", "/v1/status")


def test_join_drops_empty_query_values():
    url = hub._join("https://example.test", "/v1/templates", {"q": None, "limit": 10})
    assert "q" not in url
    assert "limit=10" in url


def test_record_maps_remote_origin_and_fields():
    template = hub._record(_template(), source="image-hub.alxy.app")
    assert isinstance(template, PromptTemplate)
    assert template.origin == "remote"
    assert template.remote is not None
    assert template.remote.source == "image-hub.alxy.app"
    assert template.remote.remote_id == "abcdefghijk1"
    assert template.name == "测试模板"


def test_record_rejects_bad_payload():
    with pytest.raises(SourceUnavailable):
        hub._record("not a dict")
    with pytest.raises(SourceUnavailable):
        hub._record({"name": "缺少 id"})


@pytest.mark.asyncio
async def test_search_maps_page_and_cursor():
    source, session = _source(_ok({"templates": [_template()], "next_cursor": "cur-1"}))
    page = await source.search("猫", tag="摄影", cursor="prev", limit=99)
    assert len(page.templates) == 1
    assert page.next_cursor == "cur-1"
    url = session.calls[0]["url"]
    assert "limit=50" in url
    assert "tag=" in url


@pytest.mark.asyncio
async def test_get_many_enforces_batch_limit():
    source, _ = _source(_ok({"templates": []}))
    with pytest.raises(SourceRejected):
        await source.get_many([str(i) for i in range(51)])
    assert await source.get_many([]) == []


@pytest.mark.asyncio
async def test_status_and_version():
    source, _ = _source(
        _ok(
            {
                "version": "0.1.0",
                "template_count": 30,
                "rate_limits": {"anonymous_read_per_minute_per_ip": 60},
            }
        )
    )
    status = await source.status()
    assert status.template_count == 30
    assert status.rate_limits["anonymous_read_per_minute_per_ip"] == 60

    source, _ = _source(_ok({"hash": "abc", "count": 3, "size": 12, "updated_at": "t"}))
    version = await source.catalog_version()
    assert version.hash == "abc"


@pytest.mark.asyncio
async def test_download_catalog_returns_etag_or_none():
    source, _ = _source(_ok({"templates": [_template()]}, headers={"ETag": 'W/"abc"'}))
    assert await source.download_catalog() == ('W/"abc"', {"templates": [_template()]})

    def not_modified(method, url, kwargs):
        return _Response(304, None, {})

    source, session = _source(not_modified)
    assert await source.download_catalog('W/"abc"') is None
    assert session.calls[0]["headers"]["If-None-Match"] == 'W/"abc"'


@pytest.mark.parametrize(
    ("status", "expected", "retry_after"),
    [
        (429, SourceRateLimited, "12"),
        (401, SourceAuthError, None),
        (403, SourceAuthError, None),
        (503, SourceUnavailable, None),
    ],
)
@pytest.mark.asyncio
async def test_error_classification(status, expected, retry_after):
    def handler(method, url, kwargs):
        return _Response(
            status,
            {"message": "nope"},
            {"Retry-After": retry_after} if retry_after else {},
        )

    source, _ = _source(handler)
    with pytest.raises(expected):
        await source.status()


@pytest.mark.asyncio
async def test_rate_limited_reads_retry_after():
    def handler(method, url, kwargs):
        return _Response(429, {}, {"Retry-After": "7"})

    source, _ = _source(handler)
    with pytest.raises(SourceRateLimited) as error:
        await source.status()
    assert error.value.retry_after == 7.0


@pytest.mark.asyncio
async def test_4xx_becomes_rejected_with_server_message():
    source, _ = _source(lambda m, u, k: _Response(400, {"message": "名称太长"}))
    with pytest.raises(SourceRejected) as error:
        await source.upload(hub._record(_template()))
    assert "名称太长" in str(error.value)


@pytest.mark.asyncio
async def test_auth_header_only_when_requested():
    source, session = _source(_ok({"templates": [_template()]}))
    await source.get_many(["abcdefghijk1"])
    assert "Authorization" not in session.calls[0]["headers"]

    keyed = hub.HubTemplateSource(api_key="iph_x", session=_Session(_ok({})))
    await keyed.key_status()
    assert keyed._headers(auth=True)["Authorization"] == "Bearer iph_x"


@pytest.mark.asyncio
async def test_key_endpoints_require_identifier():
    source, _ = _source(_ok({"templates": [_template()]}))
    with pytest.raises(SourceRejected):
        await source.delete("")
    with pytest.raises(SourceRejected):
        await source.update("", hub._record(_template()))
    with pytest.raises(SourceRejected):
        await source.request_key(" ")
    with pytest.raises(SourceRejected):
        await source.report("", "spam")


@pytest.mark.asyncio
async def test_upload_body_omits_local_only_fields():
    source, session = _source(_ok(_template()))
    template = hub._record(_template(preview="x.png"))
    await source.upload(template)
    body = session.calls[0]["json"]
    assert "preview" not in body
    assert "ai_enabled" not in body
    assert "origin" not in body
    assert body["name"] == "测试模板"


@pytest.mark.asyncio
async def test_outbound_check_failure_becomes_unavailable():
    async def reject(url, *, pin=None):
        raise RuntimeError("blocked")

    source, _ = _source(_ok({}), url_checker=reject)
    with pytest.raises(SourceUnavailable):
        await source.status()


@pytest.mark.asyncio
async def test_connection_error_becomes_unavailable():
    import aiohttp

    def handler(method, url, kwargs):
        class _Req:
            async def __aenter__(self):
                raise aiohttp.ClientConnectorError(
                    SimpleNamespace(host="x"), OSError("down")
                )

            async def __aexit__(self, *exc):
                return False

        return _Req()

    source, _ = _source(handler)
    with pytest.raises(SourceUnavailable):
        await source.status()


@pytest.mark.asyncio
async def test_oversized_response_is_rejected():
    def handler(method, url, kwargs):
        return _Response(200, None, {}, raw=b"x" * (hub._MAX_BYTES + 1))

    source, _ = _source(handler)
    with pytest.raises(SourceUnavailable):
        await source.status()


@pytest.mark.asyncio
async def test_close_releases_owned_session():
    session = _Session(_ok({}))
    source = hub.HubTemplateSource(session=session)
    source._owns_session = True
    await source.close()
    assert session.closed
    assert source._session is None


def test_source_for_defaults_to_official_address():
    source = hub.source_for("", None)
    assert source.base_url == hub.DEFAULT_BASE_URL
    assert source.api_key is None
    assert source.source_name == "image-hub.alxy.app"
