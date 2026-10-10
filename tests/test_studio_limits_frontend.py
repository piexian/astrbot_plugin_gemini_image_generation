"""Exercise the real Studio limits controller with a small bubbling DOM/bridge VM."""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

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
