"""工作台临时参数弹窗的草稿、确认与取消行为。"""

from tests.test_studio_frontend import _run_settings_script, _settings_script


def _dialog_script() -> str:
    return (
        _settings_script(include_workbench=True)
        + r"""
Node.prototype.classList = {add() {}, remove() {}};
Node.prototype.removeEventListener = function() {};
const Toast = {success() {}, warning() {}, info() {}};
const Modal = {
  count: 0, current: null,
  openCustom(options) {
    this.close(false);
    this.count++;
    this.current = options;
    this.body = new Node(); this.footer = new Node();
    options.renderBody(this.body);
    options.renderFooter(this.footer, () => this.close(true), () => this.close(false));
  },
  close(result) {
    const current = this.current; this.current = null;
    current?.onClose?.(result);
  }
};
function makeView() {
  const view = Object.create(WorkbenchView.prototype);
  view.destroyed = false; view.parameterDialog = null; view.formAvailable = true;
  view.selectModel = new Node(); view.selectModel.value = '0';
  view.selectResolution = new Node(); view.selectResolution.value = '2K';
  view.selectAspectRatio = new Node(); view.selectAspectRatio.value = '16:9';
  view.inputImageCount = new Node(); view.inputImageCount.value = '3';
  view.btnToggleMoreParams = new Node(); view.moreParamsDisclosure = new Node();
  view.overrideCount = new Node();
  view.preferences = new StudioPreferences();
  view.store = {referenceTray: [], capabilities: [{candidate_id: 'test#1', provider: 'test', model: 'model', parameters: {}, generation_fields: {
    seed: {type: 'integer', label: '种子', value: 7, minimum: 0, maximum: 100},
    watermark: {type: 'boolean', label: '水印', value: true},
    max_reference_images: {type: 'integer', label: '参考图上限', value: 6, minimum: 0, maximum: 14},
    size_mode: {type: 'string', label: '尺寸模式', value: 'custom', max_length: 20, enum: ['custom', 'preset']},
    custom_size: {type: 'string', label: '尺寸', value: '100x100', max_length: 20, condition: {size_mode: 'custom'}}
  }}]};
  view.activeCandidate = view.selectedModel();
  view.settingsEditor = new GenerationSettingsEditor(new Node());
  view.settingsEditor.render(view.activeCandidate.generation_fields, {seed: 7, size_mode: 'custom', custom_size: '100x100'});
  view.conditionsChanged = 0; view.referencesChanged = 0;
  view.updateGenerationConditions = () => view.conditionsChanged++;
  view.updateReferenceCounter = () => view.referencesChanged++;
  return view;
}
function change(dialog, name, value) {
  const control = dialog.editor.controls.get(name);
  control.enabled.checked = true;
  control.input.value = String(value);
  control.input.handlers.input();
}
const apply = () => Modal.footer.children[2].handlers.click();
const reset = () => Modal.footer.children[0].handlers.click();
"""
    )


def test_edits_and_cancel_do_not_modify_confirmed_parameters_or_preferences():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView(); const before = view.settingsEditor.overrides();
view.openParameterDialog();
assert.equal(Modal.current.variant, 'parameters');
assert.equal(view.btnToggleMoreParams.attributes['aria-expanded'], 'true');
change(view.parameterDialog, 'seed', 42);
change(view.parameterDialog, 'max_reference_images', 1);
assert.deepEqual(view.settingsEditor.overrides(), before);
assert.equal(view.conditionsChanged, 0); assert.equal(view.referencesChanged, 0);
assert.equal(stored.size, 0);
Modal.close(false);
assert.deepEqual(view.settingsEditor.overrides(), before);
assert.equal(view.parameterDialog, null);
assert.equal(view.btnToggleMoreParams.attributes['aria-expanded'], 'false');
view.openParameterDialog();
assert.equal(view.parameterDialog.editor.controls.get('seed').input.value, '7');
Modal.close(false);
"""
    )


def test_save_applies_typed_values_once_and_keeps_other_workbench_fields():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView(); view.openParameterDialog();
change(view.parameterDialog, 'seed', 0);
change(view.parameterDialog, 'watermark', false);
change(view.parameterDialog, 'size_mode', 'preset');
apply();
assert.equal(view.parameterDialog, null);
assert.deepEqual(view.settingsEditor.overrides(), {seed: 0, watermark: false, size_mode: 'preset'});
assert.equal(view.settingsEditor.overrides({includeHidden:true}).custom_size, '100x100');
assert.equal(view.conditionsChanged, 1); assert.equal(view.referencesChanged, 1);
const saved = view.preferences.get(view.activeCandidate);
assert.equal(saved.generation_settings.seed, 0);
assert.equal(saved.generation_settings.watermark, false);
assert.equal(saved.resolution, '2K'); assert.equal(saved.aspect_ratio, '16:9'); assert.equal(saved.image_count, 3);
assert.equal(view.activeCandidate.generation_fields.seed.value, 7);
"""
    )


def test_invalid_input_keeps_dialog_open_without_saving():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView(); view.openParameterDialog();
change(view.parameterDialog, 'seed', 101);
apply();
assert.ok(view.parameterDialog);
assert.match(view.parameterDialog.feedback.textContent, /种子/);
assert.equal(view.settingsEditor.overrides().seed, 7);
assert.equal(stored.size, 0);
Modal.close(false);
"""
    )


def test_restore_defaults_requires_confirmation_and_does_not_reset_image_count():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView(); view.openParameterDialog(); reset();
assert.deepEqual(view.parameterDialog.editor.overrides(), {});
assert.equal(view.settingsEditor.overrides().seed, 7);
assert.equal(stored.size, 0);
Modal.close(false);
assert.equal(view.settingsEditor.overrides().seed, 7);
view.openParameterDialog(); reset(); apply();
assert.deepEqual(view.settingsEditor.overrides(), {});
assert.equal(view.inputImageCount.value, '3');
assert.equal(view.selectResolution.value, '2K');
assert.deepEqual(view.preferences.get(view.activeCandidate).generation_settings, {});
"""
    )


def test_model_change_closes_dialog_and_stale_confirmation_is_ignored():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView(); view.openParameterDialog();
change(view.parameterDialog, 'seed', 42);
const staleApply = Modal.footer.children[2].handlers.click;
view.store.capabilities.push({...view.activeCandidate, candidate_id: 'test#2', model: 'other'});
view.selectModel.value = '1';
view.handleModelChange({remember: false, restore: false});
assert.equal(view.parameterDialog, null);
staleApply();
assert.equal(view.activeCandidate.model, 'other');
assert.deepEqual(view.settingsEditor.overrides(), {});
assert.equal(view.preferences.get(view.activeCandidate), null);
"""
    )


def test_old_expanded_preference_never_reopens_dialog_and_destroy_discards_draft():
    _run_settings_script(
        _dialog_script()
        + r"""
const view = makeView();
view.preferences.remember(view.activeCandidate, {generation_settings: {seed: 9}, expanded: true});
view.handleModelChange({remember: false});
assert.equal(Modal.count, 0);
assert.equal(view.settingsEditor.overrides().seed, 9);
view.openParameterDialog(); change(view.parameterDialog, 'seed', 42);
view.destroy();
assert.equal(view.parameterDialog, null);
assert.equal(view.settingsEditor.overrides().seed, 9);
assert.equal(view.preferences.get(view.activeCandidate).generation_settings.seed, 9);
view.openParameterDialog(); assert.equal(Modal.count, 1);
"""
    )


def test_unconfirmed_dialog_cannot_submit_generation():
    _run_settings_script(
        _dialog_script()
        + r"""
(async () => {
  const view = makeView(); view.openParameterDialog();
  view.calculateBatchBudget = () => {throw new Error('Unconfirmed draft reached submission');};
  await view.submit();
  assert.equal(stored.size, 0);
  Modal.close(false);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )
