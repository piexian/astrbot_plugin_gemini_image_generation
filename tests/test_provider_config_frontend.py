"""Run the actual isolated provider controller and SafeDOM with bubbling DOM/bridge."""

import json
import subprocess
from pathlib import Path

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
const listedKeys = () => {
  if (keyAction('toggle-keys')?.textContent === '显示') keyAction('toggle-keys').click();
  return body.querySelectorAll('[data-pc-action="edit-key"]').map(node => node.textContent);
};
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


def test_revision_conflict_requires_explicit_confirmed_reload():
    error = {"status": 409, "data": {"reason": "revision"}}
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
