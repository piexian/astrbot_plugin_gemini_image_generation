"""Provider controls exercise the real controller with fake bridge data only."""

from test_provider_config_frontend import _run


def test_outer_switch_is_a_dirty_draft_not_an_editor_or_immediate_save():
    _run(r"""
server.entries.push({id: 'unknown', api_type: 'legacy', supported: false, values: {}, secrets: {}});
const view = create(); await view.open();
let toggle = root.querySelector('[data-pc-enabled="0"]');
assert.equal(toggle.getAttribute('role'), 'switch');
assert.equal(toggle.getAttribute('aria-label'), '启用条目 1');
assert.equal(toggle.checked, true);
assert.equal(root.querySelector('[data-pc-enabled="2"]').disabled, true);
toggle.click(); assert.equal(view.draft.entries[0].values.enabled, false);
assert.equal(view.dirty, true); assert.equal(modal.options, undefined); assert.equal(posts().length, 0);
selectTab('polling'); assert.deepEqual(plain(view.pollingOrder()), []);
selectTab('entries');
const pending = deferred(); post = () => pending.promise;
action('save').click(); assert.equal(root.querySelector('[data-pc-enabled="0"]').disabled, true);
assert.equal(posts()[0].payload.entries[0].values.enabled, false);
pending.reject({status: 500}); await tick();
assert.equal(view.dirty, true); assert.equal(root.querySelector('[data-pc-enabled="0"]').checked, false);
assert.equal(root.querySelector('[data-pc-enabled="0"]').disabled, false); view.destroy();
""")


def test_managed_keys_dedupe_clear_and_cancel_without_secret_actions():
    _run(r"""
const view = create(); await view.open(); edit(0);
assert.equal(field('api_keys'), null); assert.equal(mode('api_keys'), null);
setKeys([' fake-old-two ', 'fake-old-two', 'fake-new']);
manageKeys(); keyAction('batch-keys').click();
input(keyInput('batchValue'), 'fake-new\n fake-append \nfake-append'); keyAction('import-keys').click();
assert.deepEqual(listedKeys(), ['fake-old-two', 'fake-new', 'fake-append']);
keyAction('apply-keys').click();
apply();
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['fake-old-two', 'fake-new', 'fake-append']);
assert.equal(view.draft.entries[0].secret_actions.api_keys, undefined);
edit(0); setKeys(['cancelled-fake']); action('cancel-edit', null, footer).click();
assert.deepEqual(plain(view.draft.entries[0].values.api_keys), ['fake-old-two', 'fake-new', 'fake-append']);
edit(0); setKeys([]); apply();
action('save').click(); await tick();
assert.deepEqual(plain(posts()[0].payload.entries[0].values.api_keys), []);
assert.equal(posts()[0].payload.entries[0].secret_actions.api_keys, undefined); view.destroy();
""")


def test_old_backend_no_keys_cannot_be_mistaken_for_empty_replacement():
    _run(r"""
delete server.key_values_visible; delete server.entries[0].values.api_keys;
const view = create(); await view.open(); edit(0);
assert.equal(field('api_keys').disabled, true); assert.match(body.textContent, /旧后台未返回已有 Key/);
assert.equal(keyAction('manage-keys').disabled, true);
keyAction('manage-keys').click(); assert.equal(body.querySelector('[data-pc-key-manager]'), null);
input(field('api_keys'), 'not-allowed'); input(field('model'), 'new-model'); apply();
action('save').click(); await tick();
assert.equal(posts()[0].payload.entries[0].values.api_keys, undefined);
assert.equal(posts()[0].payload.entries[0].secret_actions.api_keys, undefined);
assert.equal(posts()[0].payload.entries[0].values.model, 'new-model'); view.destroy();
""")


def test_key_batch_limits_do_not_replace_original_draft():
    _run(r"""
const view = create(); await view.open(); edit(0);
manageKeys(); keyAction('batch-keys').click();
input(keyInput('batchValue'), Array.from({length: 201}, (_, i) => `fake-${i}`).join('\n'));
keyAction('import-keys').click();
assert.match(body.querySelector('[data-pc-key-status]').textContent, /最多 200/);
assert.ok(keyInput('batchValue'));
assert.deepEqual(plain(view.editor.keys.items), ['fake-old-one', 'fake-old-two']);
input(keyInput('batchValue'), 'x'.repeat(8193)); keyAction('import-keys').click();
assert.match(body.querySelector('[data-pc-key-status]').textContent, /8192/);
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-old-one', 'fake-old-two']); view.destroy();
""")


def test_real_vision_select_refresh_and_manual_fallback_preserve_all_drafts():
    _run(r"""
server.common.values.vision_provider_id = 'missing-current';
const view = create(); await view.open(); selectTab('common');
assert.equal(field('vision_provider_id', root).tagName, 'select');
assert.equal(root.querySelectorAll('datalist').length, 0);
assert.equal(field('vision_provider_id', root).value, 'missing-current');
assert.ok(field('vision_provider_id', root).children.some(item => item.value === 'missing-current'));
input(field('vision_model', root), 'custom-kept'); input(field('proxy', root), 'http://fake-proxy.invalid');
const pending = deferred(); get = () => pending.promise;
action('refresh-vision').click(); assert.equal(action('refresh-vision').disabled, true);
change(field('vision_provider_id', root), 'vision-1');
pending.resolve({vision_providers: [{id: 'new-vision', label: 'Configured fake', model: 'fake-model', available: false}], vision_providers_available: true});
await tick(); assert.equal(calls.at(-1).route, 'webui/vision-providers');
assert.equal(field('vision_provider_id', root).value, 'vision-1');
assert.ok(field('vision_provider_id', root).children.some(item => item.value === 'new-vision'));
assert.equal(field('vision_model', root).value, 'custom-kept');
assert.equal(field('proxy', root).value, 'http://fake-proxy.invalid');
get = async () => { throw Error('do-not-echo-private'); };
action('refresh-vision').click(); await tick();
assert.match(root.textContent, /刷新失败/); assert.doesNotMatch(root.textContent, /do-not-echo-private/);
assert.equal(field('vision_provider_id', root).value, 'vision-1');
action('manual-vision').click(); assert.equal(field('vision_provider_id', root).tagName, 'input');
input(field('vision_provider_id', root), 'manual-custom'); action('manual-vision').click();
assert.equal(field('vision_provider_id', root).tagName, 'select');
assert.equal(field('vision_provider_id', root).value, 'manual-custom');
selectTab('entries'); selectTab('common'); assert.equal(field('vision_model', root).value, 'custom-kept');
view.destroy();
""")


def test_entry_models_use_connection_only_current_draft_search_and_explicit_pick():
    _run(r"""
const view = create(); await view.open(); edit(0);
assert.equal(posts().length, 0);
input(field('model'), ''); input(field('priority'), 'not-a-number');
setKeys([' fake-current ', 'fake-second']);
input(field('api_base'), 'https://fake-draft.invalid/v1');
const pending = deferred(); post = () => pending.promise;
action('fetch-models', null, body).click();
assert.equal(action('fetch-models', null, body).disabled, true);
action('fetch-models', null, body).click(); assert.equal(posts().length, 1);
const request = posts()[0]; assert.equal(request.route, 'webui/providers/models');
assert.deepEqual(plain(request.payload), {kind: 'entry', revision: 'r1', confirmed_target: false,
 entry: {id: 'opaque-0', api_type: 'google', values: {api_keys: ['fake-current', 'fake-second'], api_base: 'https://fake-draft.invalid/v1'}, secret_actions: {}},
 common: {values: {proxy: ''}, secret_actions: {}}});
pending.resolve({models: [{id: 'm-one', label: 'First'}, {id: 'm-two', label: 'Second'}], warning: 'fake-warning', truncated: true});
await tick(); assert.equal(field('model').value, '');
assert.match(body.textContent, /fake-warning/); assert.match(body.textContent, /截断/);
input(body.querySelector('[data-pc-search]'), 'second');
assert.equal(body.querySelectorAll('[data-pc-action="choose-model"]').length, 1);
action('choose-model', null, body).click(); assert.equal(field('model').value, 'm-two');
assert.equal(view.editor.entry.values.model, 'm-two');
assert.equal(view.draft.entries[0].values.model, 'model-free-form');
input(field('model'), 'any-custom-model'); input(field('priority'), '0'); apply();
assert.equal(view.draft.entries[0].values.model, 'any-custom-model'); assert.equal(posts().length, 1);
edit(1); assert.equal(action('fetch-models', null, body), null); assert.match(body.textContent, /暂未接入/); view.destroy();
""")


def test_model_catalog_metadata_drives_endpoint_id_and_pick_updates_conditions():
    _run(r"""
server.model_catalog = {doubao: {supported: true}};
server.templates.doubao.fields.endpoint_id = {type: 'string', description: '假端点', default: ''};
server.templates.doubao.fields.fake_dependent = {type: 'string', description: '条件字段', condition: {endpoint_id: 'fake-endpoint'}};
const view = create(); await view.open(); edit(1);
const wrap = body.querySelector('[data-pc-field-wrap="fake_dependent"]'); assert.equal(wrap.hidden, true);
post = async () => ({models: [{id: 'fake-endpoint', label: 'Fake endpoint'}]});
action('fetch-models', null, body).click(); await tick(); action('choose-model', null, body).click();
assert.equal(field('endpoint_id').value, 'fake-endpoint'); assert.equal(wrap.hidden, false); view.destroy();
""")


def test_target_confirmation_is_inline_same_request_and_invalidated_by_connection_edits():
    _run(r"""
const view = create(); await view.open(); edit(0);
post = async () => { throw {status: 409, data: {reason: 'confirm_target', target: 'fake-target.invalid'}}; };
input(field('api_base'), 'https://fake-target.invalid');
action('fetch-models', null, body).click(); await tick();
assert.equal(confirms, 0); assert.ok(view.editor); assert.equal(view.conflict, false);
assert.ok(action('confirm-target', null, body)); assert.match(body.textContent, /fake-target.invalid/);
const first = plain(posts()[0].payload);
post = async () => ({models: [{id: 'confirmed-model', label: 'Confirmed'}]});
action('confirm-target', null, body).click(); await tick();
assert.deepEqual(plain(posts()[1].payload), {...first, confirmed_target: true});
assert.equal(action('confirm-target', null, body), null); assert.equal(confirms, 0);
post = async () => { throw {status: 409, data: {reason: 'confirm_target', target: 'fake-next.invalid'}}; };
action('fetch-models', null, body).click(); await tick();
const stale = action('confirm-target', null, body);
setKeys(['changed-fake-key']);
assert.equal(action('confirm-target', null, body), null);
assert.equal(view.catalogs.edit.confirmation, null);
stale.click(); await tick(); assert.equal(posts().length, 3);
action('fetch-models', null, body).click(); await tick();
input(field('proxy'), 'http://new-proxy.invalid'); assert.equal(action('confirm-target', null, body), null);
action('fetch-models', null, body).click(); await tick();
action('cancel-target', null, body).click(); assert.equal(view.catalogs.edit.confirmation, null);
assert.equal(field('model').value, 'model-free-form'); view.destroy();
""")


def test_models_late_results_after_connection_change_close_reopen_and_destroy_are_dropped():
    _run(r"""
const view = create(); await view.open(); edit(0);
let pending = deferred(); post = () => pending.promise;
action('fetch-models', null, body).click(); input(field('api_base'), 'https://changed.invalid');
assert.equal(action('fetch-models', null, body).disabled, false);
pending.resolve({models: [{id: 'stale-base', label: 'Stale'}]}); await tick();
assert.equal(action('choose-model', null, body), null);
const old = deferred(); post = () => old.promise; action('fetch-models', null, body).click();
action('cancel-edit', null, footer).click(); edit(0);
const fresh = deferred(); post = () => fresh.promise; action('fetch-models', null, body).click();
fresh.resolve({models: [{id: 'fresh-only', label: 'Fresh'}]}); await tick();
old.resolve({models: [{id: 'stale-reopen', label: 'Old'}]}); await tick();
assert.match(body.textContent, /fresh-only/); assert.doesNotMatch(body.textContent, /stale-reopen/);
pending = deferred(); post = () => pending.promise; action('fetch-models', null, body).click();
view.destroy(); pending.resolve({models: [{id: 'after-destroy', label: 'Old'}]}); await tick();
assert.equal(root.children.length, 0); assert.equal(body.children.length, 0);
assert.equal(view.catalogs.edit.confirmation, null);
""")


def test_vision_models_empty_failure_and_changed_provider_keep_current_model():
    _run(r"""
const view = create(); await view.open(); selectTab('common');
assert.equal(action('fetch-models').disabled, true);
change(field('vision_provider_id', root), 'vision-1'); input(field('vision_model', root), 'manual-vision-model');
let pending = deferred(); post = () => pending.promise;
action('fetch-models').click(); assert.deepEqual(plain(posts()[0].payload), {kind: 'vision', provider_id: 'vision-1'});
action('manual-vision').click(); input(field('vision_provider_id', root), 'vision-2');
pending.resolve({models: [{id: 'stale-vision', label: 'Stale'}]}); await tick();
assert.equal(action('choose-model'), null); assert.equal(field('vision_model', root).value, 'manual-vision-model');
post = async () => ({models: []}); action('fetch-models').click(); await tick();
assert.match(root.textContent, /未获取到模型/); assert.equal(field('vision_model', root).value, 'manual-vision-model');
post = async () => { throw {status: 502, message: 'fake-private-key-must-not-echo'}; };
action('fetch-models').click(); await tick(); assert.match(root.textContent, /模型拉取失败/);
assert.doesNotMatch(root.textContent, /fake-private-key-must-not-echo/);
assert.equal(field('vision_model', root).value, 'manual-vision-model');
post = async () => ({models: [{id: 'chosen-vision', label: 'Chosen'}]});
action('fetch-models').click(); await tick(); action('choose-model').click();
assert.equal(view.draft.common.values.vision_model, 'chosen-vision'); assert.equal(view.dirty, true);
const refresh = deferred(); get = () => refresh.promise; action('refresh-vision').click();
view.destroy(); refresh.resolve({vision_providers: []}); await tick(); assert.equal(root.children.length, 0);
""")


def test_sensitive_connection_modes_invalidate_queries_without_losing_managed_keys():
    _run(r"""
server.entries[0].secrets.api_base = {present: true}; delete server.entries[0].values.api_base;
server.common.secrets.proxy = {present: true}; delete server.common.values.proxy;
const view = create(); await view.open(); edit(0);
manageKeys(); input(keyInput('newValue'), 'pending-fake-key'); keyAction('add-key').click();
keyAction('apply-keys').click();
const pending = deferred(); post = () => pending.promise;
action('fetch-models', null, body).click();
assert.deepEqual(plain(posts()[0].payload.entry.secret_actions.api_base), {mode: 'keep'});
assert.deepEqual(plain(posts()[0].payload.common.secret_actions.proxy), {mode: 'keep'});
change(mode('api_base'), 'replace');
manageKeys(); assert.deepEqual(listedKeys(), ['fake-old-one', 'fake-old-two', 'pending-fake-key']);
keyAction('cancel-keys').click();
input(field('api_base'), 'https://fake-user:fake-pass@target.invalid');
pending.reject({status: 409, data: {reason: 'confirm_target', target: 'stale-target.invalid'}}); await tick();
assert.equal(view.catalogs.edit.confirmation, null); assert.equal(action('confirm-target', null, body), null);
assert.deepEqual(plain(view.editor.entry.values.api_keys), ['fake-old-one', 'fake-old-two', 'pending-fake-key']);
post = async () => ({models: []}); action('fetch-models', null, body).click(); await tick();
assert.deepEqual(plain(posts()[1].payload.entry.secret_actions.api_base), {mode: 'replace', value: 'https://fake-user:fake-pass@target.invalid'});
assert.equal(posts()[1].payload.entry.values.api_base, undefined); view.destroy();
""")


def test_catalog_revision_error_is_not_target_confirmation_or_configuration_conflict():
    _run(r"""
const view = create(); await view.open(); edit(0);
post = async () => { throw {status: 409, data: {reason: 'revision'}}; };
action('fetch-models', null, body).click(); await tick();
assert.match(body.textContent, /配置版本已变化/); assert.equal(action('confirm-target', null, body), null);
assert.equal(view.conflict, false); assert.equal(field('model').value, 'model-free-form');
assert.ok(view.editor); view.destroy();
""")
