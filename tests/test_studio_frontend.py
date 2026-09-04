from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "pages" / "studio" / "index.html").read_text(encoding="utf-8")


def _image_loader_source() -> str:
    start = APP_JS.index("const ImageLoader = {")
    end = APP_JS.index("// 3. BridgeClient", start)
    return APP_JS[start:end]


def test_image_loader_uses_bridge_bytes_without_iframe_credentials() -> None:
    source = _image_loader_source()

    assert "BridgeClient.get(" in source
    assert "webui/image_b64/" in source
    assert "data:${mime};base64,${b64}" in source
    assert "localStorage" not in source
    assert "fetch(" not in source
    assert "SAFE_IMAGE_PATH_PREFIX" not in APP_JS
    assert "图片已随配额自动清理" not in APP_JS
    assert "原图已随存储配额自动清理" not in APP_JS
    assert "图片加载失败或已清理" in APP_JS


def test_image_loader_uses_thumbnails_except_for_lightbox() -> None:
    assert "ImageLoader.attach(this.img, item.imageName, { thumb: false })" in APP_JS
    assert APP_JS.count("{ thumb: true }") == 5


def test_seed_control_is_capability_driven_and_submitted() -> None:
    assert 'id="item-seed"' in INDEX_HTML
    assert 'id="input-seed"' in INDEX_HTML
    assert "params.seed?.type === 'integer'" in APP_JS
    assert "seed," in APP_JS
