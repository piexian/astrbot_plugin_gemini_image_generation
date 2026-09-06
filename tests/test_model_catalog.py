from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tl import model_catalog as catalog
from tl.model_catalog import (
    ModelCatalogError,
    ModelCatalogService,
    catalog_capabilities,
    make_catalog_request,
    safe_target,
)
from tl.provider_metadata import iter_provider_specs

FAKE_KEY = "fake-catalog-key-never-real"
FAKE_QUERY_SECRET = "fake-query-secret"


class FakeResponse:
    def __init__(self, payload=None, *, status=200, body=None, block=None):
        self.status = status
        self.body = json.dumps(payload).encode() if body is None else body
        self.content_length = None
        self.content = self
        self.block = block
        self.started = asyncio.Event()
        self.exited = False

    async def __aenter__(self):
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class FakeHTTP:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.sessions = []

    def session(self, **kwargs):
        session = FakeSession(self, kwargs)
        self.sessions.append(session)
        return session


class FakeSession:
    def __init__(self, http, kwargs):
        self.http = http
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.http.calls.append((url, kwargs))
        if not self.http.responses:
            raise AssertionError("Unexpected fake HTTP request")
        response = self.http.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def http(monkeypatch):
    """No test can accidentally use a real supplier connection."""
    http = FakeHTTP()
    monkeypatch.setattr(catalog.aiohttp, "ClientSession", http.session)
    return http


def request(protocol="openai", base="https://catalog.invalid/gateway"):
    return make_catalog_request(protocol, base, FAKE_KEY)


@pytest.mark.parametrize(
    ("api_type", "protocol", "path"),
    [
        ("google", "google", "/v1beta/models"),
        ("gemini_interactions", "google", "/v1beta/models"),
        ("openai", "openai", "/v1/models"),
        ("openai_images", "openai", "/v1/models"),
        ("agnes_ai", "openai", "/v1/models"),
        ("minimax", "openai", "/v1/models"),
        ("stepfun", "openai", "/v1/models"),
        ("modelscope", "openai", "/v1/models"),
        ("xai", "xai", "/v1/image-generation-models"),
        ("siliconflow", "siliconflow", "/v1/models"),
    ],
)
def test_supported_protocols_keep_prefix(api_type, protocol, path):
    req = request(api_type)
    assert req.protocol == protocol
    assert req.url == f"https://catalog.invalid/gateway{path}"
    assert req.proxy is None
    if protocol == "google":
        assert req.headers == {"x-goog-api-key": FAKE_KEY}
        assert req.params == {"pageSize": "1000"}
    else:
        assert req.headers == {"Authorization": f"Bearer {FAKE_KEY}"}
        assert req.params == ({"type": "image"} if protocol == "siliconflow" else {})


def test_all_existing_provider_metadata_remains():
    specs = iter_provider_specs()
    assert [spec.api_type for spec in specs] == [
        "google",
        "gemini_interactions",
        "openai",
        "agnes_ai",
        "xai",
        "minimax",
        "stepfun",
        "openai_images",
        "doubao",
        "sensenova",
        "dashscope",
        "modelscope",
        "siliconflow",
    ]
    capabilities = catalog_capabilities()
    assert len(capabilities) == 13
    assert sum(item["supported"] for item in capabilities.values()) == 10
    for api_type in ("agnes_ai", "minimax", "stepfun", "modelscope"):
        assert capabilities[api_type]["supported"] is True
        assert "不消耗生成额度" in capabilities[api_type]["message"]
    assert (
        next(spec for spec in specs if spec.api_type == "modelscope").max_concurrency
        == 1
    )
    for api_type in ("doubao", "sensenova", "dashscope"):
        assert capabilities[api_type]["supported"] is False
        assert "暂未接入" in capabilities[api_type]["message"]
        with pytest.raises(ModelCatalogError, match="暂未接入") as error:
            request(api_type)
        assert error.value.status_code == 400


@pytest.mark.parametrize(
    "api_type",
    [
        "google",
        "gemini_interactions",
        "openai",
        "openai_images",
        "agnes_ai",
        "minimax",
        "stepfun",
        "modelscope",
        "xai",
        "siliconflow",
    ],
)
@pytest.mark.parametrize("version", ["v1", "v1beta"])
def test_existing_versions_are_not_doubled(api_type, version):
    req = request(api_type, f"http://localhost:8123/prefix/{version}/")
    endpoint = "image-generation-models" if api_type == "xai" else "models"
    assert req.url == f"http://localhost:8123/prefix/{version}/{endpoint}"


@pytest.mark.parametrize(
    ("api_type", "suffix", "expected"),
    [
        ("google", "v1beta/models/fake-model:generateContent", "v1beta/models"),
        ("google", "v1/models/fake-model:streamGenerateContent", "v1/models"),
        ("gemini_interactions", "v1beta/interactions", "v1beta/models"),
        ("openai", "v1/chat/completions", "v1/models"),
        ("openai_images", "v1/images/generations", "v1/models"),
        ("openai_images", "v1/images/edits", "v1/models"),
        ("agnes_ai", "v1/images/generations", "v1/models"),
        ("minimax", "v1/image_generation", "v1/models"),
        ("stepfun", "v1/images/generations", "v1/models"),
        ("stepfun", "v1/images/edits", "v1/models"),
        ("modelscope", "v1/images/generations", "v1/models"),
        ("xai", "v1/images/generations", "v1/image-generation-models"),
        ("siliconflow", "v1/images/generations", "v1/models"),
        ("openai", "v1/models", "v1/models"),
        ("xai", "v1/image-generation-models", "v1/image-generation-models"),
    ],
)
def test_full_generation_paths_keep_prefix_and_explicit_query(
    api_type, suffix, expected
):
    req = request(
        api_type,
        f"https://catalog.invalid/custom/{suffix}?token={FAKE_QUERY_SECRET}&route=a&route=b",
    )
    assert (
        req.url
        == f"https://catalog.invalid/custom/{expected}?token={FAKE_QUERY_SECRET}&route=a&route=b"
    )
    assert FAKE_KEY not in repr(req)
    assert FAKE_QUERY_SECRET not in repr(req)
    assert "catalog.invalid" not in repr(req)
    assert safe_target(req) == "https://catalog.invalid"


@pytest.mark.parametrize(
    ("api_type", "base", "generation_suffix"),
    [
        ("agnes_ai", "https://apihub.agnes-ai.com", "/images/generations"),
        ("minimax", "https://api.minimaxi.com", "/image_generation"),
        ("stepfun", "https://api.stepfun.com", "/images/generations"),
        ("modelscope", "https://api-inference.modelscope.cn", "/images/generations"),
    ],
)
@pytest.mark.parametrize(
    "path", ["", "/", "/v1", "/v1/", "/v1/models", "/v1{generation_suffix}/"]
)
def test_extended_catalog_default_and_complete_bases(
    api_type, base, generation_suffix, path
):
    req = request(api_type, base + path.format(generation_suffix=generation_suffix))
    assert req.url == f"{base}/v1/models"
    assert req.protocol == "openai"
    assert req.headers == {"Authorization": f"Bearer {FAKE_KEY}"}
    assert req.params == {}


@pytest.mark.parametrize(
    "suffix",
    ["", "/v1", "/v1/", "/v1/images/generations", "/v1/images/edits", "/v1/models"],
)
def test_step_plan_keeps_custom_prefix(suffix):
    req = request("stepfun", f"https://api.stepfun.com/step_plan{suffix}?route=keep")
    assert req.url == "https://api.stepfun.com/step_plan/v1/models?route=keep"


@pytest.mark.parametrize("host", ["api.siliconflow.cn", "api.siliconflow.com"])
def test_siliconflow_does_not_rewrite_host(host):
    req = request("siliconflow", f"https://{host}/v1?type=image")
    assert req.url == f"https://{host}/v1/models?type=image"
    assert not req.params


@pytest.mark.parametrize(
    "base",
    [
        "",
        "file:///tmp/private",
        "ftp://catalog.invalid",
        "//catalog.invalid",
        "http://",
        "https://catalog.invalid:bad",
        "https://fake-user:fake-password@catalog.invalid/v1",
        "https://catalog.invalid/v1#fake-fragment",
        "https://catalog.invalid/\nfoo",
    ],
)
def test_bad_or_unsupported_base_fails_explicitly_without_echo(base):
    with pytest.raises(ModelCatalogError) as error:
        request(base=base)
    assert error.value.status_code == 400
    assert "fake-password" not in str(error.value)
    assert "fake-fragment" not in str(error.value)


@pytest.mark.parametrize("key", ["", " ", "fake-key\r\nX-Header: bad", None])
def test_invalid_key_is_not_echoed(key):
    with pytest.raises(ModelCatalogError) as error:
        make_catalog_request("google", "https://catalog.invalid", key)
    assert error.value.status_code == 400
    assert "X-Header" not in str(error.value)


@pytest.mark.parametrize(
    ("api_type", "query"),
    [
        ("google", "pageSize=1001"),
        ("google", "pageSize=bad"),
        ("siliconflow", "type=text"),
        ("siliconflow", "type=text&type=image"),
        ("google", "pageSize=1001&pageSize=1"),
        ("google", "pageToken=first&pageToken=second"),
    ],
)
def test_explicit_conflicting_query_not_silently_replaced(api_type, query):
    with pytest.raises(ModelCatalogError) as error:
        request(api_type, f"https://catalog.invalid/v1?{query}")
    assert error.value.status_code == 400


def test_safe_target_ipv6_and_proxy_repr():
    req = make_catalog_request(
        "openai",
        "http://[::1]:9123/custom?key=fake-query",
        FAKE_KEY,
        "http://user:fake-proxy-password@127.0.0.1:9000",
    )
    assert safe_target(req) == "http://[::1]:9123"
    assert "fake-proxy-password" not in repr(req)


def test_safe_target_does_not_echo_key_in_host():
    req = request(base=f"https://{FAKE_KEY}.catalog.invalid/v1")
    assert safe_target(req) == "自定义目标"


@pytest.mark.asyncio
async def test_openai_deduplicates_limits_fields_and_warns(http):
    http.responses = [
        FakeResponse(
            {
                "data": [
                    {"id": "fake-model", "owned_by": FAKE_KEY},
                    {"id": "fake-model"},
                    {"id": "fake-other"},
                ]
            }
        )
    ]
    service = ModelCatalogService()
    result = await service.fetch(request())
    assert result["models"] == [
        {"id": "fake-model", "label": "fake-model"},
        {"id": "fake-other", "label": "fake-other"},
    ]
    assert "非生图" in result["warning"]
    assert not result["truncated"]
    assert FAKE_KEY not in json.dumps(result)
    assert len(http.sessions) == 1 and http.sessions[0].closed
    assert http.sessions[0].kwargs["trust_env"] is False
    assert http.sessions[0].kwargs["timeout"].total == 20
    assert http.calls[0][1]["allow_redirects"] is False
    assert "ssl" not in http.calls[0][1]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_type", "image_id"),
    [
        ("agnes_ai", "agnes-image-2.5-flash"),
        ("minimax", "image-01"),
        ("stepfun", "step-image-edit-2"),
        ("modelscope", "Qwen/Qwen-Image-Edit"),
    ],
)
async def test_extended_catalog_keeps_source_image_and_text_models(
    http, api_type, image_id
):
    # Synthetic catalogs test parsing, not upstream availability or account access.
    ids = ["fake-text-model", image_id, "fake-unclassified-model"]
    http.responses = [
        FakeResponse({"object": "list", "data": [{"id": model_id} for model_id in ids]})
    ]
    service = ModelCatalogService()
    result = await service.fetch(request(api_type))
    assert result == {
        "models": [{"id": model_id, "label": model_id} for model_id in ids],
        "warning": catalog._UNFILTERED_WARNING,
        "truncated": False,
    }
    assert len(http.calls) == 1
    assert http.calls[0][1]["headers"] == {"Authorization": f"Bearer {FAKE_KEY}"}
    assert http.calls[0][1]["params"] == {}
    assert http.sessions[0].closed
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_type", ["agnes_ai", "minimax", "stepfun", "modelscope"])
async def test_extended_empty_catalog_has_no_static_fallback(http, api_type):
    http.responses = [FakeResponse({"object": "list", "data": []})]
    service = ModelCatalogService()
    result = await service.fetch(request(api_type))
    assert result == {
        "models": [],
        "warning": catalog._UNFILTERED_WARNING,
        "truncated": False,
    }
    assert len(http.calls) == 1
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_type", ["agnes_ai", "minimax", "stepfun", "modelscope"])
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "upstream_unauthorized"),
        (403, "upstream_forbidden"),
        (404, "upstream_not_found"),
        (429, "upstream_rate_limited"),
        (500, "upstream_error"),
        (302, "redirect_blocked"),
    ],
)
async def test_extended_http_errors_do_not_echo_or_retry_another_prefix(
    http, api_type, status, reason
):
    base = f"https://catalog.invalid/step_plan/v1?token={FAKE_QUERY_SECRET}"
    http.responses = [
        FakeResponse(status=status, body=f"{FAKE_KEY} {FAKE_QUERY_SECRET}".encode())
    ]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request(api_type, base))
    assert error.value.status_code == 502
    assert error.value.reason == reason
    assert FAKE_KEY not in str(error.value)
    assert FAKE_QUERY_SECRET not in str(error.value)
    assert [url for url, _ in http.calls] == [
        f"https://catalog.invalid/step_plan/v1/models?token={FAKE_QUERY_SECRET}"
    ]
    assert http.calls[0][1]["allow_redirects"] is False
    assert http.sessions[0].closed
    await service.close()


@pytest.mark.asyncio
async def test_google_filters_methods_and_only_follows_page_token(http):
    http.responses = [
        FakeResponse(
            {
                "models": [
                    {
                        "name": "models/fake-image",
                        "displayName": "Fake Image",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/fake-embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                    {
                        "name": "models/fake-no-methods",
                        "supportedGenerationMethods": [],
                    },
                ],
                "nextPageToken": "next/token+fake",
                "next": "https://malicious.invalid",
            }
        ),
        FakeResponse(
            {"models": [{"name": "models/fake-legacy"}, {"name": "models/fake-image"}]}
        ),
    ]
    service = ModelCatalogService()
    result = await service.fetch(
        request(
            "google",
            "https://catalog.invalid/proxy/v1beta?pageSize=12&pageToken=start&route=keep",
        )
    )
    assert result["models"] == [
        {"id": "fake-image", "label": "Fake Image"},
        {"id": "fake-legacy", "label": "fake-legacy"},
    ]
    assert len(http.calls) == 2
    second_url, kwargs = http.calls[1]
    assert urlsplit(second_url).hostname == "catalog.invalid"
    assert parse_qs(urlsplit(second_url).query) == {
        "pageSize": ["12"],
        "route": ["keep"],
    }
    assert kwargs["params"]["pageToken"] == "next/token+fake"
    assert FAKE_KEY not in second_url
    await service.close()


@pytest.mark.asyncio
async def test_google_five_page_limit(http):
    http.responses = [
        FakeResponse(
            {"models": [{"name": f"models/fake-{i}"}], "nextPageToken": str(i)}
        )
        for i in range(5)
    ]
    service = ModelCatalogService()
    result = await service.fetch(request("google"))
    assert len(result["models"]) == 5
    assert result["truncated"] is True
    assert len(http.calls) == 5
    await service.close()


@pytest.mark.asyncio
async def test_google_repeated_page_token_fails_safely(http):
    http.responses = [
        FakeResponse({"models": [], "nextPageToken": FAKE_QUERY_SECRET})
        for _ in range(2)
    ]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request("google"))
    assert error.value.reason == "repeated_page_token"
    assert FAKE_QUERY_SECRET not in str(error.value)
    assert len(http.calls) == 2
    await service.close()


@pytest.mark.asyncio
async def test_xai_documented_fallback_same_target_only_on_404(http):
    http.responses = [
        FakeResponse(status=404, body=FAKE_KEY.encode()),
        FakeResponse({"data": [{"id": "fake-chat"}]}),
    ]
    service = ModelCatalogService()
    result = await service.fetch(
        request("xai", "https://catalog.invalid/prefix/v1?token=fake-query")
    )
    assert result["models"] == [{"id": "fake-chat", "label": "fake-chat"}]
    assert "回退" in result["warning"] and "非生图" in result["warning"]
    assert [url for url, _ in http.calls] == [
        "https://catalog.invalid/prefix/v1/image-generation-models?token=fake-query",
        "https://catalog.invalid/prefix/v1/models?token=fake-query",
    ]
    await service.close()


@pytest.mark.asyncio
async def test_xai_fallback_not_repeated(http):
    http.responses = [FakeResponse(status=404), FakeResponse(status=404)]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request("xai"))
    assert error.value.reason == "upstream_not_found"
    assert len(http.calls) == 2
    await service.close()


@pytest.mark.asyncio
async def test_xai_official_models_field(http):
    http.responses = [
        FakeResponse({"models": [{"id": "fake-image", "name": FAKE_KEY}]})
    ]
    service = ModelCatalogService()
    result = await service.fetch(request("xai"))
    assert result == {
        "models": [{"id": "fake-image", "label": "fake-image"}],
        "warning": "",
        "truncated": False,
    }
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "upstream_unauthorized"),
        (403, "upstream_forbidden"),
        (429, "upstream_rate_limited"),
        (500, "upstream_error"),
        (302, "redirect_blocked"),
    ],
)
async def test_status_errors_safe_and_never_dashboard_401_or_xai_fallback(
    http, status, reason
):
    http.responses = [
        FakeResponse(status=status, body=f"{FAKE_KEY} {FAKE_QUERY_SECRET}".encode())
    ]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request("xai"))
    assert error.value.status_code == 502
    assert error.value.reason == reason
    assert FAKE_KEY not in str(error.value)
    assert FAKE_QUERY_SECRET not in repr(error.value)
    assert len(http.calls) == 1
    assert http.sessions[0].closed
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": {}},
        {"data": [None]},
        {"data": [{}]},
        {"data": [{"id": 12}]},
        {"data": [{"id": ""}]},
        {"data": [{"id": "fake\nmodel"}]},
        {"data": [{"id": "a" * 513}]},
        {"data": [{"id": "\ud800"}]},
    ],
)
async def test_invalid_structure_never_masquerades_as_empty(http, payload):
    http.responses = [FakeResponse(payload)]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.reason == "invalid_response"
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "models": [
                {"name": "models/fake", "supportedGenerationMethods": "generateContent"}
            ]
        },
        {"models": [{"name": "models/fake", "supportedGenerationMethods": None}]},
        {"models": [{"name": 42}]},
        {"models": [], "nextPageToken": 12},
    ],
)
async def test_invalid_google_structure(http, payload):
    http.responses = [FakeResponse(payload)]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request("google"))
    assert error.value.reason == "invalid_response"
    await service.close()


@pytest.mark.asyncio
async def test_empty_is_success_not_error(http):
    http.responses = [FakeResponse({"data": []})]
    service = ModelCatalogService()
    result = await service.fetch(request("siliconflow"))
    assert result == {"models": [], "warning": "", "truncated": False}
    assert http.calls[0][1]["params"] == {"type": "image"}
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json-fake-key", b"\xff", b"[" * 2000])
async def test_invalid_json_is_safe(http, body):
    http.responses = [FakeResponse(body=body)]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.reason == "invalid_response"
    assert "fake-key" not in str(error.value)
    await service.close()


@pytest.mark.asyncio
async def test_filters_known_credentials_and_obvious_echoes(http):
    http.responses = [
        FakeResponse(
            {
                "data": [
                    {"id": value}
                    for value in [
                        "fake-valid-model",
                        FAKE_KEY,
                        f"echo-{FAKE_KEY}",
                        FAKE_QUERY_SECRET,
                        "Bearer fake-other-key",
                        "token=fake-private",
                        '{"api_key":"fake-private"}',
                        "sk-fakeotherkey0123456789",
                        "api_key=fake-other",
                        "https://fake:fake-password@catalog.invalid",
                    ]
                ]
            }
        )
    ]
    service = ModelCatalogService()
    result = await service.fetch(
        request(base=f"https://catalog.invalid/v1?token={FAKE_QUERY_SECRET}")
    )
    assert result["models"] == [{"id": "fake-valid-model", "label": "fake-valid-model"}]
    assert "过滤" in result["warning"]
    assert FAKE_KEY not in json.dumps(result)
    await service.close()


@pytest.mark.asyncio
async def test_google_label_credential_echo_filtered(http):
    http.responses = [
        FakeResponse(
            {"models": [{"name": "models/fake-valid", "displayName": FAKE_KEY}]}
        )
    ]
    service = ModelCatalogService()
    result = await service.fetch(request("google"))
    assert result["models"] == []
    assert "过滤" in result["warning"]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_type", ["openai", "google"])
async def test_maximum_models_is_2000(http, api_type):
    payload = (
        {"models": [{"name": f"models/fake-{i}"} for i in range(2001)]}
        if api_type == "google"
        else {"data": [{"id": f"fake-{i}"} for i in range(2001)]}
    )
    http.responses = [FakeResponse(payload)]
    service = ModelCatalogService()
    result = await service.fetch(request(api_type))
    assert len(result["models"]) == 2000 and result["truncated"]
    await service.close()


@pytest.mark.asyncio
async def test_cumulative_google_response_limit(http):
    http.responses = [
        FakeResponse(
            {"models": [], "padding": "x" * (1024 * 1024), "nextPageToken": str(i)}
        )
        for i in range(2)
    ]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request("google"))
    assert error.value.reason == "response_too_large"
    assert len(http.calls) == 2
    assert http.sessions[0].closed
    await service.close()


@pytest.mark.asyncio
async def test_single_response_length_limit(http):
    response = FakeResponse({"data": []})
    response.content_length = 2 * 1024 * 1024 + 1
    http.responses = [response]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.reason == "response_too_large"
    await service.close()


@pytest.mark.asyncio
async def test_http_proxy_auth_not_echoed(http):
    req = make_catalog_request(
        "openai",
        "https://catalog.invalid",
        FAKE_KEY,
        "http://fake-user:fake-proxy-password@localhost:9000",
    )
    http.responses = [RuntimeError(f"{req.url} {req.headers} {req.proxy}")]
    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(req)
    assert error.value.reason == "connection_error"
    assert "fake-proxy-password" not in repr(error.value)
    assert FAKE_KEY not in str(error.value)
    assert http.calls[0][1]["proxy"] == req.proxy
    assert http.sessions[0].closed
    await service.close()


@pytest.mark.asyncio
async def test_socks_missing_is_explicit_failure_without_direct_request(
    http, monkeypatch
):
    monkeypatch.setitem(sys.modules, "aiohttp_socks", None)
    service = ModelCatalogService()
    req = make_catalog_request(
        "google", "https://catalog.invalid", FAKE_KEY, "socks5://127.0.0.1:9000"
    )
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(req)
    assert error.value.reason == "proxy_unavailable"
    assert "未尝试直连" in str(error.value)
    assert not http.calls and not http.sessions
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheme", "normalized"),
    [
        ("socks4", "socks4"),
        ("socks4a", "socks4"),
        ("socks5", "socks5"),
        ("socks5h", "socks5"),
    ],
)
async def test_socks_connector_owned_by_session(http, monkeypatch, scheme, normalized):
    calls = []
    connector = object()

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return connector

    monkeypatch.setitem(
        sys.modules,
        "aiohttp_socks",
        SimpleNamespace(ProxyConnector=SimpleNamespace(from_url=from_url)),
    )
    http.responses = [FakeResponse({"data": []})]
    service = ModelCatalogService()
    req = make_catalog_request(
        "openai",
        "https://catalog.invalid",
        FAKE_KEY,
        f"{scheme}://fake:fake-password@localhost:9000",
    )
    await service.fetch(req)
    assert calls == [
        (f"{normalized}://fake:fake-password@localhost:9000", {"rdns": True})
    ]
    assert http.sessions[0].kwargs["connector"] is connector
    assert http.sessions[0].closed
    assert http.calls[0][1]["proxy"] is None
    await service.close()


@pytest.mark.asyncio
async def test_concurrency_two_and_close_cancels_active_and_queued(http):
    release = asyncio.Event()
    responses = [FakeResponse({"data": []}, block=release) for _ in range(3)]
    http.responses = responses.copy()
    service = ModelCatalogService()
    tasks = [asyncio.create_task(service.fetch(request())) for _ in range(3)]
    await asyncio.wait_for(
        asyncio.gather(responses[0].started.wait(), responses[1].started.wait()), 1
    )
    assert len(http.sessions) == 2 and len(service._workers) == 3
    assert not responses[2].started.is_set()
    await service.close()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert all(session.closed for session in http.sessions)
    assert not service._workers
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.reason == "closed" and error.value.status_code == 503
    await service.close()


@pytest.mark.asyncio
async def test_caller_cancellation_releases_owned_session(http):
    response = FakeResponse({"data": []}, block=asyncio.Event())
    http.responses = [response, FakeResponse({"data": []})]
    service = ModelCatalogService()
    task = asyncio.create_task(service.fetch(request()))
    await asyncio.wait_for(response.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert http.sessions[0].closed and not service._workers
    assert (await service.fetch(request()))["models"] == []
    await service.close()


@pytest.mark.asyncio
async def test_timeout_is_total_and_safe(http, monkeypatch):
    monkeypatch.setattr(catalog, "_TIMEOUT", 0.02)
    http.responses = [
        FakeResponse({"data": []}, block=asyncio.Event()) for _ in range(3)
    ]
    service = ModelCatalogService()
    results = await asyncio.gather(
        *(service.fetch(request()) for _ in range(3)), return_exceptions=True
    )
    assert all(
        isinstance(result, ModelCatalogError)
        and result.reason == "timeout"
        and result.status_code == 504
        for result in results
    )
    assert all(session.closed for session in http.sessions)
    assert not service._workers
    await service.close()


@pytest.mark.asyncio
async def test_vision_borrows_only_get_models_and_filters_echoes(http):
    class Provider:
        calls = 0

        async def get_models(self):
            self.calls += 1
            return ["fake-vision", "fake-vision", "fake-other", "Bearer fake-private"]

        async def terminate(self):
            raise AssertionError("Borrowed providers must not be terminated")

    provider = Provider()
    service = ModelCatalogService()
    result = await service.fetch_vision(provider)
    assert result["models"] == [
        {"id": "fake-vision", "label": "fake-vision"},
        {"id": "fake-other", "label": "fake-other"},
    ]
    assert provider.calls == 1
    assert "过滤" in result["warning"]
    assert not http.sessions
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("models", [None, {}, {"data": []}, [None], [{"id": "fake"}]])
async def test_vision_invalid_shape_is_not_empty(models):
    async def get_models():
        return models

    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch_vision(SimpleNamespace(get_models=get_models))
    assert error.value.reason == "invalid_response"
    await service.close()


@pytest.mark.asyncio
async def test_vision_errors_are_safe_and_not_dashboard_401():
    async def get_models():
        raise RuntimeError(f"401 Authorization Bearer {FAKE_KEY}")

    service = ModelCatalogService()
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch_vision(SimpleNamespace(get_models=get_models))
    assert error.value.status_code == 502 and FAKE_KEY not in str(error.value)
    await service.close()


@pytest.mark.asyncio
async def test_vision_cancellation_close_and_late_request():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def get_models():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service = ModelCatalogService()
    provider = SimpleNamespace(get_models=get_models)
    task = asyncio.create_task(service.fetch_vision(provider))
    await asyncio.wait_for(entered.wait(), 1)
    await service.close()
    assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch_vision(provider)
    assert error.value.reason == "closed"


@pytest.mark.asyncio
async def test_vision_maximum_models():
    async def get_models():
        return [f"fake-vision-{i}" for i in range(2001)]

    service = ModelCatalogService()
    result = await service.fetch_vision(SimpleNamespace(get_models=get_models))
    assert len(result["models"]) == 2000 and result["truncated"]
    await service.close()
