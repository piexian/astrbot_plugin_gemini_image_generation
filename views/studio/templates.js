/* 提示词广场：独立视图，只通过注入的 bridge / dom / modal / t 工作，不 import app.js。 */
(() => {
  'use strict';

  // 参考图选项：value 为字段取值，label 以中文原文作 i18n key。
  const REFERENCE_VALUES = ['none', 'optional', 'required'];

  class StudioTemplatesView {
    constructor({
      root,
      bridge,
      dom,
      modal,
      t,
      onUsed = () => {},
      onSaved = async () => {}
    }) {
      this.root = root;
      this.bridge = bridge;
      this.dom = dom;
      this.modal = modal;
      this.t = typeof t === 'function' ? t : (key => key);
      this.onUsed = onUsed;
      this.onSaved = onSaved;
      this.prefix = 'studio-templates';
      this.templates = [];
      this.loaded = false;
      this.loading = false;
      this.loadFailed = false;
      this.saving = false;
      this.destroyed = false;
      this.epoch = 0;
      this.message = this.t('打开提示词广场后加载。');
      this.query = '';
      this.tag = '';
      this.cleanups = [];
      this.bind(root, this.cleanups);
      this.render();
    }

    el(tag, attrs = {}, children = []) { return this.dom.el(tag, attrs, children); }

    get available() {
      return !!this.bridge && (!this.bridge.isAvailable || this.bridge.isAvailable());
    }

    get writable() {
      return !this.destroyed && this.loaded && !this.loading && !this.saving && this.available;
    }

    bind(container, cleanups) {
      const handlers = {
        click: event => this.click(event),
        input: event => this.input(event),
        change: event => this.input(event),
        keydown: event => this.keydown(event)
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

    async load() {
      if (this.destroyed || this.loading || this.saving) return;
      if (!this.available) {
        this.message = this.t('桥接不可用，请在 AstrBot Dashboard 中打开。');
        this.render();
        return;
      }
      const epoch = ++this.epoch;
      this.loading = true;
      this.message = this.t('正在加载提示词模板…');
      this.render();
      try {
        const payload = await this.bridge.get('webui/templates');
        if (this.destroyed || epoch !== this.epoch) return;
        this.templates = Array.isArray(payload?.templates) ? payload.templates : [];
        this.loaded = true;
        this.loadFailed = false;
        this.message = this.t('已加载 {v0} 个模板。', {v0: this.templates.length});
      } catch (_) {
        if (this.destroyed || epoch !== this.epoch) return;
        this.loadFailed = true;
        this.message = this.t('加载失败，请重新打开本页签。');
      } finally {
        if (!this.destroyed && epoch === this.epoch) { this.loading = false; this.render(); }
      }
    }

    visible() {
      const words = String(this.query || '').trim().toLowerCase();
      const terms = words ? words.split(/\s+/).filter(Boolean) : [];
      return this.templates.filter(item => {
        if (this.tag && !(item.tags || []).includes(this.tag)) return false;
        if (!terms.length) return true;
        const haystack = [item.name, item.ai_description, (item.tags || []).join(' ')]
          .join(' ').toLowerCase();
        return terms.every(term => haystack.includes(term));
      });
    }

    tags() {
      const seen = [];
      for (const item of this.templates) {
        for (const tag of item.tags || []) if (!seen.includes(tag)) seen.push(tag);
      }
      return seen;
    }

    click(event) {
      const target = event.target.closest?.('[data-tpl-action]');
      if (!target || this.destroyed) return;
      const action = target.dataset.tplAction;
      const id = target.dataset.tplId;
      event.preventDefault();
      if (action === 'refresh') { void this.load(); return; }
      if (action === 'create') { this.openEditor({}); return; }
      if (action === 'edit') { this.openEditor(this.byId(id)); return; }
      if (action === 'copy') { this.openEditor(this.byId(id), {copy: true}); return; }
      if (action === 'use') { this.use(this.byId(id)); return; }
      if (action === 'restore') { void this.mutate({action: 'restore', id}); return; }
      if (action === 'delete') { void this.confirmDelete(this.byId(id)); return; }
      if (action === 'toggle-ai') { void this.toggleAi(this.byId(id)); return; }
      if (action === 'export') { void this.exportPackage(); return; }
      if (action === 'import') { this.openImport(); return; }
    }

    input(event) {
      const field = event.target.dataset?.tplField;
      if (!field || this.destroyed) return;
      if (field === 'query') { this.query = event.target.value; this.renderGrid(); return; }
      if (field === 'tag') { this.tag = event.target.value; this.renderGrid(); }
    }

    keydown(event) {
      if (event.key !== 'Enter' || this.destroyed) return;
      const field = event.target.dataset?.tplField;
      if (field === 'query') event.preventDefault();
    }

    byId(id) { return this.templates.find(item => item.id === id) || null; }

    referenceOptions() {
      const text = value => (value === 'required' ? this.t('需要参考图')
        : value === 'optional' ? this.t('可选参考图') : this.t('不需要参考图'));
      return REFERENCE_VALUES.map(value => ({value, label: text(value)}));
    }

    referenceLabel(value) {
      const hit = this.referenceOptions().find(item => item.value === value);
      return hit ? hit.label : this.referenceOptions()[0].label;
    }

    use(template) {
      if (!template) return;
      this.onUsed({prompt: template.content, template});
      this.message = this.t('已把「{v0}」填入工作台。', {v0: template.name});
      this.render();
    }

    async confirmDelete(template) {
      if (!template || !this.writable) return;
      const ok = await this.modal.confirm({
        title: this.t('删除模板'),
        content: this.t('确定删除「{v0}」？此操作不可撤销。', {v0: template.name}),
        confirmText: this.t('删除'),
        danger: true
      });
      if (ok) await this.mutate({action: 'delete', id: template.id});
    }

    async toggleAi(template) {
      if (!template || !this.writable) return;
      await this.mutate({
        action: 'set_ai_enabled',
        id: template.id,
        ai_enabled: !template.ai_enabled
      });
    }

    async mutate(payload) {
      if (!this.available || this.saving) return;
      this.saving = true;
      this.message = this.t('正在保存…');
      this.render();
      try {
        await this.bridge.post('webui/templates', payload);
        await this.onSaved();
        await this.load();
      } catch (_) {
        if (!this.destroyed) {
          this.message = this.t('保存失败，请稍后重试。');
          this.render();
        }
      } finally {
        if (!this.destroyed) { this.saving = false; this.render(); }
      }
    }

    async exportPackage() {
      if (!this.available) return;
      try {
        const payload = await this.bridge.get('webui/templates/export');
        const text = JSON.stringify(payload, null, 2);
        this.openTextDialog(this.t('导出模板包'), text, {readOnly: true});
      } catch (_) {
        this.message = this.t('导出失败，请稍后重试。');
        this.render();
      }
    }

    openImport() {
      this.openTextDialog(this.t('导入模板包'), '', {
        readOnly: false,
        submit: async value => {
          let parsed = null;
          try { parsed = JSON.parse(value); } catch (_) { parsed = null; }
          if (!parsed) { this.message = this.t('模板包不是合法的 JSON。'); this.render(); return; }
          await this.mutate({action: 'import', package: parsed});
        }
      });
    }

    openTextDialog(title, value, {readOnly = true, submit = null} = {}) {
      this.modal.openCustom({
        title,
        renderBody: body => {
          const area = this.el('textarea', {
            className: 'comic-input comic-textarea tpl-package-input',
            rows: 16,
            readOnly: readOnly ? 'readonly' : null,
            ...(readOnly ? {} : {'data-tpl-package': '1'})
          });
          area.value = value || '';
          body.appendChild(area);
        },
        renderFooter: (footer, confirm, cancel) => {
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline', onClick: cancel
          }, [this.t('关闭')]));
          if (submit) {
            footer.appendChild(this.el('button', {
              type: 'button',
              className: 'comic-btn comic-btn--cta',
              onClick: async () => {
                const area = this.root.querySelector('[data-tpl-package]')
                  || document.querySelector('[data-tpl-package]');
                await submit(area ? area.value : '');
                confirm();
              }
            }, [this.t('导入')]));
          }
        }
      });
    }

    openEditor(template, {copy = false} = {}) {
      if (!template) return;
      const draft = {
        id: copy ? '' : (template.id || ''),
        name: template.name || '',
        ai_description: template.ai_description || '',
        content: template.content || '',
        reference: template.reference || 'none',
        tags: (template.tags || []).join(' '),
        defaults_resolution: template.defaults?.resolution || '',
        defaults_aspect_ratio: template.defaults?.aspect_ratio || ''
      };
      this.modal.openCustom({
        title: copy ? this.t('复制模板') : (draft.id ? this.t('编辑模板') : this.t('新建模板')),
        renderBody: body => {
          body.appendChild(this.field('name', this.t('名称'), draft.name, {max: 40}));
          body.appendChild(this.field('ai_description', this.t('一句话说明'), draft.ai_description, {max: 80}));
          body.appendChild(this.textarea('content', this.t('提示词正文'), draft.content));
          body.appendChild(this.select('reference', this.t('参考图'), draft.reference,
            this.referenceOptions()));
          body.appendChild(this.field('tags', this.t('标签（空格分隔）'), draft.tags));
          body.appendChild(this.field('defaults_resolution', this.t('默认分辨率（如 2K）'), draft.defaults_resolution));
          body.appendChild(this.field('defaults_aspect_ratio', this.t('默认比例（如 16:9）'), draft.defaults_aspect_ratio));
        },
        renderFooter: (footer, confirm, cancel) => {
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline', onClick: cancel
          }, [this.t('取消')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--cta',
            onClick: async () => {
              const payload = this.collectEditor(draft);
              if (!payload) { this.message = this.t('名称和正文不能为空。'); this.render(); return; }
              confirm();
              await this.mutate(payload);
            }
          }, [this.t('保存')]));
        }
      });
    }

    field(name, label, value, {max = 0} = {}) {
      return this.el('div', {className: 'tpl-field'}, [
        this.el('label', {className: 'tpl-field-label', for: `${this.prefix}-${name}`}, [label]),
        this.el('input', {
          type: 'text',
          id: `${this.prefix}-${name}`,
          className: 'comic-input',
          'data-tpl-editor': name,
          maxlength: max || null,
          value: value || ''
        })
      ]);
    }

    textarea(name, label, value) {
      return this.el('div', {className: 'tpl-field'}, [
        this.el('label', {className: 'tpl-field-label', for: `${this.prefix}-${name}`}, [label]),
        this.el('textarea', {
          id: `${this.prefix}-${name}`,
          className: 'comic-input comic-textarea',
          rows: 8,
          'data-tpl-editor': name
        }, [value || ''])
      ]);
    }

    select(name, label, value, options) {
      const select = this.el('select', {
        id: `${this.prefix}-${name}`,
        className: 'comic-select',
        'data-tpl-editor': name
      }, options.map(option => this.el('option', {value: option.value, selected: option.value === value}, [option.label])));
      return this.el('div', {className: 'tpl-field'}, [
        this.el('label', {className: 'tpl-field-label', for: `${this.prefix}-${name}`}, [label]),
        select
      ]);
    }

    collectEditor(draft) {
      const read = name => {
        const node = document.querySelector(`[data-tpl-editor="${name}"]`);
        return node ? node.value : '';
      };
      const name = String(read('name') || '').trim();
      const content = String(read('content') || '').trim();
      if (!name || !content) return null;
      const defaults = {};
      const resolution = String(read('defaults_resolution') || '').trim();
      const aspectRatio = String(read('defaults_aspect_ratio') || '').trim();
      if (resolution) defaults.resolution = resolution;
      if (aspectRatio) defaults.aspect_ratio = aspectRatio;
      const tags = String(read('tags') || '').split(/\s+/).filter(Boolean);
      const template = {
        name,
        content,
        ai_description: String(read('ai_description') || '').trim(),
        reference: read('reference') || 'none',
        tags,
        ...(Object.keys(defaults).length ? {defaults} : {})
      };
      if (draft.id) return {action: 'update', id: draft.id, changes: template};
      return {action: 'create', template};
    }

    render() {
      if (this.destroyed) return;
      const status = this.el('p', {
        className: 'tpl-status', role: 'status', 'aria-live': 'polite'
      }, [this.message]);

      const search = this.el('input', {
        type: 'text', className: 'comic-input tpl-search',
        'data-tpl-field': 'query', value: this.query,
        placeholder: this.t('搜索名称、说明或标签'),
        'aria-label': this.t('搜索名称、说明或标签')
      });

      const tagSelect = this.el('select', {
        className: 'comic-select', 'data-tpl-field': 'tag',
        'aria-label': this.t('按标签筛选')
      }, [
        this.el('option', {value: ''}, [this.t('全部标签')]),
        ...this.tags().map(tag => this.el('option', {value: tag, selected: tag === this.tag}, [tag]))
      ]);

      const actions = this.el('div', {className: 'tpl-actions'}, [
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--cta',
          'data-tpl-action': 'create', disabled: !this.writable
        }, [this.t('新建模板')]),
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--outline',
          'data-tpl-action': 'import', disabled: !this.writable
        }, [this.t('导入')]),
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--outline',
          'data-tpl-action': 'export', disabled: !this.loaded
        }, [this.t('导出')]),
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--outline',
          'data-tpl-action': 'refresh', disabled: this.loading
        }, [this.t('刷新')])
      ]);

      const grid = this.el('div', {className: 'tpl-grid', id: 'tpl-grid'});
      this.root.replaceChildren(
        this.el('div', {className: 'tpl-heading'}, [
          this.el('h2', {className: 'card-title'}, [this.t('提示词广场')]),
          this.el('p', {className: 'tpl-note'}, [
            this.t('管理内置与本地模板：新建、编辑、导入导出，并把模板填入工作台。')
          ])
        ]),
        status,
        this.el('div', {className: 'tpl-toolbar'}, [search, tagSelect, actions]),
        grid
      );
      this.renderGrid();
    }

    renderGrid() {
      const grid = this.root.querySelector('#tpl-grid');
      if (!grid) return;
      const items = this.visible();
      if (!items.length) {
        grid.replaceChildren(this.el('p', {className: 'tpl-empty'}, [
          this.loaded ? this.t('没有符合条件的模板。') : this.t('尚未加载模板。')
        ]));
        return;
      }
      grid.replaceChildren(...items.map(item => this.card(item)));
    }

    card(template) {
      const tags = this.el('div', {className: 'tpl-card-tags'},
        (template.tags || []).map(tag => this.el('span', {className: 'meta-pill'}, [tag])));
      const defaults = [];
      if (template.defaults?.resolution) defaults.push(template.defaults.resolution);
      if (template.defaults?.aspect_ratio) defaults.push(template.defaults.aspect_ratio);
      const builtin = template.builtin === true;
      const row = this.el('div', {className: 'tpl-card-actions'}, [
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--sm comic-btn--cta',
          'data-tpl-action': 'use', 'data-tpl-id': template.id
        }, [this.t('填入工作台')]),
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
          'data-tpl-action': 'edit', 'data-tpl-id': template.id, disabled: !this.writable
        }, [this.t('编辑')]),
        this.el('button', {
          type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
          'data-tpl-action': 'copy', 'data-tpl-id': template.id, disabled: !this.writable
        }, [this.t('复制')]),
        builtin
          ? this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
            'data-tpl-action': 'restore', 'data-tpl-id': template.id, disabled: !this.writable
          }, [this.t('恢复默认')])
          : this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--danger',
            'data-tpl-action': 'delete', 'data-tpl-id': template.id, disabled: !this.writable
          }, [this.t('删除')])
      ]);
      return this.el('article', {className: 'tpl-card comic-card'}, [
        this.el('div', {className: 'tpl-card-head'}, [
          this.el('h3', {className: 'tpl-card-title'}, [template.name]),
          this.el('span', {className: 'meta-pill'}, [builtin ? this.t('内置') : this.t('本地')])
        ]),
        this.el('p', {className: 'tpl-card-desc'}, [template.ai_description || '']),
        tags,
        this.el('p', {className: 'tpl-card-meta'}, [
          this.referenceLabel(template.reference),
          defaults.length ? ` · ${defaults.join(' · ')}` : ''
        ]),
        this.el('label', {className: 'tpl-card-ai'}, [
          this.el('input', {
            type: 'checkbox',
            className: 'comic-checkbox',
            checked: template.ai_enabled !== false,
            disabled: !this.writable,
            'data-tpl-action': 'toggle-ai',
            'data-tpl-id': template.id
          }),
          this.t('对 AI 可用')
        ]),
        row
      ]);
    }

    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      this.cleanups.forEach(cleanup => cleanup());
      this.cleanups = [];
    }
  }

  window.StudioTemplatesView = StudioTemplatesView;
})();
