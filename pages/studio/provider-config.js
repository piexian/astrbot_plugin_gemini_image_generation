/* Provider configuration is a separate, memory-only draft; never import app.js. */
(() => {
  'use strict';
  const copy = value => JSON.parse(JSON.stringify(value));
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const SECRET_FIELDS = new Set(['api_keys', 'api_base', 'proxy']);
  const GENERAL = new Set(['enabled', 'priority', 'model', 'base_model', 'model_alias', 'endpoint_id', 'endpoint_mode', 'api_keys', 'api_base', 'proxy', 'service_account_files', 'project_id', 'location']);
  const GENERATION = new Set(['resolution', 'aspect_ratio', 'size', 'size_mode', 'custom_size', 'default_size', 'width', 'height', 'quality', 'n', 'negative_prompt', 'seed', 'output_format', 'style', 'style_type', 'style_weight']);
  let sequence = 0;

  class StudioProviderConfigView {
    constructor({root, bridge, dom, modal, onSaved = async () => {}}) {
      this.root = root;
      this.bridge = bridge;
      this.dom = dom;
      this.modal = modal;
      this.onSaved = onSaved;
      this.prefix = `provider-config-${++sequence}`;
      this.tab = 'entries';
      this.snapshot = null;
      this.draft = null;
      this.loaded = false;
      this.loading = false;
      this.saving = false;
      this.conflict = false;
      this.loadFailed = false;
      this.destroyed = false;
      this.epoch = 0;
      this.message = '打开供应商配置后加载；修改仅在点击保存后生效。';
      this.cleanups = [];
      this.editorCleanups = [];
      this.catalogs = {edit: {token: 0}, common: {token: 0}};
      this.visionRefreshToken = 0;
      this.visionRefreshing = false;
      this.visionManual = false;
      this.keysHidden = true;
      this.bind(root, this.cleanups);
      this.render();
    }

    el(tag, attrs = {}, children = []) { return this.dom.el(tag, attrs, children); }
    button(text, action, attrs = {}) {
      return this.el('button', {type: 'button', className: 'comic-btn comic-btn--outline',
        'aria-label': text, 'data-pc-action': action, ...attrs}, [text]);
    }
    get available() { return !!this.bridge && (!this.bridge.isAvailable || this.bridge.isAvailable()); }
    get writable() { return !this.destroyed && this.loaded && !this.loading && !this.loadFailed && !this.saving && this.available && !this.snapshot?.requires_reload; }
    get dirty() { return !!this.draft && JSON.stringify(this.draft) !== this.baseline; }
    label(type) { return this.snapshot?.templates?.[type]?.label || this.snapshot?.provider_types?.find(item => item.id === type)?.label || type; }

    bind(container, cleanups) {
      const handlers = {
        click: event => this.click(event), input: event => this.input(event), change: event => this.input(event),
        paste: event => {
          if (event.target.dataset.pcField !== 'api_keys' || !this.editor || !this.keysVisible(this.editor.entry)) return;
          const text = event.clipboardData?.getData('text') || '';
          if (/\r|\n/.test(text)) { event.preventDefault(); this.openKeys(text); }
        },
        keydown: event => this.keydown(event), pointerdown: event => this.pointerDown(event),
        pointermove: event => this.pointerMove(event), pointerup: event => this.pointerEnd(event),
        pointercancel: () => this.releasePointer(), lostpointercapture: () => this.releasePointer()
      };
      for (const [type, handler] of Object.entries(handlers)) {
        container.addEventListener(type, handler);
        cleanups.push(() => container.removeEventListener(type, handler));
      }
    }

    async open() {
      if (this.destroyed || this.loaded) return;
      if (this.pendingLoad) return this.pendingLoad;
      this.pendingLoad = this.load();
      try { await this.pendingLoad; } finally { this.pendingLoad = null; }
    }

    adopt(snapshot) {
      if (!snapshot || typeof snapshot.revision !== 'string' || !Array.isArray(snapshot.entries) || !snapshot.templates || !snapshot.common?.values || !snapshot.common_fields) {
        throw new Error('Invalid provider configuration response');
      }
      this.invalidateCatalog('edit');
      this.invalidateCatalog('common');
      ++this.visionRefreshToken;
      this.visionRefreshing = false;
      this.visionRefreshMessage = '';
      this.snapshot = copy(snapshot);
      const entry = item => ({id: item.id ?? null, api_type: item.api_type,
        values: item.supported === false ? {} : copy(item.values || {}), secret_actions: {}});
      this.draft = {revision: snapshot.revision, provider_polling: copy(snapshot.provider_polling || []),
        entries: snapshot.entries.map(entry), common: {values: copy(snapshot.common.values || {}), secret_actions: {}}};
      this.baseline = JSON.stringify(this.draft);
      this.loaded = true;
      this.loadFailed = false;
      this.conflict = false;
    }

    async load() {
      if (this.destroyed || this.loading || this.saving) return;
      if (!this.available) { this.message = '桥接不可用，请在 AstrBot Dashboard 中打开。'; this.render(); return; }
      const epoch = ++this.epoch;
      this.loading = true;
      this.message = '正在加载供应商配置…';
      this.render();
      try {
        const snapshot = await this.bridge.get('webui/providers');
        if (this.destroyed || epoch !== this.epoch) return;
        this.adopt(snapshot);
        this.message = snapshot.requires_reload ? '运行时恢复失败，请重载插件后再使用。' : snapshot.busy
          ? '生成任务正在运行；可编辑草稿，任务结束后再保存。' : '配置已加载。密钥仅在此配置页明文编辑；保存不会自动重载插件。';
      } catch (_) {
        if (this.destroyed || epoch !== this.epoch) return;
        this.loadFailed = true;
        this.message = '加载失败，已有草稿保留；请重新加载后再编辑。';
      } finally {
        if (!this.destroyed && epoch === this.epoch) { this.loading = false; this.render(); }
      }
    }

    metadata(entry) {
      return entry.id == null ? null : this.snapshot.entries.find(item => item.id === entry.id);
    }
    supported(entry) {
      return this.metadata(entry)?.supported !== false && own(this.snapshot.templates, entry.api_type);
    }
    fields(entry) { return this.snapshot.templates[entry.api_type]?.fields || {}; }
    secrets(entry, common = false) { return common ? this.snapshot.common.secrets || {} : this.metadata(entry)?.secrets || {}; }

    render() {
      if (this.destroyed) return;
      this.releasePointer();
      this.root.replaceChildren();
      this.root.classList.add('provider-config');
      const heading = this.el('div', {className: 'pc-heading'}, [this.el('div', {}, [
        this.el('h2', {}, ['供应商配置']), this.el('p', {className: 'pc-note'}, ['配置与创作参数独立；保存只更新供应商设置，不修改限流。'])])]);
      const actions = this.el('div', {className: 'pc-actions'});
      this.reloadButton = this.button('重新加载', 'reload', {disabled: this.loading || this.saving || !this.available});
      this.saveButton = this.button(this.saving ? '正在保存…' : '保存供应商配置', 'save',
        {className: 'comic-btn comic-btn--cta', disabled: !this.writable || this.conflict || !this.dirty || !!this.editor});
      actions.appendChild(this.reloadButton); actions.appendChild(this.saveButton); heading.appendChild(actions);
      this.status = this.el('p', {className: 'pc-status', role: 'status', 'aria-live': 'polite'}, [this.message]);
      this.dirtyNotice = this.el('span', {className: 'pc-note', 'data-pc-dirty': ''}, [this.dirty ? '有未保存的修改' : '无未保存的修改']);
      this.root.appendChild(heading); this.root.appendChild(this.status); this.root.appendChild(this.dirtyNotice);
      const tabs = this.el('div', {role: 'tablist', 'aria-label': '供应商配置分类', className: 'pc-tabs'});
      for (const [id, text] of [['entries', '配置条目'], ['polling', '轮询排序'], ['common', '公共设置']]) {
        tabs.appendChild(this.button(text, 'tab', {role: 'tab', id: `${this.prefix}-tab-${id}`,
          'data-pc-tab': id, 'aria-controls': `${this.prefix}-panel-${id}`, 'aria-selected': String(id === this.tab),
          tabindex: id === this.tab ? 0 : -1}));
      }
      this.root.appendChild(tabs);
      for (const id of ['entries', 'polling', 'common']) {
        const panel = this.el('section', {role: 'tabpanel', id: `${this.prefix}-panel-${id}`,
          'aria-labelledby': `${this.prefix}-tab-${id}`, tabindex: 0, className: 'pc-panel'});
        panel.hidden = id !== this.tab;
        this.root.appendChild(panel);
        if (id === this.tab) this.panel = panel;
      }
      if (!this.draft) return;
      this.fieldset = this.el('fieldset', {className: 'pc-fieldset', disabled: !this.writable});
      this.panel.appendChild(this.fieldset);
      if (this.tab === 'entries') this.renderEntries(this.fieldset);
      else if (this.tab === 'polling') this.renderPolling(this.fieldset);
      else this.renderFields(this.fieldset, this.draft.common, this.snapshot.common_fields, true);
    }

    touch() {
      if (this.dirtyNotice) this.dirtyNotice.textContent = this.dirty ? '有未保存的修改' : '无未保存的修改';
      if (this.saveButton) this.saveButton.disabled = !this.writable || this.conflict || !this.dirty || !!this.editor;
    }

    renderEntries(container) {
      const add = this.el('select', {'aria-label': '新增供应商类型', 'data-pc-new-type': '', className: 'comic-select'});
      for (const type of this.snapshot.provider_types || []) {
        if (own(this.snapshot.templates, type.id)) add.appendChild(this.el('option', {value: type.id}, [type.label]));
      }
      container.appendChild(this.el('div', {className: 'pc-actions'}, [add, this.button('新增配置条目', 'add', {disabled: this.draft.entries.length >= 100})]));
      container.appendChild(this.el('p', {className: 'pc-note'}, ['以下为原始配置，包括禁用和不完整条目，并非当前有效候选。同类型按优先级降序尝试；同优先级按此表顺序。']));
      const table = this.el('table', {className: 'pc-table', 'aria-label': '供应商原始配置条目'});
      table.appendChild(this.el('thead', {}, [this.el('tr', {}, ['条目 / 供应商', '模型', '状态', '优先级', '密钥', '操作'].map(text => this.el('th', {scope: 'col'}, [text])))]));
      const body = this.el('tbody');
      this.draft.entries.forEach((entry, index) => {
        const known = this.supported(entry);
        const meta = this.metadata(entry);
        const count = Array.isArray(entry.values.api_keys) ? entry.values.api_keys.length : meta?.secrets?.api_keys?.count || 0;
        // 无 API Key 但配置了文件/内联凭证的条目（如 vertex 服务账号）显示凭证状态而非「0 个」
        const credentialFiles = Array.isArray(entry.values.service_account_files)
          ? entry.values.service_account_files.filter((item) => String(item).trim()).length
          : 0;
        const keyText = !known ? '保留原值'
          : count ? `${count} 个`
          : credentialFiles ? 'JSON 凭证'
          : '0 个';
        const enabled = this.el('input', {type: 'checkbox', role: 'switch', 'aria-label': `启用条目 ${index + 1}`,
          'data-pc-enabled': index, disabled: !known || !this.writable});
        enabled.checked = known && entry.values.enabled !== false;
        const rowActions = this.el('div', {className: 'pc-actions'});
        rowActions.appendChild(this.button('编辑', 'edit', {'aria-label': `编辑条目 ${index + 1}`, 'data-pc-index': index, disabled: !known}));
        rowActions.appendChild(this.button('删除', 'delete', {'aria-label': `删除条目 ${index + 1}`, 'data-pc-index': index}));
        const values = [ `${index + 1} · ${this.label(entry.api_type)}`, known ? entry.values.model || entry.values.endpoint_id || '未填写' : '旧条目（只可保留或删除）',
          this.el('label', {className: 'pc-enabled'}, [enabled, !known ? '不支持' : entry.values.enabled === false ? '已禁用' : '已启用']),
          known ? entry.values.priority ?? 0 : '—', known ? keyText : '保留原值'];
        const row = this.el('tr', {'data-pc-entry': index}, values.map(value => this.el('td', {}, [value])));
        row.appendChild(this.el('td', {}, [rowActions])); body.appendChild(row);
      });
      table.appendChild(body);
      container.appendChild(this.el('div', {className: 'pc-table-scroll', tabindex: 0, 'aria-label': '配置表，可横向滚动'}, [table]));
      if (!this.draft.entries.length) container.appendChild(this.el('p', {className: 'pc-empty'}, ['尚无配置条目。选择供应商后添加独立配置。']));
    }

    pollingOrder() {
      if (this.draft.provider_polling.length) return [...this.draft.provider_polling];
      // This is a configuration preview, NOT a reconstruction of runtime candidates.
      return [...new Set(this.draft.entries.filter(entry => this.supported(entry) && entry.values.enabled !== false).map(entry => entry.api_type))];
    }
    renderPolling(container) {
      const automatic = !this.draft.provider_polling.length;
      container.appendChild(this.el('h3', {}, [automatic ? '自动跟随有效配置' : '手动轮询顺序']));
      container.appendChild(this.el('p', {className: 'pc-note'}, [automatic
        ? '当前自动跟随后端有效配置。下方仅预览已启用的配置类型，不代表有效候选；拖动、上移或下移后切换为手动轮询。'
        : '按下方顺序尝试供应商；未参与的供应商不参与聊天自动生成，但仍可在工作台选择，其配置不会删除。']));
      container.appendChild(this.button('恢复自动轮询', 'auto', {disabled: automatic}));
      const order = this.pollingOrder();
      const list = this.el('ol', {className: 'pc-poll-list', 'aria-label': automatic ? '自动轮询配置类型预览' : '参与轮询的供应商'});
      order.forEach((type, index) => {
        list.appendChild(this.el('li', {className: 'pc-poll-row', 'data-pc-poll-index': index}, [
          this.button('拖动', 'handle', {className: 'comic-btn comic-btn--outline pc-drag-handle',
            'aria-label': `拖动排序 ${this.label(type)}`, 'data-pc-index': index, 'aria-describedby': `${this.prefix}-drag-help`}),
          this.el('span', {className: 'pc-poll-name'}, [`${index + 1}. ${this.label(type)}`]),
          this.el('div', {className: 'pc-actions'}, [
            this.button('上移', 'up', {'aria-label': `上移 ${this.label(type)}`, 'data-pc-index': index, disabled: index === 0}),
            this.button('下移', 'down', {'aria-label': `下移 ${this.label(type)}`, 'data-pc-index': index, disabled: index === order.length - 1}),
            this.button('移出轮询', 'remove-poll', {'aria-label': `移出轮询 ${this.label(type)}`, 'data-pc-index': index, disabled: automatic})])
        ]));
      });
      container.appendChild(list);
      container.appendChild(this.el('p', {className: 'pc-note', id: `${this.prefix}-drag-help`}, ['仅排序手柄接管触摸拖动；也可使用上移/下移按钮，或聚焦手柄按方向键。移出最后一项将恢复自动。']));
      container.appendChild(this.el('h3', {}, ['未参与 / 可添加']));
      const rest = this.el('div', {className: 'pc-actions'});
      for (const type of this.snapshot.provider_types || []) {
        if (!order.includes(type.id)) rest.appendChild(this.button(`加入轮询 ${type.label}`, 'add-poll', {'data-pc-type': type.id}));
      }
      if (!rest.children.length) rest.appendChild(this.el('span', {className: 'pc-note'}, ['没有其他供应商类型。']));
      container.appendChild(rest);
    }

    renderFields(container, entry, fields, common = false) {
      const groups = {};
      for (const [id, title] of [['general', '通用'], ['generation', '生成'], ['advanced', '高级设置']]) {
        const section = this.el(id === 'advanced' ? 'details' : 'section', {className: 'pc-field-group', 'data-pc-group': id});
        section.appendChild(this.el(id === 'advanced' ? 'summary' : 'h3', {}, [title]));
        groups[id] = this.el('div', {className: 'pc-field-grid'});
        section.appendChild(groups[id]); container.appendChild(section);
      }
      for (const [name, schema] of Object.entries(fields || {})) {
        const group = common || GENERAL.has(name) ? 'general' : GENERATION.has(name) ? 'generation' : 'advanced';
        const label = schema.description || name;
        const field = this.el('div', {className: 'pc-field', 'data-pc-field-wrap': name});
        // Sibling labels plus an exact aria-label prevent option text entering the accessible name.
        const id = `${this.prefix}-${common ? 'common' : 'edit'}-${name}`;
        field.appendChild(this.el('label', {for: id}, [label]));
        // string 型 api_keys（单凭证渠道）走普通文本输入，不进列表 Key 管理器
        const keyList = name === 'api_keys' && schema.type !== 'string';
        const secret = keyList || (name !== 'api_keys' && !!this.secrets(entry, common)[name]?.present);
        if (keyList) this.renderKeys(field, entry, schema, id);
        else if (common && name === 'vision_provider_id') this.renderVisionProvider(field, entry, schema, id);
        else if (secret) this.renderSecret(field, entry, name, schema, common, id);
        else if (schema.type === 'file' || name === 'service_account_json') this.renderCredentialField(field, entry, schema, name, common, id);
        else field.appendChild(this.control(entry.values[name] ?? schema.default, schema, name, common, id));
        if ((!common && (name === 'model' || name === 'endpoint_id')) || (common && name === 'vision_model')) {
          this.renderCatalogField(field, entry, name, common);
        }
        if (schema.hint) field.appendChild(this.el('p', {className: 'pc-note', id: `${id}-hint`}, [schema.hint]));
        groups[group].appendChild(field);
      }
      for (const group of Object.values(groups)) if (!group.children.length) group.parentNode.hidden = true;
      this.updateConditions(container, entry, fields);
    }

    renderCredentialField(field, entry, schema, name, common, id) {
      // 凭证框：粘贴 JSON 内容或填写文件路径均可；「上传 .json」把文件内容填入框内。
      const current = Array.isArray(entry.values[name]) ? (entry.values[name][0] ?? '') : String(entry.values[name] ?? '');
      const textarea = this.el('textarea', {id, 'data-pc-field': name, 'data-pc-scope': common ? 'common' : 'edit',
        'data-pc-secret': 'false', 'aria-label': schema.description || name, rows: 6,
        autocomplete: 'off', spellcheck: 'false', className: 'comic-input'});
      textarea.value = current;
      field.appendChild(textarea);
      field.appendChild(this.button('上传 .json', 'upload-credential',
        {'data-pc-cred-target': name, 'aria-label': `上传 ${schema.description || name} JSON 文件`}));
      const picker = this.el('input', {type: 'file', accept: '.json,application/json', hidden: true,
        'data-pc-file-picker': name});
      picker.addEventListener('change', () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
          textarea.value = String(reader.result ?? '').trim();
          textarea.dispatchEvent(new Event('change', {bubbles: true}));
        };
        reader.readAsText(file);
        picker.value = '';
      });
      field.appendChild(picker);
    }

    control(value, schema, name, common, id, secret = false) {
      const attrs = {id, 'aria-label': schema.description || name, 'data-pc-field': name,
        'data-pc-scope': common ? 'common' : 'edit', 'data-pc-secret': secret ? 'true' : 'false',
        'aria-describedby': schema.hint ? `${secret ? id.replace(/-replacement$/, '') : id}-hint` : undefined, className: 'comic-input'};
      let control;
      if (Array.isArray(schema.options) && schema.type !== 'list') {
        control = this.el('select', attrs, schema.options.map(option => this.el('option', {value: String(option)}, [String(option) || '（留空）'])));
      } else if (schema.type === 'list' || schema.type === 'file') {
        control = this.el('textarea', {...attrs, rows: 4, autocomplete: 'off', spellcheck: 'false', 'aria-label': schema.description || name});
      } else {
        const numeric = schema.type === 'int' || schema.type === 'float';
        control = this.el('input', {...attrs, type: schema.type === 'bool' ? 'checkbox' : numeric ? 'number' : secret ? 'password' : 'text',
          min: schema.slider?.min, max: schema.slider?.max, step: numeric ? schema.slider?.step ?? (schema.type === 'int' ? 1 : 'any') : undefined,
          autocomplete: 'off', spellcheck: 'false'});
      }
      if (schema.type === 'bool') control.checked = !!value;
      else control.value = (schema.type === 'list' || schema.type === 'file') ? (Array.isArray(value) ? value.join('\n') : value || '') : String(value ?? '');
      return control;
    }

    keysVisible(entry) {
      return own(entry.values, 'api_keys') || entry.id == null || !this.secrets(entry).api_keys?.count;
    }

    renderKeys(container, entry, schema, id) {
      const visible = this.keysVisible(entry), keys = entry.values.api_keys || [];
      const row = this.el('div', {className: 'pc-key-summary', 'data-pc-key-summary': ''});
      const toggle = this.button(this.keysHidden ? '显示' : '隐藏', 'toggle-keys', {
        className: 'comic-btn comic-btn--outline pc-key-toggle', 'aria-label': this.keysHidden ? '显示完整 API Key' : '隐藏 API Key', 'aria-pressed': String(!this.keysHidden)});
      if (keys.length <= 1) {
        const control = this.control(this.keysHidden && keys[0] ? '••••••••' : keys[0] || '', {...schema, type: 'string'}, 'api_keys', false, id);
        control.disabled = !visible || (this.keysHidden && !!keys[0]); control.placeholder = '输入 API Key';
        control.readOnly = this.keysHidden && !!keys[0];
        row.appendChild(control);
      } else {
        row.appendChild(this.el('span', {className: 'pc-key-chip'}, [this.keysHidden ? '••••••••' : keys[0].length > 20 ? `${keys[0].slice(0, 20)}…` : keys[0]]));
        row.appendChild(this.el('span', {className: 'pc-key-count'}, [`+${keys.length - 1}`]));
      }
      if (visible) row.appendChild(toggle);
      row.appendChild(this.button(keys.length <= 1 ? '添加更多' : '管理 Key', 'manage-keys', {
        id: keys.length > 1 ? id : undefined, disabled: !visible, 'aria-label': '管理 API Key'}));
      container.appendChild(row);
      if (!visible) container.appendChild(this.el('p', {className: 'pc-note'}, [
        '旧后台未返回已有 Key，请更新后台并重新加载；当前保存仍保留原值。']));
    }

    keyInput(name, value, attrs = {}) {
      const input = this.el(name === 'batchValue' ? 'textarea' : 'input', {
        type: name === 'batchValue' ? undefined : 'text', className: 'comic-input',
        autocomplete: 'off', spellcheck: 'false', 'data-pc-key-input': name, ...attrs});
      input.value = value;
      return input;
    }

    openKeys(batchValue = null) {
      const editor = this.editor;
      if (!editor || editor.keys || !this.keysVisible(editor.entry) || !this.writable) return;
      this.invalidateCatalog('edit'); this.catalogs.edit.host = null;
      editor.keys = {items: copy(editor.entry.values.api_keys || []), newValue: '',
        editIndex: -1, editValue: '', batch: batchValue !== null, batchValue: batchValue || '', message: ''};
      this.renderKeyManager();
    }

    renderEditorFooter() {
      const editor = this.editor;
      if (!editor?.footer) return;
      const keys = editor.keys;
      editor.footer.replaceChildren();
      editor.footer.appendChild(this.button(keys ? keys.batch ? '返回密钥列表' : '取消密钥更改' : '取消编辑',
        keys ? keys.batch ? 'cancel-key-batch' : 'cancel-keys' : 'cancel-edit'));
      editor.footer.appendChild(this.button(keys ? keys.batch ? '导入到列表' : '完成密钥管理' : '应用到草稿',
        keys ? keys.batch ? 'import-keys' : 'apply-keys' : 'apply-edit',
        {className: 'comic-btn comic-btn--cta', disabled: !!keys && (keys.batch ? !keys.batchValue.trim() : keys.editIndex >= 0 || !!keys.newValue.trim())}));
    }

    keyAction(label, action, icon, index, disabled = false) {
      const button = this.button(label, action, {className: 'comic-btn comic-btn--outline pc-key-icon',
        'data-pc-index': index, title: label, disabled});
      const graphic = this.el('span', {'aria-hidden': 'true'});
      this.dom.setSvgIcon(graphic, icon); button.replaceChildren(graphic);
      return button;
    }

    renderKeyManager(focus = '') {
      const editor = this.editor, keys = editor?.keys;
      if (!keys) return;
      const pane = this.el('div', {className: 'provider-config pc-editor pc-keys', 'data-pc-key-manager': ''});
      pane.appendChild(this.el('div', {className: 'pc-key-heading'}, [
        this.el('h3', {}, [keys.batch ? '批量导入 Key' : 'API Key 管理']),
        keys.batch ? this.el('span', {className: 'pc-key-count'}, [`${keys.items.length} 个 Key`])
          : this.button(this.keysHidden ? '显示' : '隐藏', 'toggle-keys', {'aria-label': this.keysHidden ? '显示完整 API Key' : '隐藏 API Key', 'aria-pressed': String(!this.keysHidden)})]));
      pane.appendChild(this.el('p', {className: 'pc-note'}, [keys.batch
        ? '每行一个，追加到当前列表并去重；不会覆盖已有 Key。'
        : '点击一行编辑，右侧移除；完成管理后返回条目，主保存后才生效。']));
      const status = this.el('p', {className: 'pc-note', role: 'status', 'aria-live': 'polite', 'data-pc-key-status': ''}, [keys.message]);
      pane.appendChild(status);
      if (keys.batch) {
        pane.appendChild(this.keyInput('batchValue', keys.batchValue, {rows: 7, 'aria-label': '批量导入 API Key', placeholder: '每行粘贴一个 Key'}));
      } else {
        const adding = this.keyInput('newValue', keys.newValue, {'aria-label': '新增 API Key', placeholder: '输入新 Key，按 Enter 添加', disabled: keys.editIndex >= 0});
        const add = this.button('添加', 'add-key', {disabled: keys.editIndex >= 0 || !keys.newValue.trim()});
        const batch = this.button('批量导入', 'batch-keys', {disabled: keys.editIndex >= 0});
        pane.appendChild(this.el('div', {className: 'pc-key-add'}, [adding, add, batch]));
        const list = this.el('ol', {className: 'pc-key-list', 'aria-label': 'API Key 列表'});
        keys.items.forEach((value, index) => {
          const editing = keys.editIndex === index;
          const line = this.el('li', {className: 'pc-key-row', 'data-pc-key-row': index});
          line.appendChild(this.el('span', {className: 'pc-key-number', 'aria-hidden': 'true'}, [String(index + 1).padStart(2, '0')]));
          line.appendChild(editing ? this.keyInput('editValue', keys.editValue, {'aria-label': `编辑 Key ${index + 1}`})
            : this.button(this.keysHidden ? '••••••••' : value, 'edit-key', {className: 'pc-key-value', 'data-pc-index': index,
              'aria-label': `编辑 Key ${index + 1}`, title: this.keysHidden ? undefined : value, disabled: keys.editIndex >= 0}));
          const actions = this.el('div', {className: 'pc-key-row-actions'});
          if (editing) actions.appendChild(this.keyAction('确认此 Key', 'save-key', 'check', index));
          actions.appendChild(this.keyAction(editing ? '取消此 Key 编辑' : `移除 Key ${index + 1}`,
            editing ? 'cancel-key-edit' : 'remove-key', 'x', index, !editing && keys.editIndex >= 0));
          line.appendChild(actions); list.appendChild(line);
        });
        if (!keys.items.length) pane.appendChild(this.el('p', {className: 'pc-key-empty'}, ['暂无 Key，从上方添加或批量导入。']));
        else pane.appendChild(list);
      }
      editor.body.replaceChildren(pane); this.renderEditorFooter();
      const selector = focus || (keys.batch ? '[data-pc-key-input="batchValue"]' : keys.editIndex >= 0 ? '[data-pc-key-input="editValue"]' : '[data-pc-key-input="newValue"]');
      (editor.body.querySelector(selector) || editor.body.querySelector('[data-pc-key-input="newValue"]'))?.focus();
    }

    keyMessage(message) {
      if (!this.editor?.keys) return;
      this.editor.keys.message = message;
      this.editor.body.querySelector('[data-pc-key-status]').textContent = message;
    }

    checkedKeyItems(items) {
      const values = [...new Set(items.map(item => item.trim()).filter(Boolean))];
      this.validate(values, this.fields(this.editor.entry).api_keys, 'api_keys');
      return values;
    }

    mutateKeys(action, index) {
      const keys = this.editor?.keys;
      if (!keys) return;
      if (action === 'cancel-key-batch') { keys.batch = false; keys.batchValue = ''; this.renderKeyManager(); return; }
      if (action === 'cancel-key-edit') { keys.editIndex = -1; keys.editValue = ''; this.renderKeyManager(); return; }
      if (action === 'batch-keys' && keys.editIndex < 0) {
        keys.batch = true; keys.batchValue = ''; keys.message = ''; this.renderKeyManager(); return;
      }
      if (action === 'edit-key' && keys.editIndex < 0 && keys.items[index] !== undefined) {
        keys.editIndex = index; keys.editValue = keys.items[index]; keys.message = ''; this.renderKeyManager(); return;
      }
      if (action === 'remove-key' && keys.editIndex < 0 && keys.items[index] !== undefined) {
        keys.items.splice(index, 1); keys.message = '已从管理草稿移除，取消密钥更改可恢复。';
        this.renderKeyManager(`[data-pc-action="remove-key"][data-pc-index="${Math.min(index, keys.items.length - 1)}"]`); return;
      }
      try {
        if (action === 'add-key' && keys.editIndex < 0 && keys.newValue.trim()) {
          const previous = keys.items.length;
          keys.items = this.checkedKeyItems([...keys.items, keys.newValue]); keys.newValue = '';
          keys.message = keys.items.length === previous ? '该 Key 已存在，未重复添加。' : '已添加到管理草稿。';
        } else if (action === 'save-key' && keys.editIndex >= 0) {
          if (!keys.editValue.trim()) { this.keyMessage('Key 不能为空，需要删除请使用移除按钮。'); return; }
          const values = [...keys.items]; values[keys.editIndex] = keys.editValue;
          keys.items = this.checkedKeyItems(values); keys.editIndex = -1; keys.editValue = ''; keys.message = '已更新管理草稿。';
        } else if (action === 'import-keys' && keys.batch && keys.batchValue.trim()) {
          const previous = keys.items.length;
          keys.items = this.checkedKeyItems([...keys.items, ...keys.batchValue.split(/\r\n?|\n/)]);
          keys.message = `已追加 ${keys.items.length - previous} 个 Key，重复与空白已忽略。`; keys.batchValue = ''; keys.batch = false;
        } else return;
        this.renderKeyManager();
      } catch (error) { this.keyMessage(error.message); }
    }

    finishKeys(accepted) {
      const editor = this.editor, keys = editor?.keys;
      if (!keys) return;
      if (accepted) {
        if (keys.editIndex >= 0 || keys.newValue.trim() || keys.batch) return;
        try {
          const values = this.checkedKeyItems(keys.items);
          if (JSON.stringify(values) !== JSON.stringify(editor.entry.values.api_keys || [])) {
            editor.entry.values.api_keys = values; delete editor.entry.secret_actions.api_keys;
          }
        } catch (error) { this.keyMessage(error.message); return; }
      }
      editor.keys = null; this.renderEditorBody(); this.renderEditorFooter(); this.touch();
      editor.body.querySelector('[data-pc-action="manage-keys"]')?.focus();
    }

    renderVisionProvider(container, entry, schema, id) {
      const value = String(entry.values.vision_provider_id ?? schema.default ?? '');
      if (this.visionManual) container.appendChild(this.control(value, {...schema, options: undefined}, 'vision_provider_id', true, id));
      else {
        const options = [this.el('option', {value: ''}, ['（留空禁用）'])];
        const providers = this.snapshot.vision_providers || [];
        if (value && !providers.some(item => item.id === value)) options.push(this.el('option', {value}, [`${value}（当前选择不在可用名单，保留）`]));
        for (const provider of providers) options.push(this.el('option', {value: provider.id}, [
          `${provider.label || provider.id}${provider.model ? ` · ${provider.model}` : ''}${provider.available === false ? '（实例暂不可用）' : ''}`]));
        const select = this.el('select', {id, className: 'comic-select', 'aria-label': schema.description || 'vision_provider_id',
          'data-pc-field': 'vision_provider_id', 'data-pc-scope': 'common'}, options);
        select.value = value;
        container.appendChild(select);
      }
      container.appendChild(this.el('div', {className: 'pc-actions'}, [
        this.button(this.visionRefreshing ? '正在刷新…' : '刷新视觉提供商', 'refresh-vision', {disabled: this.visionRefreshing}),
        this.button(this.visionManual ? '返回提供商选择' : '手填 ID（回退）', 'manual-vision')]));
      const warning = this.visionRefreshMessage || this.snapshot.vision_providers_warning;
      if (warning) container.appendChild(this.el('p', {className: 'pc-note', role: 'status'}, [warning]));
    }

    async refreshVision() {
      if (!this.writable || this.visionRefreshing) return;
      const token = ++this.visionRefreshToken;
      this.visionRefreshing = true; this.visionRefreshMessage = ''; this.render();
      try {
        const data = await this.bridge.get('webui/vision-providers');
        if (this.destroyed || token !== this.visionRefreshToken) return;
        if (!Array.isArray(data?.vision_providers) || data.vision_providers_available === false) throw new Error('Provider list unavailable');
        this.snapshot.vision_providers = copy(data.vision_providers);
        this.snapshot.vision_providers_available = data.vision_providers_available;
        this.snapshot.vision_providers_warning = data.vision_providers_warning;
        this.visionRefreshMessage = data.vision_providers_warning || '视觉提供商名单已刷新，其他草稿保持不变。';
      } catch (_) {
        if (this.destroyed || token !== this.visionRefreshToken) return;
        this.visionRefreshMessage = '刷新失败，当前选择、原名单和其他草稿均已保留。';
      } finally {
        if (!this.destroyed && token === this.visionRefreshToken) { this.visionRefreshing = false; this.render(); }
      }
    }

    invalidateCatalog(scope) {
      const state = this.catalogs[scope];
      ++state.token;
      state.loading = false; state.confirmation = null; state.models = []; state.search = ''; state.message = '';
      if (!this.destroyed) this.renderCatalog(scope);
    }

    renderCatalogField(container, entry, name, common) {
      const scope = common ? 'common' : 'edit';
      const capability = this.snapshot.model_catalog?.[entry.api_type];
      if (!common && !capability?.supported) {
        container.appendChild(this.el('p', {className: 'pc-note'}, [capability?.message || '此供应商暂未接入模型目录，可继续手填模型。']));
        return;
      }
      const state = this.catalogs[scope];
      state.field = name;
      state.host = this.el('div', {className: 'pc-catalog', 'data-pc-catalog': scope});
      container.appendChild(state.host); this.renderCatalog(scope);
    }

    renderCatalog(scope) {
      const state = this.catalogs[scope];
      if (!state.host) return;
      state.host.replaceChildren();
      const provider = this.draft?.common.values.vision_provider_id;
      state.host.appendChild(this.button(state.loading ? '正在拉取模型…' : '拉取模型', 'fetch-models', {
        'data-pc-scope': scope, disabled: state.loading || !this.writable || (scope === 'common' && !provider)}));
      if (state.message) state.host.appendChild(this.el('p', {className: 'pc-note', role: 'status'}, [state.message]));
      if (state.confirmation) {
        state.host.appendChild(this.el('div', {className: 'pc-target-confirm', role: 'group', 'aria-label': '确认模型查询目标'}, [
          this.el('p', {}, [`将向 ${state.confirmation.target} 发送该条目已保存的 Key。请确认目标可信；修改连接参数会取消本次确认。`]),
          this.button('确认目标并拉取', 'confirm-target', {'data-pc-scope': scope}),
          this.button('取消目标确认', 'cancel-target', {'data-pc-scope': scope})]));
      }
      if (state.models?.length) {
        const search = this.el('input', {type: 'search', 'aria-label': '搜索模型', className: 'comic-input',
          'data-pc-search': '', 'data-pc-scope': scope, autocomplete: 'off'});
        search.value = state.search || '';
        state.host.appendChild(search);
        state.results = this.el('div', {className: 'pc-model-results', role: 'group', 'aria-label': '可选模型'});
        state.host.appendChild(state.results); this.renderCatalogResults(scope);
      } else state.results = null;
    }

    renderCatalogResults(scope) {
      const state = this.catalogs[scope];
      if (!state.results) return;
      state.results.replaceChildren();
      const query = (state.search || '').trim().toLocaleLowerCase();
      state.models.forEach((model, index) => {
        if (!`${model.id} ${model.label}`.toLocaleLowerCase().includes(query)) return;
        state.results.appendChild(this.button(model.label && model.label !== model.id ? `${model.label} · ${model.id}` : model.id,
          'choose-model', {'data-pc-scope': scope, 'data-pc-model': index}));
      });
      if (!state.results.children.length) state.results.appendChild(this.el('p', {className: 'pc-note'}, ['没有匹配的模型。可继续手填。']));
    }

    catalogPayload(scope) {
      if (scope === 'common') return {kind: 'vision', provider_id: this.draft.common.values.vision_provider_id};
      const entry = this.editor?.entry;
      if (!entry || !this.snapshot.model_catalog?.[entry.api_type]?.supported) return null;
      const connectionFields = Object.fromEntries(Object.entries(this.fields(entry)).filter(([name]) => SECRET_FIELDS.has(name)));
      return {kind: 'entry', revision: this.draft.revision, entry: this.normalized(entry, connectionFields),
        common: this.normalized(this.draft.common, {proxy: this.snapshot.common_fields.proxy}, true), confirmed_target: false};
    }

    async fetchModels(scope, confirmed = false) {
      const state = this.catalogs[scope];
      if (!state || state.loading || !this.writable) return;
      let payload;
      try {
        if (confirmed) {
          if (!state.confirmation) return;
          payload = state.confirmation.payload;
          payload.confirmed_target = true;
        } else payload = this.catalogPayload(scope);
      } catch (_) { state.message = '连接参数无效，请检查 Key、地址和代理。'; this.renderCatalog(scope); return; }
      if (!payload || (scope === 'common' && !payload.provider_id)) return;
      // A generation token invalidates late replies without retaining a credential fingerprint.
      const token = ++state.token;
      state.confirmation = null; state.loading = true; state.message = ''; state.models = []; state.search = '';
      this.renderCatalog(scope);
      try {
        const data = await this.bridge.post('webui/providers/models', payload);
        if (this.destroyed || state.token !== token) return;
        if (data?.confirmation_required === true && scope === 'edit' && !confirmed && typeof data.target === 'string') {
          state.confirmation = {payload, target: data.target};
          return;
        }
        if (!Array.isArray(data?.models) || data.models.some(model => typeof model?.id !== 'string' || typeof model?.label !== 'string')) throw new Error('Invalid models');
        state.models = copy(data.models);
        state.message = [data.models.length ? `已获取 ${data.models.length} 个模型，点击才会填入；目录不保证模型支持当前任务。` : '未获取到模型，当前值保持不变，可继续手填。',
          data.warning || '', data.truncated ? '列表已截断，可手填未列出的模型。' : ''].filter(Boolean).join(' ');
      } catch (error) {
        if (this.destroyed || state.token !== token) return;
        const data = error?.data || error?.response?.data || {};
        if (data.reason === 'confirm_target' && scope === 'edit' && !confirmed && typeof data.target === 'string') {
          state.confirmation = {payload, target: data.target};
        } else {
          const status = [error?.status, error?.statusCode, error?.status_code, error?.response?.status].map(Number).find(value => value >= 100 && value <= 599);
          const text = String(error?.message || '');
          state.message = data.reason === 'revision' || status === 409 || /供应商配置已更新/.test(text)
            ? '配置版本已变化，请先应用或取消编辑，再重新加载配置；当前模型未更改。'
            : '模型拉取失败，请检查连接、凭据和提供商状态后重试；当前模型保持不变。';
        }
      } finally {
        payload = null;
        if (!this.destroyed && state.token === token) { state.loading = false; this.renderCatalog(scope); }
      }
    }

    chooseModel(scope, index) {
      const state = this.catalogs[scope];
      const model = state.models?.[index];
      const entry = scope === 'common' ? this.draft.common : this.editor?.entry;
      if (!model || !entry) return;
      entry.values[state.field] = model.id;
      const container = scope === 'common' ? this.fieldset : this.editor.body;
      const control = container.querySelector(`[data-pc-field="${state.field}"]`);
      if (control) control.value = model.id;
      this.updateConditions(container, entry, scope === 'common' ? this.snapshot.common_fields : this.fields(entry));
      this.touch();
    }

    renderSecret(container, entry, name, schema, common, id) {
      const meta = this.secrets(entry, common)[name];
      const action = entry.secret_actions[name] || {mode: 'keep'};
      const label = schema.description || name;
      const mode = this.el('select', {id, className: 'comic-select', 'aria-label': `${label}操作`,
        'data-pc-secret-mode': name, 'data-pc-scope': common ? 'common' : 'edit'}, [
        this.el('option', {value: 'keep'}, [meta?.present ? `保持原值${name === 'api_keys' ? `（${meta.count || 0} 个）` : '（已配置）'}` : '保持（未配置）']),
        this.el('option', {value: 'replace'}, ['替换']), this.el('option', {value: 'clear'}, ['清空'])]);
      mode.value = action.mode;
      container.appendChild(mode);
      if (action.mode === 'replace') {
        container.appendChild(this.control(action.value ?? (schema.type === 'list' ? [] : ''), schema, name, common, `${id}-replacement`, true));
        container.appendChild(this.el('p', {className: 'pc-note'}, [name === 'api_keys' ? '每行一个 Key；新值仅保存在当前页面内存中。' : '新地址仅保存在当前页面内存中。']));
      } else if (action.mode === 'clear') container.appendChild(this.el('p', {className: 'pc-note'}, ['将在主保存后清空此字段。']));
    }

    updateConditions(container, entry, fields) {
      for (const node of container.querySelectorAll('[data-pc-field-wrap]')) {
        const condition = fields[node.dataset.pcFieldWrap]?.condition;
        node.hidden = !!condition && !Object.entries(condition).every(([name, expected]) =>
          (entry.values[name] ?? fields[name]?.default) === expected);
      }
    }

    input(event) {
      if (!this.writable) return;
      const node = event.target;
      if (node.dataset.pcEnabled != null) {
        const row = this.draft.entries[Number(node.dataset.pcEnabled)];
        if (row && this.supported(row) && row.values.enabled !== node.checked) {
          row.values.enabled = node.checked; this.render();
        }
        return;
      }
      const keyInput = node.dataset.pcKeyInput, keys = this.editor?.keys;
      if (keys) {
        if (['newValue', 'editValue', 'batchValue'].includes(keyInput) && keys[keyInput] !== node.value) {
          keys[keyInput] = node.value;
          const add = this.editor.body.querySelector('[data-pc-action="add-key"]');
          if (add) add.disabled = keys.editIndex >= 0 || !keys.newValue.trim();
          this.renderEditorFooter();
        }
        return;
      }
      const common = node.dataset.pcScope === 'common';
      const scope = common ? 'common' : 'edit';
      if (node.dataset.pcSearch != null) {
        if (this.catalogs[scope].search === node.value) return;
        this.catalogs[scope].search = node.value;
        this.renderCatalogResults(scope); return;
      }
      const entry = common ? this.draft.common : this.editor?.entry;
      if (!entry) return;
      const fields = common ? this.snapshot.common_fields : this.fields(entry);
      const modeField = node.dataset.pcSecretMode;
      if (modeField && event.type === 'change') {
        if (!SECRET_FIELDS.has(modeField) || !own(fields, modeField)) return;
        const mode = node.value;
        if (!['keep', 'replace', 'clear'].includes(mode)) return;
        const previous = entry.secret_actions[modeField];
        entry.secret_actions[modeField] = mode === 'replace'
          ? {mode, value: previous?.mode === 'replace' ? previous.value : fields[modeField].type === 'list' ? [] : ''} : {mode};
        delete entry.values[modeField];
        this.invalidateCatalog(scope);
        if (common && modeField === 'proxy') this.invalidateCatalog('edit');
        if (common) this.render(); else this.renderEditorBody();
        this.touch(); return;
      }
      const name = node.dataset.pcField;
      if (!name || !own(fields, name)) return;
      const schema = fields[name];
      const value = schema.type === 'bool' ? node.checked
        : schema.type === 'list' ? node.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
        : schema.type === 'file' ? (String(node.value).trim() ? [String(node.value).trim()] : [])
        : node.value;
      const current = node.dataset.pcSecret === 'true' ? entry.secret_actions[name]?.value : entry.values[name];
      const keyList = name === 'api_keys' && schema.type !== 'string';
      const next = keyList ? [...new Set(value)] : value;
      // Text inputs also emit change on blur; do not replace a button mid-click.
      if (JSON.stringify(current) === JSON.stringify(next)) return;
      if (keyList) {
        // 省略显示的占位值不是真实 Key，忽略对其的程序化编辑。
        if (!this.keysVisible(entry) || (this.keysHidden && (entry.values.api_keys || []).length > 0 && value === '••••••••')) return;
        entry.values.api_keys = [...new Set(value)];
        delete entry.secret_actions.api_keys;
      } else if (node.dataset.pcSecret === 'true') entry.secret_actions[name] = {mode: 'replace', value};
      else entry.values[name] = value;
      if (['api_keys', 'api_base', 'proxy', 'vision_provider_id'].includes(name)) {
        this.invalidateCatalog(scope);
        if (common && name === 'proxy') this.invalidateCatalog('edit');
      }
      this.updateConditions(common ? this.fieldset : this.editor.body, entry, fields);
      this.touch();
    }

    async click(event) {
      const button = event.target.closest('[data-pc-action]');
      if (!button || button.disabled || this.destroyed) return;
      const action = button.dataset.pcAction;
      if (action === 'tab') {
        this.tab = button.dataset.pcTab; this.render();
        this.root.querySelector(`[data-pc-tab="${this.tab}"]`)?.focus(); return;
      }
      if (action === 'reload') {
        if (this.loading || this.saving) return;
        const epoch = this.epoch;
        if (this.dirty && !await this.modal.confirm({title: '重新加载供应商配置', content: '将丢弃当前未保存草稿（包括新密钥），是否继续？', danger: true})) return;
        if (!this.destroyed && epoch === this.epoch) await this.load();
        return;
      }
      if (action === 'cancel-edit') { this.editor?.cancel(); return; }
      if (action === 'cancel-keys') { this.finishKeys(false); return; }
      if (action === 'cancel-key-edit' || action === 'cancel-key-batch') { this.mutateKeys(action); return; }
      if (action === 'toggle-keys') {
        this.keysHidden = !this.keysHidden;
        if (this.editor?.keys) { this.renderKeyManager(); return; }
        this.renderEditorBody();
        return;
      }
      if (!this.writable) return;
      const scope = button.dataset.pcScope || 'edit';
      if (action === 'manage-keys') { this.openKeys(); return; }
      if (action === 'apply-keys') { this.finishKeys(true); return; }
      if (this.editor?.keys) { this.mutateKeys(action, Number(button.dataset.pcIndex)); return; }
      if (action === 'refresh-vision') { await this.refreshVision(); return; }
      if (action === 'manual-vision') {
        this.visionManual = !this.visionManual;
        this.render(); return;
      }
      if (action === 'fetch-models') { await this.fetchModels(scope); return; }
      if (action === 'confirm-target') { await this.fetchModels(scope, true); return; }
      if (action === 'cancel-target') { this.invalidateCatalog(scope); return; }
      if (action === 'choose-model') { this.chooseModel(scope, Number(button.dataset.pcModel)); return; }
      const index = Number(button.dataset.pcIndex);
      if (action === 'save') { await this.save(); return; }
      if (action === 'upload-credential') {
        const target = button.dataset.pcCredTarget;
        (this.editor?.body || this.fieldset || root).querySelector(`[data-pc-file-picker="${target}"]`)?.click();
        return;
      }
      if (action === 'add') {
        const type = this.root.querySelector('[data-pc-new-type]')?.value;
        if (!own(this.snapshot.templates, type) || this.draft.entries.length >= 100) return;
        const values = {};
        for (const [name, schema] of Object.entries(this.snapshot.templates[type].fields)) {
          if (name !== 'api_keys' && own(schema, 'default')) values[name] = copy(schema.default);
        }
        if (own(this.snapshot.templates[type].fields, 'enabled')) values.enabled = false;
        this.openEditor({id: null, api_type: type, values, secret_actions: {}}, null); return;
      }
      if (action === 'edit') { if (this.draft.entries[index] && this.supported(this.draft.entries[index])) this.openEditor(this.draft.entries[index], index); return; }
      if (action === 'apply-edit') { this.applyEditor(); return; }
      if (action === 'delete') {
        const entry = this.draft.entries[index];
        if (!entry) return;
        const epoch = this.epoch;
        if (!await this.modal.confirm({title: `删除条目 ${index + 1}`, content: '主保存后将永久删除本条配置及其密钥；其他同类型条目不受影响。是否删除？', danger: true})) return;
        if (!this.writable || epoch !== this.epoch || this.draft.entries[index] !== entry) return;
        this.draft.entries.splice(index, 1); this.render(); return;
      }
      if (action === 'auto') {
        this.draft.provider_polling = []; this.message = '已恢复自动轮询，主保存后生效。'; this.render(); return;
      }
      if (action === 'up' || action === 'down') { this.movePolling(index, index + (action === 'up' ? -1 : 1)); return; }
      if (action === 'add-poll') {
        const type = button.dataset.pcType;
        if (!this.snapshot.provider_types.some(item => item.id === type)) return;
        const order = this.pollingOrder();
        if (!order.includes(type)) order.push(type);
        this.draft.provider_polling = order;
        this.message = '已切换为手动轮询；未参与类型将被跳过，主保存后生效。'; this.render(); return;
      }
      if (action === 'remove-poll' && this.draft.provider_polling.length) {
        this.draft.provider_polling.splice(index, 1);
        this.message = this.draft.provider_polling.length ? '已移出轮询，配置条目仍保留。' : '轮询列表为空，已恢复自动跟随有效配置。';
        this.render();
      }
    }

    openEditor(entry, index) {
      if (this.editor || !this.writable) return;
      const editor = {entry: copy(entry), index, body: null};
      this.editor = editor;
      this.touch();
      this.modal.openCustom({title: `${index == null ? '新增' : `编辑条目 ${index + 1}`} · ${this.label(entry.api_type)}`, variant: 'parameters',
        onClose: () => {
          this.invalidateCatalog('edit');
          this.editorCleanups.splice(0).forEach(cleanup => cleanup());
          this.editor = null;
          editor.entry = null; editor.keys = null;
          if (!this.destroyed) this.render();
        },
        renderBody: body => { editor.body = body; this.bind(body, this.editorCleanups); this.renderEditorBody(); },
        renderFooter: (footer, accept, cancel) => {
          editor.accept = accept; editor.cancel = cancel; editor.footer = footer;
          this.bind(footer, this.editorCleanups);
          this.renderEditorFooter();
        }
      });
    }

    renderEditorBody() {
      const {entry, body} = this.editor;
      body.replaceChildren();
      const container = this.el('div', {className: 'provider-config pc-editor'});
      this.editor.feedback = this.el('p', {role: 'status', className: 'pc-status'}, ['应用到草稿不会立即保存；关闭或取消将丢弃本次弹窗编辑。']);
      container.appendChild(this.editor.feedback);
      container.appendChild(this.el('p', {className: 'pc-note'}, ['未配置字段仅展示模板默认值，修改才写入；旧字段仍由服务器保留。']));
      const unknown = this.metadata(entry)?.unknown_fields || [];
      if (unknown.length) container.appendChild(this.el('p', {className: 'pc-note'}, [`旧字段仅由服务器保留，不显示或提交其值：${unknown.join('、')}`]));
      const fields = this.el('fieldset', {className: 'pc-fieldset', disabled: !this.writable});
      this.renderFields(fields, entry, this.fields(entry));
      container.appendChild(fields); body.appendChild(container);
    }

    normalized(entry, fields, common = false) {
      const values = {};
      const secret_actions = {};
      for (const [name, schema] of Object.entries(fields || {})) {
        if (name === 'api_keys') {
          if (own(entry.values, name)) {
            if (schema.type === 'string') {
              // 单凭证渠道（如 vertex）：api_keys 为字符串
              const key = String(entry.values[name] ?? '').trim();
              this.validate(key, schema, name);
              values[name] = key;
            } else {
              const keys = [...new Set(entry.values[name].map(value => value.trim()).filter(Boolean))];
              this.validate(keys, schema, name);
              values[name] = keys;
            }
          }
          continue;
        }
        const secret = SECRET_FIELDS.has(name) && (this.secrets(entry, common)[name]?.present || own(entry.secret_actions, name));
        if (secret) {
          const action = entry.secret_actions[name] || {mode: 'keep'};
          secret_actions[name] = copy(action);
          if (action.mode === 'replace') this.validate(action.value, schema, name);
        } else if (own(entry.values, name)) {
          let value = entry.values[name];
          if (schema.type === 'int' || schema.type === 'float') {
            if (typeof value === 'string' && !value.trim()) throw new Error(`${schema.description || name}：请输入数字。`);
            value = Number(value);
          }
          const original = common ? this.snapshot.common.values : this.metadata(entry)?.values;
          if (!original || !own(original, name) || JSON.stringify(value) !== JSON.stringify(original[name])) this.validate(value, schema, name);
          values[name] = value;
        }
      }
      return common ? {values, secret_actions} : {id: entry.id, api_type: entry.api_type, values, secret_actions};
    }

    validate(value, schema, name) {
      const fail = message => { throw new Error(`${schema.description || name}：${message}`); };
      if (schema.type === 'bool' && typeof value !== 'boolean') fail('请选择启用或禁用。');
      if (schema.type === 'string' && (typeof value !== 'string' || value.length > 16384)) fail('文本过长或类型无效。');
      if (schema.type === 'int' || schema.type === 'float') {
        if (!Number.isFinite(value) || (schema.type === 'int' && !Number.isInteger(value))) fail('请输入有效数字。');
        if (schema.slider && ((schema.slider.min != null && value < schema.slider.min) || (schema.slider.max != null && value > schema.slider.max))) fail('数字超出允许范围。');
      }
      if (schema.type === 'list') {
        if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) fail('请每行输入一项。');
        if (name === 'api_keys' && (value.length > 200 || value.some(item => item.length > 8192))) fail('最多 200 个 Key，每项最多 8192 字符。');
        if (value.some(item => item.length > 16384)) fail('列表项过长。');
      }
      if (schema.type === 'file') {
        if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) fail('凭证内容无效。');
        if (value.some(item => item.length > 16384)) fail('凭证内容过长。');
      }
      if (Array.isArray(schema.options) && (Array.isArray(value) ? value.some(item => !schema.options.includes(item)) : !schema.options.includes(value))) fail('请选择允许的选项。');
    }

    applyEditor() {
      if (!this.editor || this.editor.keys) return;
      try {
        const entry = this.normalized(this.editor.entry, this.fields(this.editor.entry));
        if (this.editor.index == null) this.draft.entries.push(entry);
        else this.draft.entries[this.editor.index] = entry;
        this.editor.accept();
      } catch (error) { this.editor.feedback.textContent = error.message; }
    }

    async save() {
      if (!this.writable || this.conflict || this.editor || !this.dirty) return;
      let payload;
      try {
        payload = {revision: this.draft.revision, provider_polling: [...this.draft.provider_polling],
          entries: this.draft.entries.map(entry => this.supported(entry) ? this.normalized(entry, this.fields(entry))
            : {id: entry.id, api_type: entry.api_type, values: {}, secret_actions: {}}),
          common: this.normalized(this.draft.common, this.snapshot.common_fields, true)};
      } catch (error) { this.message = error.message; this.render(); return; }
      const epoch = ++this.epoch;
      this.saving = true; this.message = '正在保存，请稍候…'; this.render();
      let saved = false;
      try {
        const snapshot = await this.bridge.post('webui/providers', payload);
        if (this.destroyed || epoch !== this.epoch) return;
        this.adopt(snapshot);
        this.message = snapshot.requires_reload ? '配置需要重载插件恢复；当前不可继续编辑或生成。' : '供应商配置已保存，运行时已更新；未自动重载插件。';
        saved = true;
      } catch (error) {
        if (this.destroyed || epoch !== this.epoch) return;
        const data = error?.data || error?.response?.data || {};
        const reason = data.reason || error?.reason;
        const text = String(error?.message || '');
        const busy = reason === 'busy' || text.includes('生成任务正在运行');
        const status = [error?.status, error?.statusCode, error?.status_code, error?.code, error?.response?.status].map(Number).find(value => value >= 100 && value <= 599);
        this.conflict = !busy && (reason === 'revision' || status === 409 || /\b409\b|conflict|revision|配置.*(?:变更|冲突|过期|更新)/i.test(text));
        if (data.requires_reload || error?.requires_reload || (status === 503 && text.includes('重载'))) this.snapshot.requires_reload = true;
        this.message = this.snapshot.requires_reload ? '保存未完成，草稿保留。运行时恢复失败，请重载插件。' : busy
          ? '生成任务正在运行，草稿完整保留；任务结束后可再次保存。' : this.conflict
            ? '配置版本冲突（409），草稿完整保留。请先重新加载，不能直接覆盖服务器配置。'
            : (status === 400 || status === 503) && text ? `${text}；草稿已保留。`
            : '保存失败，草稿完整保留；请稍后重试。';
      } finally {
        if (!this.destroyed && epoch === this.epoch) { this.saving = false; this.render(); }
      }
      if (saved && !this.destroyed) {
        try { await this.onSaved(); }
        catch (_) {
          if (!this.destroyed && epoch === this.epoch) { this.message = '供应商配置已保存，但工作台能力刷新失败；请稍后刷新。'; this.render(); }
        }
      }
    }

    keydown(event) {
      const keys = this.editor?.keys;
      if (keys) {
        if (event.isComposing) return;
        if (event.key === 'Escape') {
          event.preventDefault(); event.stopPropagation?.();
          if (keys.batch) this.mutateKeys('cancel-key-batch');
          else if (keys.editIndex >= 0) this.mutateKeys('cancel-key-edit');
          else this.finishKeys(false);
          return;
        }
        const name = event.target.dataset.pcKeyInput;
        if (event.key === 'Enter' && this.writable && ['newValue', 'editValue'].includes(name)) {
          event.preventDefault(); event.stopPropagation?.();
          this.mutateKeys(name === 'newValue' ? 'add-key' : 'save-key');
        }
        return;
      }
      const tab = event.target.closest('[data-pc-tab]');
      if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const tabs = ['entries', 'polling', 'common'];
        const current = tabs.indexOf(this.tab);
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (current + (event.key === 'ArrowRight' ? 1 : -1) + 3) % 3;
        this.tab = tabs[index]; this.render(); this.root.querySelector(`[data-pc-tab="${this.tab}"]`)?.focus();
      }
      const handle = event.target.closest('[data-pc-action="handle"]');
      if (handle && this.writable && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
        event.preventDefault();
        const index = Number(handle.dataset.pcIndex);
        this.movePolling(index, index + (event.key === 'ArrowUp' ? -1 : 1));
      }
      if (event.key === 'Escape' && this.pointer) { event.preventDefault(); this.releasePointer(); }
    }

    movePolling(from, to) {
      if (!this.writable) return;
      const order = this.pollingOrder();
      if (from < 0 || to < 0 || from >= order.length || to >= order.length || from === to) return;
      order.splice(to, 0, order.splice(from, 1)[0]);
      this.draft.provider_polling = order;
      this.message = '已更新手动轮询顺序，主保存后生效；未参与的供应商将被跳过。';
      this.render();
      this.root.querySelector(`[data-pc-action="handle"][data-pc-index="${to}"]`)?.focus();
    }

    pointerDown(event) {
      const handle = event.target.closest('[data-pc-action="handle"]');
      if (!handle || !this.writable || this.pointer || event.isPrimary === false || (event.button != null && event.button !== 0)) return;
      event.preventDefault();
      const from = Number(handle.dataset.pcIndex);
      this.pointer = {id: event.pointerId, handle, from, to: from,
        rows: [...this.root.querySelectorAll('[data-pc-poll-index]')]};
      handle.setPointerCapture?.(event.pointerId);
      handle.classList.add('pc-dragging');
    }
    pointerMove(event) {
      const pointer = this.pointer;
      if (!pointer || pointer.id !== event.pointerId) return;
      event.preventDefault();
      const other = pointer.rows.filter((_, index) => index !== pointer.from);
      pointer.to = other.filter(row => { const rect = row.getBoundingClientRect(); return event.clientY > rect.top + rect.height / 2; }).length;
      pointer.rows.forEach((row, index) => row.classList.toggle('pc-drop-target', index === pointer.to));
    }
    pointerEnd(event) {
      const pointer = this.pointer;
      if (!pointer || pointer.id !== event.pointerId) return;
      this.releasePointer();
      this.movePolling(pointer.from, pointer.to);
    }
    releasePointer() {
      const pointer = this.pointer;
      if (!pointer) return;
      this.pointer = null;
      pointer.handle.classList.remove('pc-dragging');
      pointer.rows.forEach(row => row.classList.remove('pc-drop-target'));
      if (pointer.handle.hasPointerCapture?.(pointer.id)) pointer.handle.releasePointerCapture(pointer.id);
    }

    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      ++this.epoch;
      ++this.visionRefreshToken;
      this.invalidateCatalog('edit');
      this.invalidateCatalog('common');
      this.releasePointer();
      this.editor?.cancel?.();
      this.cleanups.splice(0).forEach(cleanup => cleanup());
      this.editorCleanups.splice(0).forEach(cleanup => cleanup());
      this.editor = null; this.draft = null; this.snapshot = null; this.baseline = null;
      this.root.replaceChildren();
      this.fieldset = null; this.panel = null;
    }
  }
  window.StudioProviderConfigView = StudioProviderConfigView;
})();
