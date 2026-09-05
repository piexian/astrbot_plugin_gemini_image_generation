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
    assert APP_JS.count("{ thumb: true }") == 6


def test_generation_editor_uses_typed_fields_and_conditions() -> None:
    script = _settings_script()
    script += """
const fields = {
  watermark: {type: 'boolean', label: '水印', value: true},
  seed: {type: 'integer', label: '种子', minimum: 0, maximum: 100, value: 7},
  negative_prompt: {type: 'string', label: '负向提示词', max_length: 500, value: 'default', multiline: true},
  size_mode: {type: 'string', label: '尺寸模式', enum: ['preset', 'custom'], max_length: 20, value: 'preset'},
  custom_size: {type: 'string', label: '自定义尺寸', max_length: 20, value: '1536x1024', condition: {size_mode: 'custom'}}
};
const editor = new GenerationSettingsEditor(new Node(), () => {});
editor.render(fields, {watermark: false, seed: 0, negative_prompt: '', size_mode: 'custom', custom_size: '1024x1024'});
assert.deepEqual(editor.overrides(), {watermark: false, seed: 0, negative_prompt: '', size_mode: 'custom', custom_size: '1024x1024'});
editor.controls.get('size_mode').input.value = 'preset';
editor.updateConditions();
assert.equal(editor.controls.get('custom_size').row.hidden, true);
assert.equal(editor.overrides().custom_size, undefined);
assert.equal(editor.overrides({includeHidden: true}).custom_size, '1024x1024');
editor.controls.get('seed').input.value = '101';
assert.throws(() => editor.overrides(), /种子/);
assert.equal(editor.render(fields, {seed: true, api_keys: ['secret']}), true);
assert.deepEqual(editor.overrides(), {});
"""
    _run_settings_script(script)


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


def test_display_route_prefers_success_stats_and_keeps_request_fallback() -> None:
    start = APP_JS.index("const SafeDOM = {")
    end = APP_JS.index("const ImageLoader = {", start)
    script = "const assert = require('node:assert/strict');\n" + APP_JS[start:end]
    script += """
const params = {provider: 'requested', model: 'requested-model'};
assert.deepEqual(SafeDOM.displayRoute({params, stats: {
  provider: 'actual', model: 'actual-model', alias: '高清'
}}), {provider: 'actual', model: '高清（actual-model）'});
assert.deepEqual(SafeDOM.displayRoute({params, stats: {}}), params);
assert.deepEqual(SafeDOM.displayRoute({params}), params);
assert.deepEqual(SafeDOM.displayRoute({stats: {provider: 'actual', model: 'model'}}),
  {provider: 'actual', model: 'model'});
assert.deepEqual(params, {provider: 'requested', model: 'requested-model'});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_refill_restores_advanced_fields_and_clears_stale_values() -> None:
    script = _settings_script(include_workbench=True)
    script += """
let warnings = 0;
const Toast = {warning() {warnings++;}, info() {}};
const view = Object.create(WorkbenchView.prototype);
view.switchMode = () => {};
view.promptInput = new Node();
view.promptCharCount = new Node();
view.inputImageCount = new Node();
view.selectResolution = new Node();
view.selectAspectRatio = new Node();
view.selectModel = new Node(); view.selectModel.value = '0';
view.moreParamsDisclosure = new Node();
view.btnToggleMoreParams = new Node();
view.moreParamsPanel = new Node();
view.moreParamsToggleIcon = new Node();
view.overrideCount = new Node();
view.preferences = new StudioPreferences();
view.settingsEditor = new GenerationSettingsEditor(new Node(), () => {});
view.updateReferenceCounter = () => {};
view.store = {capabilities: [{candidate_id: 'test#1', provider: 'test', model: 'model', parameters: {}, generation_fields: {
  seed: {type: 'integer', label: '种子', value: 7, minimum: 0, maximum: 100, runtime_parameter: 'seed'},
  negative_prompt: {type: 'string', label: '负向提示词', value: 'configured', max_length: 500, runtime_parameter: 'negative_prompt'}
}}]};
view.refillParameters('draw', {seed: 0, negative_prompt: 'blur'});
assert.deepEqual(view.settingsEditor.overrides(), {seed: 0, negative_prompt: 'blur'});
assert.equal(warnings, 0);
view.refillParameters('legacy', {});
assert.deepEqual(view.settingsEditor.overrides(), {});
assert.equal(view.settingsEditor.controls.get('seed').input.value, '7');
assert.equal(view.settingsEditor.controls.get('seed').enabled.checked, false);
view.refillParameters('new', {generation_settings: {seed: 42, negative_prompt: ''}});
assert.deepEqual(view.settingsEditor.overrides(), {seed: 42, negative_prompt: ''});
for (const seed of [true, '42', 101, -1, 1.5]) {
  view.refillParameters('invalid', {seed, negative_prompt: 'x'.repeat(501)});
  assert.deepEqual(view.settingsEditor.overrides(), {});
}
assert.equal(warnings, 5);
view.store.capabilities[0].generation_fields = {};
view.refillParameters('unsupported', {seed: 42, negative_prompt: 'blur'});
assert.deepEqual(view.settingsEditor.overrides(), {});
assert.equal(warnings, 6);
"""
    _run_settings_script(script)


def test_image_loader_keeps_preview_flag_in_cache_and_ignores_stale_response() -> None:
    script = "const assert = require('node:assert/strict');\n"
    script += """
const SafeDOM = {isSafeImageName: () => true};
let calls = 0;
const BridgeClient = {async get() {
  calls++;
  return {mime: 'image/jpeg', b64: 'YQ==', preview: true};
}};
"""
    script += _image_loader_source()
    script += """
(async () => {
  const img = {dispatchEvent() {throw new Error('Unexpected image error');}};
  const first = await ImageLoader.attach(img, 'large.jpg', {thumb: false});
  assert.equal(first.preview, true);
  const cached = await ImageLoader.attach(img, 'large.jpg', {thumb: false});
  assert.equal(cached.preview, true);
  assert.equal(img.src, 'data:image/jpeg;base64,YQ==');
  assert.equal(calls, 1);
  let resolveOld;
  BridgeClient.get = () => new Promise(resolve => {resolveOld = resolve;});
  const oldRequest = ImageLoader.attach(img, 'old.png', {thumb: false});
  BridgeClient.get = async () => ({mime: 'image/png', b64: 'Yg==', preview: false});
  const current = await ImageLoader.attach(img, 'current.png', {thumb: false});
  assert.equal(current.preview, false);
  resolveOld({mime: 'image/jpeg', b64: 'YQ==', preview: true});
  assert.equal(await oldRequest, undefined);
  assert.equal(img.src, 'data:image/png;base64,Yg==');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def _settings_script(*, include_workbench: bool = False) -> str:
    script = """
const assert = require('node:assert/strict');
const stored = new Map();
const window = {localStorage: {getItem(key) {return stored.get(key) || null;}, setItem(key, value) {stored.set(key, value);}}};
class Node {
  constructor() {this.children = []; this.style = {}; this.dataset = {}; this.attributes = {}; this.value = ''; this.checked = false; this.handlers = {};}
  appendChild(child) {this.children.push(child); return child;}
  replaceChildren(...children) {this.children = children;}
  setAttribute(name, value) {this.attributes[name] = String(value); if (name === 'value') this.value = String(value);}
  toggleAttribute(name, value) {if (value) this.attributes[name] = ''; else delete this.attributes[name];}
  addEventListener(name, handler) {this.handlers[name] = handler;}
  focus() {}
  get options() {return this.children;}
  get selectedIndex() {return this.children.findIndex(child => child.value === this.value);}
}
const document = {
  createElement: () => new Node(),
  createTextNode(value) {const node = new Node(); node.textContent = value; return node;},
  getElementById: () => new Node()
};
const IconSet = {};
"""
    start = APP_JS.index("const SafeDOM = {")
    script += APP_JS[start : APP_JS.index("const ImageLoader = {", start)]
    start = APP_JS.index("class StudioPreferences {")
    end = APP_JS.index(
        "class ProgressView {" if include_workbench else "class WorkbenchView {", start
    )
    script += APP_JS[start:end]
    return script


def _run_settings_script(script: str) -> None:
    result = subprocess.run(["node"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_browser_preferences_restore_each_candidate_and_reset_independently() -> None:
    script = _settings_script()
    script += """
const one = {candidate_id: 'test#1', provider: 'test', model: 'one'};
const two = {candidate_id: 'test#2', provider: 'test', model: 'two'};
const prefs = new StudioPreferences();
prefs.remember(one, {resolution: '2K', image_count: 2, generation_settings: {seed: 0, watermark: false, negative_prompt: ''}});
prefs.remember(two, {resolution: '4K', generation_settings: {seed: 42}});
prefs.select(two);
const reloaded = new StudioPreferences();
assert.equal(reloaded.matches(reloaded.data.selected, two), true);
assert.deepEqual(reloaded.get(one).generation_settings, {seed: 0, watermark: false, negative_prompt: ''});
assert.equal(reloaded.get(two).resolution, '4K');
assert.equal(reloaded.get({...one, model: 'changed'}), null);
reloaded.reset(one);
const reset = new StudioPreferences();
assert.equal(reset.get(one), null);
assert.equal(reset.get(two).generation_settings.seed, 42);
stored.set(prefs.key, 'invalid JSON');
assert.deepEqual(new StudioPreferences().data.candidates, {});
"""
    _run_settings_script(script)


def test_sandbox_preferences_restore_through_bridge_with_ordered_revisions() -> None:
    script = _settings_script()
    script += """
Object.defineProperty(window, 'localStorage', {get() {throw new Error('sandbox');}});
const one = {candidate_id: 'test#1', provider: 'test', model: 'one'};
const calls = [];
const BridgeClient = {
  async get(route) {
    assert.equal(route, 'webui/preferences');
    return {revision: 100, preferences: {selected: one, candidates: {}}};
  },
  async post(route, body) {calls.push({route, body});}
};
(async () => {
  const prefs = new StudioPreferences();
  assert.equal(prefs.remote, true);
  await prefs.load();
  assert.equal(prefs.matches(prefs.data.selected, one), true);
  prefs.remember(one, {generation_settings: {seed: 0}});
  prefs.remember(one, {generation_settings: {seed: 42}});
  assert.equal(calls.length, 2);
  assert.equal(calls[0].route, 'webui/preferences');
  assert.equal(calls[0].body.preferences.candidates['test#1'].generation_settings.seed, 0);
  assert.equal(calls[1].body.preferences.candidates['test#1'].generation_settings.seed, 42);
  assert.ok(calls[1].body.revision > calls[0].body.revision);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    _run_settings_script(script)
