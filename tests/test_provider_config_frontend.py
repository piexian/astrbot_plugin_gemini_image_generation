"""Run the actual isolated provider controller and SafeDOM with bubbling DOM/bridge."""

import json
import subprocess
from pathlib import Path

import pytest
from test_studio_limits_frontend import DOM as LIMITS_DOM

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "pages/studio/app.js").read_text(encoding="utf-8")
SOURCE = APP[: APP.index("const ImageLoader = {")]
SOURCE += (ROOT / "pages/studio/provider-config.js").read_text(encoding="utf-8")
SETTINGS = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))[
    "provider_settings"
]["items"]
TEMPLATES = {
    name: {"label": template["description"], "fields": template["items"]}
    for name, template in SETTINGS["provider_overrides"]["templates"].items()
}
COMMON_FIELDS = {
    name: SETTINGS[name] for name in ("proxy", "vision_provider_id", "vision_model")
}
PAGE = {
    "tag": "main",
    "attrs": {},
    "children": [
        {"tag": "section", "attrs": {"id": "panel-providers"}, "children": []},
        {"tag": "div", "attrs": {"id": "modal-body"}, "children": []},
        {"tag": "div", "attrs": {"id": "modal-footer"}, "children": []},
    ],
}
DOM = (
    LIMITS_DOM[: LIMITS_DOM.index("const calls = [];")]
    + r"""
// Model bubbling cancellation locally; the shared limits harness remains unchanged.
Node.prototype.dispatch = function(type, extra = {}) {
  const event = {type, target: this, defaultPrevented: false, cancelBubble: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.cancelBubble = true; }, ...extra};
  for (let node = this; node; node = node.parentNode) {
    for (const fn of [...(node.handlers.get(type) || [])]) fn(event);
    if (event.cancelBubble) break;
  }
  return event;
};
const originalAppend = Node.prototype.appendChild;
Node.prototype.appendChild = function(child) {
  const firstOption = this.tagName === 'select' && !this.children.length;
  const result = originalAppend.call(this, child);
  if (firstOption) this.value = child.value;
  return result;
};
Node.prototype.setPointerCapture = function(id) { this.capture = id; };
Node.prototype.hasPointerCapture = function(id) { return this.capture === id; };
Node.prototype.releasePointerCapture = function(id) { if (this.capture === id) this.capture = null; };
// Deterministic row rectangles verify controller math, not browser layout/scrolling.
Node.prototype.getBoundingClientRect = function() {
  return {top: Number(this.dataset.pcPollIndex) * 100, height: 80};
};
const byId = id => document.getElementById(id);
const root = byId('panel-providers'), body = byId('modal-body'), footer = byId('modal-footer');
const calls = [];
const fields = TEMPLATES.google.fields;
const known = (id = 'opaque-0', type = 'google', extra = {}) => ({id, api_type: type, supported: true,
  values: {enabled: true, model: 'model-free-form', priority: 0, api_keys: ['fake-old-one', 'fake-old-two'], api_base: 'https://example.invalid', ...extra},
  secrets: {api_keys: {present: true, count: 2}}, unknown_fields: ['legacy_unknown']});
const response = () => ({revision: 'r1', key_values_visible: true,
  model_catalog: {google: {supported: true}, doubao: {supported: false, message: '暂未接入'}},
  provider_polling: [], entries: [known(), known('opaque-1', 'doubao', {enabled: false})],
  common: {values: {proxy: '', vision_provider_id: '', vision_model: ''}, secrets: {}},
  templates: TEMPLATES, common_fields: COMMON_FIELDS,
  provider_types: Object.keys(TEMPLATES).map(id => ({id, label: id})),
  vision_providers: [{id: 'vision-1', label: 'Vision One'}], busy: false, requires_reload: false});
let server = response(), available = true, saved = 0;
let get = async () => server;
let post = async (route, payload) => ({...server, revision: 'r2', provider_polling: payload.provider_polling,
  entries: payload.entries.map((entry, i) => ({...entry, id: entry.id ?? `new-${i}`, supported: !!TEMPLATES[entry.api_type],
    secrets: {api_keys: {present: true, count: entry.secret_actions.api_keys?.mode === 'replace' ? entry.secret_actions.api_keys.value.length : 2}}, unknown_fields: []})),
  common: {values: payload.common.values, secrets: {}}});
const bridge = {isAvailable: () => available,
  get: async route => { calls.push({method: 'get', route}); return get(route); },
  post: async (route, payload) => { calls.push({method: 'post', route, payload}); return post(route, payload); }};
let confirmation = false, confirms = 0;
const modal = {
  confirm: async () => { confirms++; return confirmation; },
  openCustom(options) {
    assert.equal(options.variant, 'parameters');
    if (this.options) this.close(false);
    this.options = options;
    options.renderBody(body);
    options.renderFooter(footer, () => this.close(true), () => this.close(false));
  },
  close(accepted = false) {
    const options = this.options; this.options = null;
    body.replaceChildren(); footer.replaceChildren(); options?.onClose?.(accepted);
  }
};
const sandbox = {Node, document, window, console};
vm.createContext(sandbox);
vm.runInContext(SOURCE + '\nthis.SafeDOM = SafeDOM;', sandbox);
const create = () => new window.StudioProviderConfigView({root, bridge, dom: sandbox.SafeDOM, modal, onSaved: async () => { saved++; }});
const action = (name, index, container = root) => container.querySelector(`[data-pc-action="${name}"]${index == null ? '' : `[data-pc-index="${index}"]`}`);
const field = (name, container = body) => container.querySelector(`[data-pc-field="${name}"]`);
const mode = (name, container = body) => container.querySelector(`[data-pc-secret-mode="${name}"]`);
const input = (node, value) => { assert.ok(node); node.value = value; node.dispatch('input'); };
const change = (node, value) => { assert.ok(node); node.value = value; node.dispatch('change'); };
const selectTab = name => root.querySelector(`[data-pc-tab="${name}"]`).click();
const apply = () => action('apply-edit', null, footer).click();
const edit = index => action('edit', index).click();
const keyInput = name => body.querySelector(`[data-pc-key-input="${name}"]`);
const keyAction = (name, index) => {
  const button = action(name, index, body) || action(name, index, footer);
  assert.ok(button, `missing key action ${name}`); return button;
};
const manageKeys = () => keyAction('manage-keys').click();
const listedKeys = () => body.querySelectorAll('[data-pc-action="edit-key"]').map(node => node.textContent);
// Replace via the actual row controls, never via a synthetic multiline field.
const setKeys = values => {
  manageKeys();
  while (action('remove-key', 0, body)) keyAction('remove-key', 0).click();
  for (const value of values) { input(keyInput('newValue'), value); keyAction('add-key').click(); }
  assert.equal(keyAction('apply-keys').disabled, false); keyAction('apply-keys').click();
};
const posts = () => calls.filter(call => call.method === 'post');
const plain = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
"""
)


def _run(body: str) -> None:
    script = "\n".join(
        "const " + name + " = " + json.dumps(value, ensure_ascii=False) + ";"
        for name, value in {
            "SOURCE": SOURCE,
            "PAGE": PAGE,
            "TEMPLATES": TEMPLATES,
            "COMMON_FIELDS": COMMON_FIELDS,
        }.items()
    )
    script += DOM + "\n(async () => {\n" + body
    script += "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
    result = subprocess.run(
        ["node"], input=script, text=True, capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_lazy_tabs_editor_grouping_aria_and_scoped_explicit_save():
    _run(r"""
const view = create();
assert.equal(calls.length, 0); assert.equal(view.saveButton.disabled, true);
await Promise.all([view.open(), view.open()]);
assert.equal(calls.length, 1); assert.equal(calls[0].route, 'webui/providers');
assert.equal(view.tab, 'entries'); assert.equal(root.querySelectorAll('table').length, 1);
assert.equal(root.querySelectorAll('[data-pc-field]').length, 0);
assert.match(root.textContent, /原始配置.*禁用.*不完整/);
edit(0);
assert.ok(modal.options); assert.equal(root.querySelectorAll('[data-pc-field]').length, 0);
assert.equal(body.querySelector('[data-pc-group="advanced"]').getAttribute('open'), undefined);
assert.ok(body.querySelector('[data-pc-group="generation"]'));
assert.equal(field('model').tagName, 'input');
assert.equal(field('model').getAttribute('aria-label'), fields.model.description);
assert.equal(field('aspect_ratio').getAttribute('aria-label'), fields.aspect_ratio.description);
assert.equal(field('aspect_ratio').closest('label'), undefined);
assert.equal(mode('api_keys'), null); assert.equal(field('api_keys'), null);
assert.match(body.querySelector('[data-pc-key-summary]').textContent, /fake-old-one\+1/);
manageKeys(); assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two']);
keyAction('cancel-keys').click();
input(field('model'), '<img src=x onerror=alert(1)>'); apply();
assert.match(root.textContent, /<img src=x onerror=alert\(1\)>/); assert.equal(root.querySelectorAll('img').length, 0);
assert.equal(view.dirty, true); assert.equal(posts().length, 0);
root.querySelector('[data-pc-tab="entries"]').dispatch('keydown', {key: 'End'});
assert.equal(view.tab, 'common'); assert.equal(document.activeElement.getAttribute('aria-selected'), 'true');
change(field('vision_provider_id', root), 'vision-1'); input(field('vision_model', root), 'vision-model');
selectTab('entries'); edit(0); assert.equal(field('model').value, '<img src=x onerror=alert(1)>');
action('cancel-edit', null, footer).click();
action('save').click(); await tick();
const payload = posts()[0].payload;
assert.deepEqual(Object.keys(payload).sort(), ['common', 'entries', 'provider_polling', 'revision']);
assert.equal(payload.revision, 'r1');
assert.equal(payload.entries[0].secret_actions.api_keys, undefined);
assert.deepEqual(plain(payload.entries[0].values.api_keys), ['fake-old-one', 'fake-old-two']);
assert.equal(payload.entries[0].values.legacy_unknown, undefined);
assert.equal(payload.common.values.vision_provider_id, 'vision-1'); assert.equal(payload.common.values.vision_model, 'vision-model');
assert.equal(saved, 1); assert.equal(view.dirty, false); assert.equal(view.draft.revision, 'r2');
await view.open(); assert.equal(calls.filter(call => call.method === 'get').length, 1); view.destroy();
""")


def test_all_real_schema_templates_types_enums_conditions_and_new_key_actions():
    assert len(TEMPLATES) == 13
    assert sum(len(template["fields"]) for template in TEMPLATES.values()) == 219
    _run(r"""
const view = create(); await view.open();
for (const [type, template] of Object.entries(TEMPLATES)) {
  change(root.querySelector('[data-pc-new-type]'), type); action('add').click();
  for (const [name, schema] of Object.entries(template.fields)) {
    const control = field(name);
    assert.ok(control, `${type}.${name}`);
    assert.equal(control.getAttribute('aria-label'), schema.description);
    if (schema.options && schema.type !== 'list') assert.equal(control.tagName, 'select');
    if (name === 'model' && !schema.options) assert.equal(control.tagName, 'input');
  }
  if (template.fields.size_mode) {
    const wrap = body.querySelector('[data-pc-field-wrap="custom_size"]');
    assert.equal(wrap.hidden, true);
    change(field('size_mode'), 'custom'); assert.equal(wrap.hidden, false);
    input(field('custom_size'), '2048*1536');
    change(field('size_mode'), 'preset'); assert.equal(wrap.hidden, true);
    change(field('size_mode'), 'custom'); assert.equal(field('custom_size').value, '2048*1536');
  }
  action('cancel-edit', null, footer).click();
}
assert.equal(view.draft.entries.length, 2);
change(root.querySelector('[data-pc-new-type]'), 'minimax'); action('add').click();
setKeys(['new-key-one', 'new-key-two']);
assert.equal(field('enabled').checked, false);
input(field('style_weight'), '0.75'); field('enabled').click();
apply(); assert.equal(view.draft.entries.length, 3);
assert.equal(view.draft.entries[2].id, null); assert.equal(view.draft.entries[2].values.enabled, true);
assert.equal(view.draft.entries[2].values.style_weight, 0.75);
assert.deepEqual(plain(view.draft.entries[2].values.api_keys), ['new-key-one', 'new-key-two']);
assert.equal(view.draft.entries[2].secret_actions.api_keys, undefined);
action('save').click(); await tick(); assert.equal(posts()[0].payload.entries[2].id, null); view.destroy();
""")


def test_key_url_protection_unknown_entries_confirmation_and_identity():
    _run(r"""
server.entries[0].secrets.api_base = {present: true, preview: '已配置'};
delete server.entries[0].values.api_base;
server.common.secrets.proxy = {present: true, preview: '已配置'}; delete server.common.values.proxy;
server.entries.push({id: 'opaque-legacy', api_type: 'removed-vendor', supported: false, values: {}, secrets: {}, unknown_fields: ['old_private']});
const view = create(); await view.open();
assert.equal(action('edit', 2).disabled, true);
assert.equal(root.querySelector('[data-pc-new-type]').children.some(option => option.value === 'removed-vendor'), false);
edit(0); assert.equal(mode('api_base').value, 'keep'); assert.equal(field('api_base'), null);
setKeys(['fresh-key']);
change(mode('api_base'), 'replace'); input(field('api_base'), 'https://user:pw@example.invalid/?token=private');
apply();
selectTab('common'); assert.equal(mode('proxy', root).value, 'keep');
change(mode('proxy', root), 'clear');
selectTab('entries'); action('delete', 1).click(); await tick(); assert.equal(confirms, 1); assert.equal(view.draft.entries.length, 3);
confirmation = true; action('delete', 1).click(); await tick(); assert.equal(view.draft.entries.length, 2);
action('save').click(); await tick();
const payload = posts()[0].payload;
assert.equal(payload.entries[0].id, 'opaque-0');
assert.deepEqual(plain(payload.entries[0].values.api_keys), ['fresh-key']);
assert.equal(payload.entries[0].secret_actions.api_keys, undefined);
assert.equal(payload.entries[0].values.api_base, undefined);
assert.equal(payload.entries[0].secret_actions.api_base.value, 'https://user:pw@example.invalid/?token=private');
assert.deepEqual(plain(payload.entries[1]), {id: 'opaque-legacy', api_type: 'removed-vendor', values: {}, secret_actions: {}});
assert.deepEqual(plain(payload.common.secret_actions.proxy), {mode: 'clear'}); assert.equal(payload.common.values.proxy, undefined);
view.destroy();
""")


@pytest.mark.parametrize(
    "error",
    [
        {"status": 500, "message": "must-not-echo-sensitive-value"},
        {"status": 409, "data": {"reason": "busy"}},
        {"message": "生成任务正在运行，请稍后保存"},
    ],
)
def test_failed_or_busy_save_keeps_new_secrets_and_allows_retry(error):
    _run(
        "const saveError = "
        + json.dumps(error, ensure_ascii=False)
        + ";\n"
        + r"""
const view = create(); await view.open();
edit(0); setKeys(['retry-key']); input(field('model'), 'retry-model'); apply();
const pending = deferred(); post = () => pending.promise;
action('save').click(); assert.equal(view.saveButton.disabled, true);
assert.equal(root.querySelector('fieldset').disabled, true);
action('edit', 0).click(); assert.equal(view.editor, null);
action('save').click(); assert.equal(posts().length, 1);
pending.reject(saveError); await tick();
assert.equal(saved, 0); assert.equal(view.dirty, true); assert.equal(view.conflict, false);
assert.equal(view.saveButton.disabled, false); assert.match(view.status.textContent, /草稿完整保留/);
assert.doesNotMatch(view.status.textContent, /must-not-echo/);
edit(0); assert.equal(field('api_keys').value, 'retry-key'); assert.equal(field('model').value, 'retry-model');
action('cancel-edit', null, footer).click();
post = async () => ({...server, revision: 'r3'});
action('save').click(); await tick(); assert.equal(posts().length, 2);
assert.deepEqual(plain(posts()[1].payload.entries[0].values.api_keys), ['retry-key']);
assert.equal(posts()[1].payload.entries[0].secret_actions.api_keys, undefined);
assert.equal(saved, 1); assert.equal(view.draft.revision, 'r3'); view.destroy();
"""
    )


@pytest.mark.parametrize(
    "error",
    [
        {"status": 409, "data": {"reason": "revision"}},
        {"message": "供应商配置已更新，请重新加载后保存"},
        {"status_code": 409},
        {"statusCode": "409"},
        {"message": "供应商配置已变更，请重新加载"},
    ],
)
def test_revision_conflict_requires_explicit_confirmed_reload(error):
    _run(
        "const saveError = "
        + json.dumps(error, ensure_ascii=False)
        + ";\n"
        + r"""
const view = create(); await view.open();
edit(0); setKeys(['conflict-key']); apply();
post = async () => { throw saveError; };
action('save').click(); await tick(); assert.equal(view.conflict, true); assert.equal(view.draft.revision, 'r1');
assert.equal(view.saveButton.disabled, true); action('save').click(); await tick(); assert.equal(posts().length, 1);
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['conflict-key']);
assert.equal(view.draft.entries[0].secret_actions.api_keys, undefined);
action('reload').click(); await tick(); assert.equal(confirms, 1); assert.equal(view.dirty, true);
confirmation = true; server.revision = 'r9'; action('reload').click(); await tick();
assert.equal(confirms, 2); assert.equal(view.draft.revision, 'r9'); assert.equal(view.dirty, false);
assert.equal(view.conflict, false); assert.equal(saved, 0); view.destroy();
"""
    )


def test_polling_keyboard_pointer_touch_scope_and_original_table_order():
    _run(r"""
server.entries[1].values.enabled = true;
server.entries.push(known('opaque-2', 'openai'));
const view = create(); await view.open();
const original = plain(view.draft.entries); selectTab('polling');
assert.deepEqual(plain(view.draft.provider_polling), []);
assert.match(root.textContent, /不代表有效候选/);
const row = root.querySelector('[data-pc-poll-index="0"]');
assert.equal(row.dispatch('pointerdown', {pointerId: 1, pointerType: 'touch', button: 0}).defaultPrevented, false);
assert.equal(row.dispatch('pointermove', {pointerId: 1, clientY: 290}).defaultPrevented, false);
const handle = action('handle', 0);
assert.equal(handle.dispatch('pointerdown', {pointerId: 2, pointerType: 'touch', button: 0}).defaultPrevented, true);
assert.equal(handle.capture, 2);
handle.dispatch('pointermove', {pointerId: 2, clientY: 290});
handle.dispatch('pointerup', {pointerId: 2});
assert.equal(handle.capture, null); assert.deepEqual(plain(view.draft.provider_polling), ['doubao', 'openai', 'google']);
assert.match(root.textContent, /手动轮询/); assert.deepEqual(plain(view.draft.entries), original);
action('handle', 2).dispatch('keydown', {key: 'ArrowUp'});
assert.deepEqual(plain(view.draft.provider_polling), ['doubao', 'google', 'openai']);
action('up', 1).click(); assert.deepEqual(plain(view.draft.provider_polling), ['google', 'doubao', 'openai']);
action('down', 0).click(); assert.deepEqual(plain(view.draft.provider_polling), ['doubao', 'google', 'openai']);
action('remove-poll', 1).click(); assert.deepEqual(plain(view.draft.provider_polling), ['doubao', 'openai']);
root.querySelector('[data-pc-action="add-poll"][data-pc-type="google"]').click();
assert.deepEqual(plain(view.draft.provider_polling), ['doubao', 'openai', 'google']);
action('auto').click(); assert.deepEqual(plain(view.draft.provider_polling), []);
const cancelHandle = action('handle', 0);
cancelHandle.dispatch('pointerdown', {pointerId: 3, button: 0});
cancelHandle.dispatch('pointermove', {pointerId: 3, clientY: 290});
cancelHandle.dispatch('pointercancel', {pointerId: 3});
assert.deepEqual(plain(view.draft.provider_polling), []); assert.equal(cancelHandle.capture, null);
cancelHandle.dispatch('pointerdown', {pointerId: 4, button: 0}); view.destroy();
assert.equal(cancelHandle.capture, null); assert.equal(root.children.length, 0);
assert.equal([...root.handlers.values()].reduce((total, handlers) => total + handlers.size, 0), 0);
""")


def test_validation_enum_range_and_hidden_fields_retained():
    _run(r"""
const view = create(); await view.open();
change(root.querySelector('[data-pc-new-type]'), 'openai_images'); action('add').click();
input(field('output_compression'), '101'); apply(); assert.ok(view.editor); assert.match(body.textContent, /超出允许范围/);
input(field('output_compression'), '5.5'); apply(); assert.ok(view.editor); assert.match(body.textContent, /有效数字/);
input(field('output_compression'), ''); apply(); assert.ok(view.editor); assert.match(body.textContent, /请输入数字/);
input(field('output_compression'), '50'); change(field('quality'), 'not-an-option'); apply();
assert.ok(view.editor); assert.match(body.textContent, /允许的选项/);
change(field('quality'), 'high'); apply(); assert.equal(view.editor, null);
assert.equal(view.draft.entries.at(-1).values.output_compression, 50);
assert.equal(view.draft.entries.at(-1).values.quality, 'high'); view.destroy();
""")


def test_bridge_loading_failure_requires_reload_and_destroy_ignore_late_results():
    _run(r"""
let view = create(); available = false; await view.open(); assert.equal(calls.length, 0); assert.equal(view.saveButton.disabled, true);
available = true; get = async () => { throw Error('private-url'); }; await view.open();
assert.equal(view.saveButton.disabled, true); assert.match(view.status.textContent, /加载失败/); assert.doesNotMatch(root.textContent, /private-url/);
const pending = deferred(); get = () => pending.promise; const loading = view.open();
assert.equal(view.loading, true); view.destroy(); pending.resolve(server); await loading;
assert.equal(root.children.length, 0); assert.equal(view.draft, null);
get = async () => server; view = create(); await view.open();
edit(0); input(field('model'), 'late-save'); apply(); const saving = deferred(); post = () => saving.promise;
action('save').click(); view.destroy(); saving.resolve({...server, revision: 'late'}); await tick();
assert.equal(view.draft, null); assert.equal(root.children.length, 0); assert.equal(saved, 0);
server.requires_reload = true; view = create(); await view.open();
assert.equal(view.saveButton.disabled, true); assert.equal(root.querySelector('fieldset').disabled, true);
action('edit', 0).click(); assert.equal(view.editor, undefined); view.destroy();
server.requires_reload = false; view = create(); await view.open(); edit(0);
view.destroy(); assert.equal(modal.options, null); assert.equal(body.children.length, 0);
assert.equal([...body.handlers.values()].reduce((total, handlers) => total + handlers.size, 0), 0);
""")
