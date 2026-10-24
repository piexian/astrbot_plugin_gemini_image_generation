from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def collect_sources(monkeypatch):
    # conftest provides a tl_utils stub; load the actual module under another name.
    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))
    name = "tl._file_image_sources_test_utils"
    source = Path(__file__).resolve().parents[1] / "tl" / "tl_utils.py"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module.collect_image_sources


class File:
    def __init__(self, file_=None, url=None):
        self.file_ = file_
        self.url = url
        self.file_reads = 0

    @property
    def file(self):
        self.file_reads += 1
        raise AssertionError("metadata collection must not invoke the download getter")


def event_with(*components):
    return types.SimpleNamespace(get_messages=lambda: components)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_value", "url", "expected"),
    [
        ("/tmp/reference.png", None, ["/tmp/reference.png"]),
        ("file:///tmp/reference.png", "", ["file:///tmp/reference.png"]),
        (
            None,
            "https://example.invalid/image.png",
            ["https://example.invalid/image.png"],
        ),
        (
            "",
            "https://example.invalid/image.png",
            ["https://example.invalid/image.png"],
        ),
        (
            "/tmp/reference.png",
            "https://example.invalid/image.png",
            ["/tmp/reference.png"],
        ),
        ("", "", []),
    ],
)
async def test_file_metadata_avoids_download_getter(
    collect_sources, file_value, url, expected
):
    component = File(file_value, url)

    sources = await collect_sources(event_with(component), log_debug=lambda _: None)

    assert sources == expected
    assert component.file_reads == 0


@pytest.mark.asyncio
async def test_file_without_backing_field_uses_url(collect_sources):
    component = File(url="https://example.invalid/image.png")
    del component.file_

    sources = await collect_sources(event_with(component), log_debug=lambda _: None)

    assert sources == [component.url]
    assert component.file_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component_name", "children_field"),
    [("Reply", "chain"), ("Node", "content"), ("Nodes", "nodes")],
)
async def test_nested_file_metadata_remains_deduplicated(
    collect_sources, component_name, children_field
):
    component = File(url="https://example.invalid/image.png")
    wrapper = type(component_name, (), {})()
    setattr(wrapper, children_field, [component])

    sources = await collect_sources(
        event_with(component, wrapper), log_debug=lambda _: None
    )

    assert sources == [component.url]
    assert component.file_reads == 0
