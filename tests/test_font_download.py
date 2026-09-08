import pytest

from tl import help_renderer


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("mirror_available", [True, False])
async def test_font_prefers_mirror_and_falls_back_to_official(
    tmp_path, monkeypatch, explicit, mirror_available
):
    import aiohttp

    target = tmp_path / "font.otf"
    calls = []
    data = b"OTTO" + b"0" * 1_000_000
    monkeypatch.setattr(help_renderer, "_find_existing_font_in_tl", lambda: None)
    monkeypatch.setattr(help_renderer, "_get_font_path", lambda: target)
    monkeypatch.setattr(help_renderer.os.path, "exists", lambda path: False)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment:8080")
    monkeypatch.setattr(help_renderer.ImageFont, "truetype", lambda *args: object())

    class Response:
        def __init__(self, url, proxy):
            self.status = (
                404
                if proxy or ("astrdark.cyou" in url and not mirror_available)
                else 200
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return data

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, *, proxy):
            calls.append((url, proxy))
            return Response(url, proxy)

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    assert await help_renderer.ensure_font_downloaded(
        "http://configured:8080" if explicit else None
    )
    assert target.read_bytes() == data
    proxy = "http://configured:8080" if explicit else "http://environment:8080"
    mirror, official = help_renderer.FONT_DOWNLOAD_URLS
    assert mirror.startswith("https://astrdark.cyou/gh/")
    expected = [(mirror, proxy), (mirror, None)]
    if not mirror_available:
        expected += [(official, proxy), (official, None)]
    assert calls == expected


@pytest.mark.asyncio
async def test_existing_font_does_not_require_network(tmp_path, monkeypatch):
    target = tmp_path / "font.otf"
    monkeypatch.setattr(help_renderer, "_find_existing_font_in_tl", lambda: target)
    assert await help_renderer.ensure_font_downloaded()
