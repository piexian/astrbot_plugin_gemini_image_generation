from __future__ import annotations

from pathlib import Path

from tl.llm_output import image_locations, sanitize_task_result, sanitize_text


def test_image_locations_prefers_paths_and_keeps_public_urls(tmp_path: Path) -> None:
    local = tmp_path / "image.png"
    local.write_bytes(b"png")

    paths, urls = image_locations(
        [
            "https://public.example/image.png",
            "https://private.example/image.png?token=secret",
            "data:image/png;base64,iVBORw0KGgo=",
        ],
        [str(local)],
    )

    assert paths == [str(local)]
    assert urls == ["https://public.example/image.png"]


def test_sanitize_text_removes_inline_data() -> None:
    text = "data:image/png;base64,iVBORw0KGgo= and normal text"
    cleaned = sanitize_text(text)
    assert cleaned is not None
    assert "data:image" not in cleaned
    assert "normal text" in cleaned


def test_sanitize_task_result_keeps_only_safe_locations(tmp_path: Path) -> None:
    local = tmp_path / "image.png"
    local.write_bytes(b"png")
    result = sanitize_task_result(
        {
            "image_paths": [str(local)],
            "image_urls": [
                "https://public.example/image.png",
                "https://private.example/image.png?sig=secret",
            ],
            "text_content": "data:image/png;base64,iVBORw0KGgo=",
        }
    )

    assert result["image_paths"] == [str(local)]
    assert result["image_urls"] == ["https://public.example/image.png"]
    assert "data:image" not in result["text_content"]
