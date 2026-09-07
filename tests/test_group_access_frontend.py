"""Exercise group controls through the Studio DOM/bridge harness."""

from tests.test_studio_limits_frontend import _run


def test_modes_share_one_group_list_and_save_with_rate_limits():
    _run(r"""
server.limits.group_limit_mode = 'blacklist';
server.limits.group_limit_list = ['123', 'hash-group'];
const view = new LimitsView(); await view.open();
const mode = view.editor.querySelector('[data-access-field="group_limit_mode"]');
const list = view.editor.querySelector('[data-access-field="group_limit_list"]');
assert.equal(mode.value, 'blacklist');
assert.equal(list.value, '123\nhash-group');
assert.match(view.editor.textContent, /黑名单群号/);
for (const value of ['whitelist', 'none', 'blacklist']) {
  mode.value = value; mode.dispatch('change');
  assert.deepEqual(plain(view.draft.group_limit_list), ['123', 'hash-group']);
  assert.equal(list.value, '123\nhash-group');
}
mode.value = 'whitelist'; mode.dispatch('change');
input(list, ' 456 \n456\n\nopaque-group');
assert.deepEqual(plain(view.draft.group_limit_list), ['456', 'opaque-group']);
assert.match(view.editor.textContent, /白名单群号/);
assert.match(view.editor.textContent, /仅允许名单中的群/);
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 1);
assert.equal(posts()[0].body.limits.group_limit_mode, 'whitelist');
assert.deepEqual(plain(posts()[0].body.limits.group_limit_list), ['456', 'opaque-group']);
assert.equal(posts()[0].body.limits.global_rate_limit.period_seconds, 60);
assert.equal(view.dirty, false);
view.destroy();
""")


def test_empty_whitelist_is_explicit_and_old_backend_never_invents_empty_access():
    _run(r"""
server.limits.group_limit_mode = 'whitelist';
const view = new LimitsView(); await view.open();
assert.match(view.editor.textContent, /白名单为空.*所有群均可使用/);
view.destroy();
delete server.limits.group_limit_mode;
delete server.limits.group_limit_list;
const old = new LimitsView(); await old.open();
assert.equal(old.loaded, false);
assert.equal(old.saveButton.disabled, true);
assert.equal(old.draft, null);
old.destroy();
""")


def test_group_limits_and_failed_save_preserve_draft():
    _run(r"""
const view = new LimitsView(); await view.open();
const mode = view.editor.querySelector('[data-access-field="group_limit_mode"]');
const list = view.editor.querySelector('[data-access-field="group_limit_list"]');
mode.value = 'blacklist'; mode.dispatch('change');
input(list, Array.from({length:1001}, (_, i) => String(i)).join('\n'));
byId('btn-save-limits').click(); await tick();
assert.equal(posts().length, 0);
assert.match(byId('limits-status').textContent, /最多 1000/);
input(list, '123');
post = async () => { throw {status:409}; };
byId('btn-save-limits').click(); await tick();
assert.equal(view.saveButton.disabled, true);
assert.equal(view.draft.group_limit_mode, 'blacklist');
assert.deepEqual(plain(view.draft.group_limit_list), ['123']);
view.destroy();
""")
