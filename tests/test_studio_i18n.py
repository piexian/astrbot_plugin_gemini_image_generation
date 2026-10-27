"""Studio 前端 i18n 一致性守门：key 全覆盖、占位符一致、无裸中文显示串。"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "views/studio/app.js").read_text(encoding="utf-8")
PC = (ROOT / "views/studio/provider-config.js").read_text(encoding="utf-8")
HTML = (ROOT / "views/studio/index.html").read_text(encoding="utf-8")
I18N_DIR = ROOT / ".astrbot-plugin/i18n"

ZH = re.compile(r"[\u4e00-\u9fff]")
SKIP_LINE = re.compile(r"console\.(log|warn|error|info|debug)")
COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*)")
PH = re.compile(r"\{v\d+\}")
LANGS = ["en-US", "ja-JP", "ru-RU"]


def _collect_keys():
    keys = set()
    for m in re.finditer(r"\bt\((['\"])(.+?)\1", APP):
        keys.add(m.group(2))
    for m in re.finditer(r"this\.t\((['\"])(.+?)\1", PC):
        keys.add(m.group(2))
    keys |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', HTML))
    return keys


def _load_strings(locale):
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data


def test_i18n_files_exist_for_all_dashboard_locales():
    for locale in ["zh-CN", *LANGS]:
        assert (I18N_DIR / f"{locale}.json").is_file(), locale


def test_translation_keys_cover_all_source_strings():
    source = _collect_keys()
    assert source, "未收集到任何 key"
    for locale in LANGS:
        table = _load_strings(locale)["strings"]
        missing = source - set(table)
        extra = set(table) - source
        assert not missing, f"{locale} 缺翻译: {sorted(missing)[:5]}"
        assert not extra, f"{locale} 多余翻译: {sorted(extra)[:5]}"


def test_translation_placeholders_match_source():
    for locale in LANGS:
        table = _load_strings(locale)["strings"]
        for key, value in table.items():
            assert PH.findall(key) == PH.findall(value), (
                f"{locale} 占位符不一致: {key!r} -> {value!r}"
            )


def test_view_title_present_in_every_locale():
    for locale in ["zh-CN", *LANGS]:
        data = _load_strings(locale)
        title = data["views"]["studio"]["title"]
        assert isinstance(title, str) and title.strip(), locale


def test_first_render_waits_on_real_bridge_readiness():
    """首帧等待必须调用 BridgeClient 真实方法：类只有 init()，无 ready()。"""
    assert "BridgeClient.ready(" not in APP, "BridgeClient 无 ready() 方法"
    assert "Promise.race([BridgeClient.init()" in APP


def test_no_bare_chinese_display_strings_in_js():
    leftovers = []
    for fname, src in (("app.js", APP), ("provider-config.js", PC)):
        for i, line in enumerate(src.split("\n"), 1):
            if SKIP_LINE.search(line) or COMMENT_LINE.match(line):
                continue
            for m in re.finditer(r"(['\"])([^'\"\n]*[\u4e00-\u9fff][^'\"\n]*)\1", line):
                prefix = line[: m.start()].rstrip()
                if prefix.endswith(("t(", "this.t(")):
                    continue
                leftovers.append((fname, i, m.group(2)[:40]))
    assert not leftovers, f"存在未包 t() 的中文显示串: {leftovers[:8]}"


def test_no_bare_chinese_text_nodes_in_html():
    leftovers = []
    for i, line in enumerate(HTML.split("\n"), 1):
        if (
            not ZH.search(line)
            or line.strip().startswith("<!--")
            or "<!DOCTYPE" in line
        ):
            continue
        for m in re.finditer(r">([^<>]*[\u4e00-\u9fff][^<>]*)<", line):
            window = line[max(0, m.start() - 160) : m.start()]
            if "data-i18n" in window:
                continue
            leftovers.append((i, m.group(1)[:40]))
    assert not leftovers, f"存在未标注 data-i18n 的中文文本节点: {leftovers[:8]}"


def test_i18n_module_lookup_fallback_and_locale_reload():
    """node VM 直接执行 app.js 的 I18n 模块：查表、插值、回退与语言切换 reload。"""
    start = APP.index("const I18n = {")
    end = APP.index("const t = (key, vars) => I18n.t(key, vars);")
    end = APP.index("\n", end) + 1
    module_src = json.dumps(APP[start:end])
    template = r"""
const vm = require('node:vm');
const en = {strings: {'取消': 'Cancel', '张数: {v0} / {v1}': 'Count: {v0} / {v1}'}};
let ctx = {locale: 'en-US', i18n: {'en-US': en}};
let handler = null;
const BridgeClient = {
  getContext: () => ctx,
  onContext: h => { handler = h; return () => {}; },
};
const reloaded = [];
const applied = [];
const document = {
  documentElement: {setAttribute: (k, v) => applied.push([k, v]), lang: ''},
  querySelectorAll: () => [],
};
const sandbox = {BridgeClient, document, location: {reload: () => reloaded.push(1)},
  console, setTimeout: () => {}};
vm.createContext(sandbox);
vm.runInContext(__MODULE__ + "\nthis.I18n = I18n; this.t = t;", sandbox);
const I18n = sandbox.I18n; const t = sandbox.t;
I18n.sync(ctx);
if (reloaded.length) throw Error('unexpected reload');
if (document.documentElement.lang !== 'en-US') throw Error('lang not applied');
if (t('取消') !== 'Cancel') throw Error('lookup failed: ' + t('取消'));
if (t('张数: {v0} / {v1}', {v0: 2, v1: 4}) !== 'Count: 2 / 4') throw Error('format failed');
if (t('不存在的串') !== '不存在的串') throw Error('fallback failed');
I18n.locale = 'fr-FR';
if (t('取消') !== 'Cancel') throw Error('locale fallback failed');
I18n.sync({locale: 'ja-JP', i18n: {}});
if (reloaded.length !== 1) throw Error('locale change did not reload');
I18n.sync({locale: 'ja-JP', i18n: {}, isDark: true});
if (reloaded.length !== 1) throw Error('theme change should not reload');
"""
    script = template.replace("__MODULE__", module_src)
    result = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
