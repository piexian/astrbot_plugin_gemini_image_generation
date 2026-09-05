import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "pages" / "studio" / "index.html").read_text(encoding="utf-8")


def test_safe_dom_false_disabled_keeps_buttons_clickable() -> None:
    start = APP_JS.index("const SafeDOM = {")
    end = APP_JS.index("const ImageLoader = {", start)
    script = "const assert = require('node:assert/strict');\n"
    script += """
const document = {
  createElement() {
    return {
      attributes: new Map(),
      setAttribute(name, value) { this.attributes.set(name, String(value)); },
      toggleAttribute(name, force) {
        if (force) this.attributes.set(name, '');
        else this.attributes.delete(name);
      },
      get disabled() { return this.attributes.has('disabled'); }
    };
  }
};
"""
    script += APP_JS[start:end]
    script += """
assert.equal(SafeDOM.el('button', {disabled: false}).disabled, false);
assert.equal(SafeDOM.el('button', {disabled: true}).disabled, true);
assert.equal(SafeDOM.el('button').disabled, false);
const button = SafeDOM.el('button', {disabled: false, 'aria-disabled': false});
assert.equal(button.disabled, false);
assert.equal(button.attributes.get('aria-disabled'), 'false');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_requester_labels_distinguish_chats_and_legacy_records() -> None:
    start = APP_JS.index("const SafeDOM = {")
    end = APP_JS.index("const ImageLoader = {", start)
    script = "const assert = require('node:assert/strict');\n" + APP_JS[start:end]
    script += """
const group = {chat_type: 'group', group_id: '123456', user_id: '10001', user_name: '小明'};
assert.deepEqual(SafeDOM.requesterLabels(group, 'command'),
  ['群聊 · 群号: 123456', '用户: 小明（ID: 10001）']);
assert.deepEqual(SafeDOM.requesterLabels({chat_type: 'private', user_id: '10001'}, 'llm_tool'),
  ['私聊', '用户 ID: 10001']);
assert.deepEqual(SafeDOM.requesterLabels({chat_type: 'group'}, 'llm_batch'),
  ['群聊 · 群号未记录']);
assert.deepEqual(SafeDOM.requesterLabels({group_id: '654321'}, 'command'),
  ['群聊 · 群号: 654321']);
assert.deepEqual(SafeDOM.requesterLabels({user_id: '10001'}, 'command'),
  ['会话类型未知', '用户 ID: 10001']);
assert.deepEqual(SafeDOM.requesterLabels({user_name: 'admin'}, 'webui'),
  ['工作台提交', '用户: admin']);
assert.deepEqual(SafeDOM.requesterLabels(undefined, 'legacy'), ['会话类型未知']);
assert.deepEqual(SafeDOM.requesterLabels({user_name: '<img onerror=alert(1)>'}, 'command'),
  ['会话类型未知', '用户: <img onerror=alert(1)>']);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


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


def test_workbench_uses_server_candidate_and_limits() -> None:
    start = APP_JS.index("class WorkbenchView {")
    end = APP_JS.index("class ProgressView {", start)
    script = "const assert = require('node:assert/strict');\n"
    script += "const SafeDOM = {setText() {}}; const Toast = {warning() {}};\n"
    script += APP_JS[start:end]
    script += """
const view = Object.create(WorkbenchView.prototype);
view.selectedModel = () => ({max_reference_images: 14});
assert.equal(view.getMaxReferenceImages(), 14);
view.selectedModel = () => ({max_reference_images: 0});
assert.equal(view.getMaxReferenceImages(), 0);
view.store = {mode: 'batch', batchItems: [{image_count: 3}, {image_count: 2}]};
view.batchBudgetLimit = 4;
view.batchMaxTasks = 2;
view.formAvailable = true;
view.btnAddBatchItem = {};
view.batchBudgetInfo = {classList: {add() {}, remove() {}}};
view.updateActionState = () => {};
view.calculateBatchBudget();
assert.equal(view.batchBudgetValid, false);
assert.equal(view.btnAddBatchItem.disabled, true);
view.batchBudgetLimit = 9;
view.calculateBatchBudget();
assert.equal(view.batchBudgetValid, true);
view.batchMaxTasks = 1;
view.calculateBatchBudget();
assert.equal(view.batchBudgetValid, false);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert "candidate_id: selectedModel?.candidate_id" in APP_JS
    assert "this.uploadMaxMb = limits.upload_max_mb" in APP_JS
    assert "file.size > this.uploadMaxMb * 1024 * 1024" in APP_JS
    assert 'id="upload-max-size"' in INDEX_HTML
    assert "SafeDOM.setText(this.uploadLimitHint, `${this.uploadMaxMb}MB`)" in APP_JS
    assert "≤20MB" not in INDEX_HTML
