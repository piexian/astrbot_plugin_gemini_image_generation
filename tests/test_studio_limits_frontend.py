"""Exercise the real Studio limits controller with a small bubbling DOM/bridge VM."""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "pages/studio/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "pages/studio/index.html").read_text(encoding="utf-8")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"meta", "link", "input", "img", "br", "hr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1]["children"].append(data)


DOM = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.attributes = {}; this.dataset = {}; this.handlers = new Map();
    this.style = {}; this.value = ''; this.checked = false; this._text = '';
    this.className = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        if (force) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      add: name => this.classList.toggle(name, true),
      remove: name => this.classList.toggle(name, false)
    };
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value ?? '');
    if (key === 'value') this.value = String(value);
    if (key === 'class') this.className = String(value);
    if (key.startsWith('data-')) this.dataset[this.dataKey(key)] = String(value ?? '');
    if (key === 'hidden') this.hidden = true;
  }
  dataKey(key) { return key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
  getAttribute(key) { return key.startsWith('data-') ? this.dataset[this.dataKey(key)] : this.attributes[key]; }
  toggleAttribute(key, force) { if (force) this.setAttribute(key, ''); else delete this.attributes[key]; }
  get disabled() { return Object.hasOwn(this.attributes, 'disabled'); }
  set disabled(value) { this.toggleAttribute('disabled', value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  set innerHTML(value) {
    // Only the existing trusted icon path may create markup.
    assert.match(value, /^<svg /); this._svg = value;
  }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = []; this._text = ''; children.forEach(child => this.appendChild(child));
  }
  matches(selector) {
    const tag = selector.match(/^[a-z]+/);
    if (tag && this.tagName !== tag[0]) return false;
    const id = selector.match(/^#([\w-]+)/);
    if (id && this.attributes.id !== id[1]) return false;
    const attrs = [...selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
    return attrs.every(([, key, value]) => this.getAttribute(key) !== undefined
      && (value === undefined || this.getAttribute(key) === value));
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [ ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector); }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  addEventListener(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.handlers.get(type)?.delete(fn); }
  dispatch(type, extra = {}) {
    const event = {type, target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...extra};
    for (let node = this; node; node = node.parentNode) {
      for (const fn of [...(node.handlers.get(type) || [])]) fn(event);
    }
    return event;
  }
  click() {
    if (this.disabled || this.closest('fieldset')?.disabled) return;
    if (this.tagName === 'input' && this.attributes.type === 'checkbox') {
      this.checked = !this.checked; this.dispatch('input'); this.dispatch('change');
    }
    this.dispatch('click');
    if (this.tagName === 'button' && this.attributes.type === 'submit') this.closest('form')?.dispatch('submit');
  }
  focus() { document.activeElement = this; }
}
function build(value) {
  if (typeof value === 'string') { const n = new Node('#text'); n.textContent = value; return n; }
  const node = new Node(value.tag);
  for (const [key, val] of Object.entries(value.attrs)) node.setAttribute(key, val);
  value.children.forEach(child => node.appendChild(build(child)));
  return node;
}
const page = build(PAGE);
const document = {
  createElement: tag => new Node(tag), createTextNode: value => build(String(value)),
  getElementById: id => page.querySelector('#' + id), activeElement: null
};
const window = new Node('window');
Object.defineProperty(window, 'localStorage', {get() { throw Error('Limits must not persist config in localStorage'); }});
const calls = [];
const rate = () => ({enabled: false, period_seconds: 60, max_requests: 10});
const rule = (extra = {}) => ({...rate(), rule_name: '测试规则', umos: [], ...extra});
const response = (rules = []) => ({revision: 'r1', limits: {
  group_limit_mode: 'none', group_limit_list: [],
  global_rate_limit: rate(), default_rate_limit: rate(), rate_limit_rules: rules,
  group_whitelist: ['must-not-post'], providers: ['must-not-post']
}, migration: {pending: false, message: '', cooldown_until: 0}});
let server = response(), available = true;
let get = async route => route === 'webui/limits' ? server : ({sessions: [], total: 0, page: 1, page_size: 20, available: true, warning: ''});
let post = async (route, body) => ({...server, revision: 'r2', limits: body.limits});
const BridgeClient = {
  isAvailable: () => available,
  get: async (route, params) => { calls.push({method: 'get', route, params}); return get(route, params); },
  post: async (route, body) => { calls.push({method: 'post', route, body}); return post(route, body); }
};
let confirms = 0, confirmation = false;
const Modal = {confirm: async () => { confirms++; return confirmation; }};
const sandbox = {Node, document, window, BridgeClient, Modal, console: {warn() {}},
  Store: class {}, SSEController: class {}, StopwatchTimer: class {}};
vm.createContext(sandbox);
vm.runInContext(SOURCE + '\nthis.LimitsView = LimitsView; this.StudioApp = StudioApp;', sandbox);
const {LimitsView, StudioApp} = sandbox;
const byId = id => document.getElementById(id);
const tick = () => new Promise(resolve => setImmediate(resolve));
const action = (name, index = '') => byId('limits-editor').querySelector(`[data-limits-action="${name}"][data-index="${index}"]`);
const field = (scope, name) => byId('limits-editor').querySelector(`[data-scope="${scope}"][data-field="${name}"]`);
const input = (node, value) => { node.value = value; node.dispatch('input'); };
const plain = value => JSON.parse(JSON.stringify(value));
const posts = () => calls.filter(call => call.method === 'post');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
"""


def _run(body: str) -> None:
    parser = _PageParser()
    parser.feed(HTML)
    source = APP[: APP.index("const ImageLoader = {")]
    source += APP[APP.index("class LimitsView {") : APP.index("// 启动应用")]
    script = "const PAGE = " + json.dumps(parser.root, ensure_ascii=False) + ";\n"
    script += "const SOURCE = " + json.dumps(source, ensure_ascii=False) + ";\n"
    script += DOM
    script += "\n(async () => {\n" + body
    script += "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
    result = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_lazy_tab_keyboard_and_explicit_scoped_save():
    _run(r"""
const app = new StudioApp();
app.limits = new LimitsView(); app.gallery = {fetchGallery() {}};
app.initNavTabs();
assert.equal(calls.length, 0);
assert.equal(byId('btn-save-limits').disabled, true);
byId('tab-btn-workbench').dispatch('keydown', {key: 'End'});
assert.equal(byId('panel-providers').hidden, false);
byId('tab-btn-providers').dispatch('keydown', {key: 'ArrowLeft'});
await tick();
assert.equal(byId('panel-limits').hidden, false);
assert.equal(byId('tab-btn-limits').getAttribute('aria-selected'), 'true');
assert.equal(document.activeElement, byId('tab-btn-limits'));
assert.equal(calls.length, 1);
byId('tab-btn-limits').dispatch('keydown', {key: 'ArrowRight'});
assert.equal(byId('panel-providers').hidden, false);
byId('tab-btn-providers').dispatch('keydown', {key: 'ArrowRight'});
assert.equal(byId('panel-workbench').hidden, false);
byId('tab-btn-limits').click(); await tick();
assert.equal(calls.length, 1);
field('global_rate_limit', 'enabled').click();
input(field('global_rate_limit', 'period_seconds'), '120');
assert.equal(app.limits.dirty, true);
assert.equal(posts().length, 0);
assert.match(byId('limits-dirty').textContent, /未保存/);
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 1);
assert.equal(posts()[0].body.revision, 'r1');
assert.deepEqual(Object.keys(posts()[0].body.limits).sort(), ['default_rate_limit', 'global_rate_limit', 'group_limit_list', 'group_limit_mode', 'rate_limit_rules']);
assert.equal(posts()[0].body.limits.global_rate_limit.period_seconds, 120);
assert.equal(app.limits.revision, 'r2');
assert.equal(app.limits.dirty, false);
input(field('global_rate_limit', 'period_seconds'), '121');
input(field('global_rate_limit', 'period_seconds'), '120');
assert.equal(app.limits.dirty, false);
app.limits.destroy(); app.navCleanups.forEach(fn => fn());
""")


def test_migration_requires_selection_and_explicit_confirmation_and_keeps_order():
    _run(r"""
server = response([rule({enabled: true, group_ids: ['123']}), rule({rule_name: 'second'})]);
server.migration = {pending: true, message: '聊天请求暂停', cooldown_until: Date.now() / 1000 + 60};
const view = new LimitsView(); await view.open();
assert.match(byId('limits-migration').textContent, /暂停.*冷却至/);
assert.match(byId('limits-editor').textContent, /所有会话分别计数，不是全局共享/);
assert.equal(action('migrate-rule', 0).disabled, true);
input(field('0', 'rule_name'), 'renamed');
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 0);
assert.match(byId('limits-status').textContent, /尚未迁移/);
action('choose-sessions', 0).click(); await tick();
input(view.picker.manual, 'qq:GroupMessage:123:thread');
action('paste-umos', 0).click();
assert.deepEqual(plain(view.draft.rate_limit_rules[0].group_ids), ['123']);
assert.equal(action('migrate-rule', 0).disabled, false);
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 0);
action('migrate-rule', 0).click();
assert.deepEqual(plain(view.draft.rate_limit_rules[0].group_ids), []);
action('down-rule', 0).click();
assert.equal(view.draft.rate_limit_rules[1].rule_name, 'renamed');
action('up-rule', 1).click();
assert.equal(view.draft.rate_limit_rules[0].rule_name, 'renamed');
action('add-rule').click(); assert.equal(view.draft.rate_limit_rules.length, 3);
action('delete-rule', 2).click(); assert.equal(view.draft.rate_limit_rules.length, 2);
byId('btn-save-limits').click(); await tick();
assert.deepEqual(plain(posts()[0].body.limits.rate_limit_rules[0].group_ids), []);
view.destroy();
""")


def test_disabled_legacy_rule_preserves_group_ids_on_save():
    _run(r"""
server = response([rule({enabled: true, group_ids: ['legacy']})]);
const view = new LimitsView(); await view.open();
field('0', 'enabled').click();
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 1);
assert.deepEqual(plain(posts()[0].body.limits.rate_limit_rules[0].group_ids), ['legacy']);
assert.match(byId('limits-editor').textContent, /旧群号待迁移/);
view.destroy();
""")


def test_session_events_search_paging_manual_removal_and_safe_rendering():
    _run(r"""
const hostile = '<img src=x onerror=alert(1)>';
server = response([rule({rule_name: hostile})]);
get = async (route, params) => {
  if (route === 'webui/limits') return server;
  const n = params.page;
  return {sessions: [{umo: `qq:GroupMessage:${n}:thread`, display_name: hostile,
    platform: 'qq', message_type: 'group', session_id: `${n}:thread`}], total: 40, page: n, page_size: 20, available: true, warning: hostile};
};
const view = new LimitsView(); await view.open();
assert.equal(field('0', 'rule_name').value, hostile);
action('choose-sessions', 0).click(); await tick();
let picker = view.picker;
assert.match(picker.results.textContent, /<img src=x onerror=alert\(1\)>/);
assert.equal(picker.results.querySelectorAll('img').length, 0);
assert.match(picker.results.textContent, /qq:GroupMessage:1:thread/);
picker.results.querySelector('input').click();
action('next-sessions', 0).click(); await tick();
picker.results.querySelector('input').click();
assert.deepEqual(plain(view.draft.rate_limit_rules[0].umos), ['qq:GroupMessage:1:thread', 'qq:GroupMessage:2:thread']);
input(picker.search, 'hello'); input(picker.platform, 'qq'); picker.type.value = 'group';
picker.type.dispatch('change'); await tick();
assert.deepEqual(plain(calls.at(-1).params), {page: 1, page_size: 20, search: 'hello', message_type: 'group', platform: 'qq'});
assert.equal(picker.results.querySelector('input').checked, true);
const form = picker.search.closest('form');
assert.equal(form.dispatch('submit').defaultPrevented, true); await tick();
input(picker.manual, 'qq:FriendMessage:5\nqq:GroupMessage:1:thread');
action('paste-umos', 0).click();
assert.equal(view.draft.rate_limit_rules[0].umos.length, 3);
input(picker.manual, '123456'); action('paste-umos', 0).click();
assert.equal(view.draft.rate_limit_rules[0].umos.length, 3);
assert.match(picker.feedback.textContent, /格式无效/);
action('remove-umo', 0).click();
assert.equal(view.draft.rate_limit_rules[0].umos.length, 2);
assert.equal(picker.results.querySelector('input').checked, false);
get = async () => ({sessions: [], available: false, warning: hostile, total: 0, page: 1, page_size: 20});
form.dispatch('submit'); await tick();
assert.match(picker.resultStatus.textContent, /暂不可用/);
assert.equal(view.draft.rate_limit_rules[0].umos.length, 2);
input(picker.manual, 'qq:OtherMessage:7'); action('paste-umos', 0).click();
assert.equal(view.draft.rate_limit_rules[0].umos.length, 3);
view.destroy();
""")


@pytest.mark.parametrize(
    "save_error",
    [
        {"status": 409, "message": "stale"},
        {"message": "限流配置已更新，请重新加载后再保存"},
        {"status_code": 409},
    ],
)
def test_409_keeps_draft_and_revision_until_confirmed_reload(save_error):
    _run(
        "const saveError = "
        + json.dumps(save_error)
        + ";\n"
        + r"""
const view = new LimitsView(); await view.open();
input(field('default_rate_limit', 'max_requests'), '42');
post = async () => { throw saveError; };
byId('btn-save-limits').click(); await tick();
assert.equal(view.revision, 'r1'); assert.equal(view.dirty, true);
assert.equal(view.draft.default_rate_limit.max_requests, 42);
assert.match(byId('limits-status').textContent, /409.*草稿完整保留/);
byId('btn-save-limits').click(); await tick(); assert.equal(posts().length, 1);
byId('btn-reload-limits').click(); await tick();
assert.equal(confirms, 1); assert.equal(view.draft.default_rate_limit.max_requests, 42);
confirmation = true; server.revision = 'r9';
byId('btn-reload-limits').click(); await tick();
assert.equal(confirms, 2); assert.equal(view.revision, 'r9'); assert.equal(view.dirty, false);
assert.equal(view.draft.default_rate_limit.max_requests, 10);
view.destroy();
"""
    )


def test_loading_sdk_failure_and_destroy_do_not_enable_save_or_accept_late_data():
    _run(r"""
const view = new LimitsView();
available = false; await view.open();
assert.equal(calls.length, 0); assert.equal(view.saveButton.disabled, true);
available = true; get = async () => { throw Error('offline'); }; await view.load();
assert.equal(view.saveButton.disabled, true); assert.match(view.status.textContent, /加载失败/);
const pending = deferred(); get = () => pending.promise;
const loading = view.load();
view.destroy(); pending.resolve(server); await loading;
assert.equal(view.draft, null); assert.equal(view.saveButton.disabled, true);
for (const handlers of view.root.handlers.values()) assert.equal(handlers.size, 0);
assert.equal(window.handlers.get('beforeunload').size, 0);
""")


def test_save_disables_edits_and_ignores_response_after_destroy():
    _run(r"""
const view = new LimitsView(); await view.open();
input(field('global_rate_limit', 'max_requests'), '15');
available = false;
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 0); assert.equal(view.saveButton.disabled, true);
available = true; view.updateState();
const pending = deferred(); post = () => pending.promise;
byId('btn-save-limits').click();
assert.equal(view.editor.disabled, true);
field('global_rate_limit', 'enabled').click();
assert.equal(view.draft.global_rate_limit.enabled, false);
view.destroy(); pending.resolve({...server, revision: 'late'}); await tick();
assert.equal(view.revision, 'r1');
assert.equal(view.draft.global_rate_limit.max_requests, 15);
assert.equal(view.saveButton.disabled, true);
""")


def test_stale_session_search_and_closed_picker_cannot_write_back():
    _run(r"""
server = response([rule(), rule()]);
const view = new LimitsView(); await view.open();
const first = deferred(), second = deferred();
let count = 0; get = () => (++count === 1 ? first.promise : second.promise);
action('choose-sessions', 0).click();
const oldPicker = view.picker;
input(oldPicker.search, 'new'); oldPicker.search.closest('form').dispatch('submit');
const sessions = label => ({sessions: [{umo: `qq:GroupMessage:${label}`, display_name: label, platform: 'qq', message_type: 'group', session_id: label}], available: true, total: 1, page: 1, page_size: 20});
second.resolve(sessions('new')); await tick();
first.resolve(sessions('old')); await tick();
assert.match(oldPicker.results.textContent, /new/); assert.doesNotMatch(oldPicker.results.textContent, /old/);
const late = deferred(); get = () => late.promise;
oldPicker.search.closest('form').dispatch('submit');
action('close-picker', 0).click();
late.resolve(sessions('late')); await tick();
assert.equal(view.picker, null); assert.doesNotMatch(oldPicker.results.textContent, /late/);
assert.equal(oldPicker.host.children.length, 0);
view.destroy();
""")


def test_validation_bounds_and_cancelled_unload_preserve_live_editor():
    _run(r"""
server = response([rule()]);
const app = new StudioApp(); const view = new LimitsView(); app.limits = view;
await view.open();
for (const [name, invalid] of [['period_seconds', '0'], ['period_seconds', '604801'], ['period_seconds', '1.5'], ['max_requests', '10001'], ['max_requests', '']]) {
  input(field('global_rate_limit', name), invalid);
  byId('btn-save-limits').click(); await tick();
  assert.equal(posts().length, 0);
  input(field('global_rate_limit', name), '1');
}
input(field('global_rate_limit', 'period_seconds'), '604800');
input(field('global_rate_limit', 'max_requests'), '10000');
assert.doesNotThrow(() => view.validate());
assert.equal(LimitsView.validUmo('p:OtherMessage:a:b'), true);
for (const umo of ['123', 'p:GroupMessage:', 'p:GroupMessage:   ', 'p:Unknown:1', ':FriendMessage:1', 'p:GroupMessage:a\u0001b']) assert.equal(LimitsView.validUmo(umo), false);
input(field('0', 'rule_name'), 'x'.repeat(101)); assert.throws(() => view.validate(), /100/);
input(field('0', 'rule_name'), ''); assert.throws(() => view.validate(), /名称/);
input(field('0', 'rule_name'), 'valid');
const prefix = 'p:GroupMessage:';
assert.equal(LimitsView.validUmo(prefix + 'x'.repeat(1024 - prefix.length)), true);
assert.equal(LimitsView.validUmo(prefix + 'x'.repeat(1025 - prefix.length)), false);
assert.equal(LimitsView.validUmo(prefix + '\u{1F600}'.repeat(1024 - prefix.length)), true);
action('choose-sessions', 0).click(); await tick();
input(view.picker.manual, Array.from({length: 500}, (_, i) => `p:GroupMessage:${i}`).join('\n'));
action('paste-umos', 0).click(); assert.equal(view.draft.rate_limit_rules[0].umos.length, 500);
input(view.picker.manual, 'p:FriendMessage:extra'); action('paste-umos', 0).click();
assert.equal(view.draft.rate_limit_rules[0].umos.length, 500);
const event = window.dispatch('beforeunload'); assert.equal(event.defaultPrevented, true);
let destroyed = false; app.destroy = async () => { destroyed = true; };
app.handleUnload({type: 'beforeunload', preventDefault() {}}); assert.equal(destroyed, false);
assert.equal(view.editor.disabled, false);
app.handleUnload({type: 'pagehide'}); assert.equal(destroyed, true);
view.draft.rate_limit_rules = Array.from({length: 100}, () => rule());
view.render(); assert.equal(action('add-rule').disabled, true);
assert.doesNotThrow(() => view.validate());
view.draft.rate_limit_rules.push(rule()); assert.throws(() => view.validate(), /100/);
view.destroy();
""")
