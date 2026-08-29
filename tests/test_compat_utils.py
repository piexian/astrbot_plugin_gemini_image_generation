"""tests for tl/api/compat_utils.py"""

from __future__ import annotations

from tl.api.compat_utils import (
    build_generation_config,
    find_markdown_relative_image_urls,
    is_temp_cache_url,
    origin_from_api_base,
    resolve_relative_url,
)


def test_origin_from_api_base() -> None:
    assert origin_from_api_base("https://gw.example.com/v1") == "https://gw.example.com"
    assert origin_from_api_base("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert origin_from_api_base("not-a-url") is None
    assert origin_from_api_base(None) is None
    assert origin_from_api_base("") is None


def test_is_temp_cache_url() -> None:
    assert is_temp_cache_url("https://gw.example.com/images/users-abc/img.png")
    assert is_temp_cache_url("https://gw.example.com/temp/image/x.jpg")
    assert not is_temp_cache_url("https://gw.example.com/images/normal.png")


def test_find_markdown_relative_image_urls_dedup_and_normalize() -> None:
    text = (
        "![a](/images/x.png) ![b](images/y.png) ![c](/images/x.png) "
        "![d](https://cdn.example.com/z.png) ![e](data:image/png;base64,AAA) "
        "![f](https://gw.example.com/temp/image/t.jpg)"
    )
    assert find_markdown_relative_image_urls(text) == ["/images/x.png", "/images/y.png"]
    assert find_markdown_relative_image_urls("") == []
    assert find_markdown_relative_image_urls(None) == []  # type: ignore[arg-type]


def test_build_generation_config_defaults_and_custom_keys() -> None:
    assert build_generation_config() == {}
    assert build_generation_config(resolution="2K", aspect_ratio="16:9") == {
        "image_size": "2K",
        "aspect_ratio": "16:9",
    }
    assert build_generation_config(
        resolution="1K", resolution_key="size", aspect_ratio_key="ratio"
    ) == {"size": "1K"}


def test_resolve_relative_url() -> None:
    assert (
        resolve_relative_url("https://gw.example.com", "/images/x.png")
        == "https://gw.example.com/images/x.png"
    )
    assert resolve_relative_url(None, "/images/x.png") is None
