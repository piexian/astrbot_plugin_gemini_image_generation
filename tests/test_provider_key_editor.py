"""Key-manager subviews exercise real controller actions, not provider/network APIs."""

import json

import pytest
from test_provider_config_frontend import TEMPLATES, _run


@pytest.mark.parametrize("keys", [[], ["fake-single"], ["fake-first", "fake-second"]])
def test_compact_plain_single_or_multi_summary_opens_same_modal(keys):
    _run(
        "const initial = "
        + json.dumps(keys)
        + ";\n"
        + r"""
server.entries[0].values.api_keys = initial;
const view = create(); await view.open(); edit(0);
const options = modal.options, editor = view.editor;
assert.equal(body.querySelectorAll('textarea').filter(node => node.dataset.pcField === 'api_keys').length, 0);
assert.equal(mode('api_keys'), null);
const summary = body.querySelector('[data-pc-key-summary]'); assert.ok(summary);
if (initial.length <= 1) {
  assert.equal(field('api_keys').tagName, 'input');
  assert.equal(field('api_keys').getAttribute('type'), 'text');
  assert.equal(field('api_keys').value, initial[0] ? '••••••••' : '');
  assert.equal(field('api_keys').disabled, !!initial[0]);
  if (initial[0]) keyAction('toggle-keys').click();
  assert.equal(field('api_keys').value, initial[0] || '');
  assert.equal(field('api_keys').disabled, false);
  assert.equal(keyAction('manage-keys').textContent, '添加更多');
} else {
  assert.equal(field('api_keys'), null);
  assert.match(summary.textContent, /••••••••\+1/);
  keyAction('toggle-keys').click();
  // renderEditorBody 重建了摘要节点，断言必须重新查询。
  assert.match(body.querySelector('[data-pc-key-summary]').textContent, /fake-first\+1/);
  assert.equal(keyAction('toggle-keys').textContent, '隐藏');
  assert.equal(keyAction('manage-keys').textContent, '管理 Key');
}
manageKeys();
assert.equal(modal.options, options); assert.equal(view.editor, editor); assert.equal(confirms, 0);
assert.equal(body.querySelectorAll('[data-pc-key-manager]').length, 1);
assert.deepEqual(listedKeys(), initial); assert.equal(field('model'), null);
assert.equal(action('apply-edit', null, footer), null);
keyAction('cancel-keys').click();
assert.equal(modal.options, options); assert.ok(field('model'));
assert.equal(document.activeElement, keyAction('manage-keys'));
assert.equal(view.dirty, false); assert.equal(posts().length, 0); view.destroy();
"""
    )


def test_single_key_input_updates_only_editor_and_enforces_length_at_apply():
    _run(r"""
server.entries[0].values.api_keys = ['fake-single'];
const view = create(); await view.open(); edit(0); keyAction('toggle-keys').click();
input(field('api_keys'), ' fake-edited ');
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-edited']);
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['fake-single']);
assert.equal(view.dirty, false); assert.equal(posts().length, 0);
manageKeys(); assert.deepEqual(listedKeys(), ['fake-edited']); keyAction('cancel-keys').click();
input(field('api_keys'), 'x'.repeat(8193)); apply();
assert.ok(view.editor); assert.match(view.editor.feedback.textContent, /8192/);
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['fake-single']);
input(field('api_keys'), 'fake-' + 'x'.repeat(8187)); apply(); action('save').click(); await tick();
assert.equal(posts()[0].payload.entries[0].values.api_keys[0].length, 8192);
assert.equal(posts()[0].payload.entries[0].secret_actions.api_keys, undefined); view.destroy();
""")


@pytest.mark.parametrize("visible_flag", [False, True])
def test_missing_key_values_never_become_empty_replacements(visible_flag):
    _run(
        "server.key_values_visible = "
        + json.dumps(visible_flag)
        + ";\n"
        + r"""
delete server.entries[0].values.api_keys;
const view = create(); await view.open(); edit(0);
assert.equal(field('api_keys').disabled, true); assert.equal(keyAction('manage-keys').disabled, true);
assert.match(body.textContent, /旧后台未返回已有 Key/); assert.equal(mode('api_keys'), null);
keyAction('manage-keys').click(); assert.equal(view.editor.keys, undefined);
// Even a synthetic input dispatched on the disabled field cannot overwrite hidden keys.
input(field('api_keys'), 'fake-not-allowed');
field('api_keys').dispatch('paste', {clipboardData: {getData: () => 'fake-one\nfake-two'}});
assert.equal(view.editor.keys, undefined); assert.equal(view.editor.entry.values.api_keys, undefined);
input(field('model'), 'fake-new-model'); apply(); action('save').click(); await tick();
const entry = posts()[0].payload.entries[0];
assert.equal(entry.values.api_keys, undefined); assert.equal(entry.secret_actions.api_keys, undefined);
assert.equal(entry.values.model, 'fake-new-model'); view.destroy();
"""
    )


def test_row_edit_cancel_delete_empty_rejection_and_ordered_dedupe():
    _run(r"""
const view = create(); await view.open(); edit(0); manageKeys();
keyAction('edit-key', 0).click();
assert.equal(keyInput('editValue').value, 'fake-old-one');
assert.equal(keyInput('newValue').disabled, true); assert.equal(keyAction('batch-keys').disabled, true);
assert.equal(keyAction('remove-key', 1).disabled, true); assert.equal(keyAction('edit-key', 1).disabled, true);
input(keyInput('editValue'), 'fake-cancelled'); keyAction('cancel-key-edit', 0).click();
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two']);
keyAction('edit-key', 0).click(); input(keyInput('editValue'), '  '); keyAction('save-key', 0).click();
assert.match(body.querySelector('[data-pc-key-status]').textContent, /不能为空/);
assert.ok(keyInput('editValue')); assert.equal(keyAction('apply-keys').disabled, true);
input(keyInput('editValue'), ' fake-renamed '); keyAction('save-key', 0).click();
assert.deepEqual(listedKeys(), ['fake-renamed', 'fake-old-two']);
input(keyInput('newValue'), ' fake-renamed '); keyAction('add-key').click();
assert.deepEqual(listedKeys(), ['fake-renamed', 'fake-old-two']);
assert.match(body.querySelector('[data-pc-key-status]').textContent, /已存在/);
keyAction('edit-key', 1).click(); input(keyInput('editValue'), 'fake-renamed'); keyAction('save-key', 1).click();
assert.deepEqual(listedKeys(), ['fake-renamed']);
keyAction('remove-key', 0).click(); assert.deepEqual(listedKeys(), []); assert.match(body.textContent, /暂无 Key/);
keyAction('apply-keys').click(); assert.equal(field('api_keys').value, '');
assert.deepEqual(plain(view.editor.entry.values.api_keys), []);
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['fake-old-one', 'fake-old-two']);
apply(); action('save').click(); await tick();
assert.deepEqual(plain(posts()[0].payload.entries[0].values.api_keys), []);
assert.equal(posts()[0].payload.entries[0].secret_actions.api_keys, undefined); view.destroy();
""")


def test_batch_confirm_cancel_dedupe_and_newline_enter_do_not_import():
    _run(r"""
const view = create(); await view.open(); edit(0); manageKeys();
input(keyInput('newValue'), 'fake-pending'); keyAction('batch-keys').click();
assert.equal(keyInput('batchValue').tagName, 'textarea'); assert.equal(keyInput('batchValue').value, '');
assert.equal(action('apply-keys', null, footer), null);
input(keyInput('batchValue'), 'fake-cancelled\nfake-other'); keyAction('cancel-key-batch').click();
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two']); assert.equal(keyInput('newValue').value, 'fake-pending');
assert.equal(keyAction('apply-keys').disabled, true);
keyAction('batch-keys').click(); assert.equal(keyInput('batchValue').value, '');
assert.equal(keyAction('import-keys').disabled, true);
input(keyInput('batchValue'), '  \n '); assert.equal(keyAction('import-keys').disabled, true);
input(keyInput('batchValue'), ' fake-old-two\r\n fake-batch \n\nfake-batch\nfake-tail ');
const enter = keyInput('batchValue').dispatch('keydown', {key: 'Enter'});
assert.equal(enter.defaultPrevented, false); assert.ok(keyInput('batchValue'));
assert.deepEqual(plain(view.editor.keys.items), ['fake-old-one', 'fake-old-two']);
keyAction('import-keys').click();
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two', 'fake-batch', 'fake-tail']);
assert.match(body.querySelector('[data-pc-key-status]').textContent, /已追加 2/);
assert.equal(keyInput('newValue').value, 'fake-pending'); assert.equal(keyAction('apply-keys').disabled, true);
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-old-one', 'fake-old-two']);
keyAction('cancel-keys').click(); manageKeys();
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two']); view.destroy();
""")


def test_three_draft_layers_and_modal_close_discard_all_editor_changes():
    _run(r"""
const view = create(); await view.open();
const original = plain(view.draft.entries[0]);
edit(0); input(field('model'), 'fake-entry-model'); manageKeys();
input(keyInput('newValue'), 'fake-managed'); keyAction('add-key').click();
assert.deepEqual(plain(view.editor.entry.values.api_keys), original.values.api_keys);
assert.deepEqual(plain(view.draft.entries[0]), original); assert.equal(posts().length, 0);
keyAction('cancel-keys').click(); assert.equal(field('model').value, 'fake-entry-model');
manageKeys(); assert.deepEqual(listedKeys(), original.values.api_keys);
input(keyInput('newValue'), 'fake-managed'); keyAction('add-key').click(); keyAction('apply-keys').click();
assert.deepEqual(plain(view.editor.entry.values.api_keys), [...original.values.api_keys, 'fake-managed']);
assert.deepEqual(plain(view.draft.entries[0]), original); assert.equal(posts().length, 0);
apply(); assert.equal(view.dirty, true); assert.equal(posts().length, 0);
const committedDraft = plain(view.draft.entries[0]);
edit(0); input(field('model'), 'fake-discarded-model'); manageKeys();
keyAction('remove-key', 0).click(); keyAction('apply-keys').click(); manageKeys();
input(keyInput('newValue'), 'fake-unfinished');
const closedEditor = view.editor; modal.close(false);
assert.equal(view.editor, null); assert.equal(closedEditor.entry, null); assert.equal(closedEditor.keys, null);
assert.deepEqual(plain(view.draft.entries[0]), committedDraft);
edit(0); assert.equal(field('model').value, 'fake-entry-model'); manageKeys();
assert.deepEqual(listedKeys(), committedDraft.values.api_keys); keyAction('cancel-keys').click();
action('cancel-edit', null, footer).click(); action('save').click(); await tick();
assert.equal(posts().length, 1); assert.equal(posts()[0].route, 'webui/providers');
assert.deepEqual(plain(posts()[0].payload.entries[0]), committedDraft); view.destroy();
""")


def test_unfinished_add_or_edit_cannot_apply_even_with_stale_buttons():
    _run(r"""
const view = create(); await view.open(); edit(0);
const staleApplyEditor = action('apply-edit', null, footer); manageKeys();
const staleFinish = keyAction('apply-keys'); input(keyInput('newValue'), 'fake-unfinished');
assert.equal(keyAction('apply-keys').disabled, true); keyAction('apply-keys').click();
staleFinish.click(); staleApplyEditor.click();
assert.ok(view.editor.keys); assert.equal(keyInput('newValue').value, 'fake-unfinished');
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-old-one', 'fake-old-two']);
assert.equal(posts().length, 0); assert.equal(view.dirty, false);
keyAction('add-key').click(); assert.equal(keyAction('apply-keys').disabled, false);
keyAction('edit-key', 0).click(); input(keyInput('editValue'), 'fake-edit-unfinished');
assert.equal(keyAction('apply-keys').disabled, true); keyAction('apply-keys').click();
assert.ok(keyInput('editValue')); keyAction('cancel-key-edit', 0).click();
keyAction('apply-keys').click(); assert.ok(field('model')); assert.equal(view.dirty, false); view.destroy();
""")


def test_enter_and_escape_layering_and_ime_composition_are_not_committed():
    _run(r"""
const view = create(); await view.open(); edit(0); manageKeys();
const options = modal.options;
input(keyInput('newValue'), 'fake-composed');
for (const key of ['Enter', 'Escape']) {
  const event = keyInput('newValue').dispatch('keydown', {key, isComposing: true});
  assert.equal(event.defaultPrevented, false); assert.equal(event.cancelBubble, false);
  assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two']); assert.ok(view.editor.keys);
}
let event = keyInput('newValue').dispatch('keydown', {key: 'Enter'});
assert.equal(event.defaultPrevented, true); assert.equal(event.cancelBubble, true);
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two', 'fake-composed']);
assert.equal(keyInput('newValue').value, ''); assert.equal(document.activeElement, keyInput('newValue'));
keyAction('edit-key', 2).click(); input(keyInput('editValue'), 'fake-renamed');
event = keyInput('editValue').dispatch('keydown', {key: 'Enter'});
assert.equal(event.defaultPrevented, true); assert.equal(event.cancelBubble, true);
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two', 'fake-renamed']);
keyAction('edit-key', 2).click(); input(keyInput('editValue'), 'fake-cancelled');
event = keyInput('editValue').dispatch('keydown', {key: 'Escape'});
assert.equal(event.defaultPrevented, true); assert.equal(event.cancelBubble, true);
assert.equal(keyInput('editValue'), null); assert.equal(modal.options, options);
assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two', 'fake-renamed']);
keyAction('batch-keys').click(); input(keyInput('batchValue'), 'fake-batch-cancelled');
event = keyInput('batchValue').dispatch('keydown', {key: 'Escape'});
assert.equal(event.cancelBubble, true); assert.equal(keyInput('batchValue'), null);
assert.ok(view.editor.keys); assert.equal(modal.options, options);
event = keyInput('newValue').dispatch('keydown', {key: 'Escape'});
assert.equal(event.cancelBubble, true); assert.equal(event.defaultPrevented, true);
assert.equal(view.editor.keys, null); assert.equal(modal.options, options);
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-old-one', 'fake-old-two']);
// Once back in the entry view, Escape is left for the host Modal handler.
event = field('model').dispatch('keydown', {key: 'Escape'});
assert.equal(event.cancelBubble, false); assert.equal(event.defaultPrevented, false);
modal.close(false); assert.equal(view.editor, null); view.destroy();
""")


def test_multiline_paste_from_single_key_opens_batch_without_overwriting():
    _run(r"""
for (const newline of ['\n', '\r\n', '\r']) {
const pasted = `fake-first${newline}fake-second`;
server.entries[0].values.api_keys = ['fake-single'];
const view = create(); await view.open(); edit(0);
// 每轮循环新建视图但 keysHidden 是视图级状态；默认即省略，先显式回到省略态。
if (keyAction('toggle-keys').textContent === '隐藏') keyAction('toggle-keys').click();
assert.equal(field('api_keys').value, '••••••••'); assert.equal(field('api_keys').readOnly, true);
assert.equal(keyAction('toggle-keys').textContent, '显示');
keyAction('toggle-keys').click();
assert.equal(field('api_keys').value, 'fake-single'); assert.equal(field('api_keys').readOnly, false);
const plainEvent = field('api_keys').dispatch('paste', {clipboardData: {getData: () => 'fake-plain'}});
assert.equal(plainEvent.defaultPrevented, false); assert.equal(view.editor.keys, undefined);
keyAction('toggle-keys').click();
assert.equal(keyAction('toggle-keys').textContent, '显示');
assert.equal(field('api_keys').readOnly, true); assert.equal(field('api_keys').value, '••••••••');
keyAction('toggle-keys').click();
const event = field('api_keys').dispatch('paste', {clipboardData: {getData: () => pasted}});
assert.equal(event.defaultPrevented, true); assert.equal(keyInput('batchValue').value, pasted);
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-single']);
keyAction('import-keys').click(); assert.deepEqual(listedKeys(), ['fake-single', 'fake-first', 'fake-second']);
keyAction('apply-keys').click(); assert.equal(field('api_keys'), null); view.destroy();
}
""")


def test_long_key_exact_limit_roundtrips_and_invalid_add_or_edit_preserves_list():
    _run(r"""
server.entries[0].values.api_keys = ['fake-' + 'x'.repeat(8187), 'fake-second'];
const original = [...server.entries[0].values.api_keys];
const view = create(); await view.open(); edit(0);
if (keyAction('toggle-keys').textContent === '隐藏') keyAction('toggle-keys').click();
const summary = body.querySelector('[data-pc-key-summary]');
assert.ok(summary.textContent.length < 100); assert.match(summary.textContent, /••••••••\+1/);
keyAction('toggle-keys').click(); assert.match(body.querySelector('[data-pc-key-summary]').textContent, /…\+1/);
manageKeys(); assert.deepEqual(listedKeys(), original);
assert.equal(keyAction('edit-key', 0).getAttribute('title'), original[0]);
input(keyInput('newValue'), 'x'.repeat(8193)); keyAction('add-key').click();
assert.match(body.querySelector('[data-pc-key-status]').textContent, /8192/);
assert.deepEqual(listedKeys(), original); assert.equal(keyInput('newValue').value.length, 8193);
assert.equal(keyAction('apply-keys').disabled, true); input(keyInput('newValue'), '');
keyAction('edit-key', 0).click(); assert.equal(keyInput('editValue').value, original[0]);
input(keyInput('editValue'), 'x'.repeat(8193)); keyAction('save-key', 0).click();
assert.ok(keyInput('editValue')); assert.deepEqual(plain(view.editor.keys.items), original);
input(keyInput('editValue'), original[0]); keyAction('save-key', 0).click(); keyAction('apply-keys').click();
input(field('model'), 'fake-force-save'); apply(); action('save').click(); await tick();
assert.deepEqual(plain(posts()[0].payload.entries[0].values.api_keys), original); view.destroy();
""")


def test_two_hundred_unique_keys_allowed_duplicates_do_not_consume_capacity():
    _run(r"""
server.entries[0].values.api_keys = Array.from({length: 199}, (_, i) => `fake-${i}`);
const view = create(); await view.open(); edit(0); manageKeys(); keyAction('batch-keys').click();
input(keyInput('batchValue'), 'fake-0\nfake-199\nfake-199\n'); keyAction('import-keys').click();
assert.equal(listedKeys().length, 200); assert.equal(listedKeys().at(-1), 'fake-199');
input(keyInput('newValue'), 'fake-0'); keyAction('add-key').click(); assert.equal(listedKeys().length, 200);
input(keyInput('newValue'), 'fake-overflow'); keyAction('add-key').click();
assert.equal(listedKeys().length, 200); assert.match(body.querySelector('[data-pc-key-status]').textContent, /最多 200/);
input(keyInput('newValue'), ''); keyAction('batch-keys').click(); input(keyInput('batchValue'), 'fake-overflow\nfake-0');
keyAction('import-keys').click(); assert.ok(keyInput('batchValue'));
assert.equal(view.editor.keys.items.length, 200); assert.equal(view.editor.entry.values.api_keys.length, 199);
keyAction('cancel-key-batch').click(); keyAction('apply-keys').click(); apply(); action('save').click(); await tick();
assert.equal(posts()[0].payload.entries[0].values.api_keys.length, 200); view.destroy();
""")


def test_chosen_model_and_all_fields_survive_management_and_late_catalog_is_dropped():
    _run(r"""
const view = create(); await view.open(); edit(0);
input(field('priority'), '7'); input(field('api_base'), 'https://fake-draft.invalid');
input(field('proxy'), 'http://fake-proxy.invalid'); change(field('aspect_ratio'), '16:9');
post = async () => ({models: [{id: 'fake-chosen', label: 'Fake chosen'}]});
action('fetch-models', null, body).click(); await tick(); action('choose-model', null, body).click();
const pending = deferred(); post = () => pending.promise;
action('fetch-models', null, body).click(); const count = posts().length;
manageKeys(); const pane = body.querySelector('[data-pc-key-manager]');
input(keyInput('newValue'), 'fake-kept-input');
pending.resolve({models: [{id: 'fake-late', label: 'Late result'}]}); await tick();
assert.equal(body.querySelector('[data-pc-key-manager]'), pane);
assert.equal(keyInput('newValue').value, 'fake-kept-input'); assert.equal(action('choose-model', null, body), null);
keyAction('add-key').click(); keyAction('apply-keys').click();
assert.equal(field('model').value, 'fake-chosen'); assert.equal(field('priority').value, '7');
assert.equal(field('api_base').value, 'https://fake-draft.invalid'); assert.equal(field('proxy').value, 'http://fake-proxy.invalid');
assert.equal(field('aspect_ratio').value, '16:9'); assert.doesNotMatch(body.textContent, /fake-late/);
assert.equal(action('choose-model', null, body), null); assert.equal(posts().length, count);
assert.equal(view.draft.entries[0].values.model, 'model-free-form');
apply(); assert.equal(view.draft.entries[0].values.model, 'fake-chosen');
assert.equal(view.draft.entries[0].values.priority, 7); view.destroy();
""")


@pytest.mark.parametrize(
    "provider",
    [
        name
        for name, template in TEMPLATES.items()
        if "endpoint_mode" in template["fields"]
    ],
)
def test_endpoint_mode_is_general_and_submits_without_rewriting_connection(provider):
    _run(
        "const provider = "
        + json.dumps(provider)
        + ";\n"
        + r"""
server.entries = [known('fake-endpoint-entry', provider)];
const original = plain(server.entries[0].values);
const view = create(); await view.open(); edit(0);
const wrap = body.querySelector('[data-pc-field-wrap="endpoint_mode"]');
assert.ok(wrap); assert.equal(wrap.hidden, false);
assert.equal(wrap.closest('[data-pc-group]').dataset.pcGroup, 'general');
assert.equal(wrap.closest('details'), undefined);
assert.equal(field('endpoint_mode').tagName, 'select');
const schema = TEMPLATES[provider].fields.endpoint_mode;
assert.equal(field('endpoint_mode').value, schema.default);
const selected = provider === 'doubao' ? 'agent_plan' : schema.options.find(value => value !== schema.default);
assert.ok(selected); change(field('endpoint_mode'), selected); manageKeys();
assert.deepEqual(listedKeys(), original.api_keys); keyAction('cancel-keys').click();
assert.equal(field('endpoint_mode').value, selected); assert.equal(field('api_base').value, original.api_base);
apply(); assert.equal(posts().length, 0); action('save').click(); await tick();
const entry = posts()[0].payload.entries[0];
assert.equal(entry.values.endpoint_mode, selected);
assert.equal(entry.values.api_base, original.api_base); assert.deepEqual(plain(entry.values.api_keys), original.api_keys);
assert.deepEqual(plain(entry.secret_actions), {}); view.destroy();
"""
    )
