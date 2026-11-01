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
      this.scope = 'local';
      this.hubQuery = '';
      this.hubTag = '';
      this.hub = {templates: [], notice: '', state: null};
      this.hubTags = [];
      this.hubMine = false;
      this.hubLoading = false;
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
      if (action === 'scope') { this.switchScope(target.dataset.tplScope); return; }
      if (action === 'hub-search') { void this.searchHub(); return; }
      if (action === 'hub-sync') { void this.syncHub(true); return; }
      if (action === 'hub-import') { void this.importHub(id); return; }
      if (action === 'hub-use') { this.useHub(id); return; }
      if (action === 'hub-view') { this.viewHub(id); return; }
      if (action === 'hub-mine') { void this.toggleHubMine(); return; }
      if (action === 'hub-report') { void this.reportHub(id); return; }
      if (action === 'hub-upload') { void this.uploadHub(id); return; }
      if (action === 'hub-key') { void this.openKeyDialog(); }
    }

    input(event) {
      const field = event.target.dataset?.tplField;
      if (!field || this.destroyed) return;
      if (field === 'query') { this.query = event.target.value; this.renderGrid(); return; }
      if (field === 'tag') { this.tag = event.target.value; this.renderGrid(); }
      if (field === 'hub-query') { this.hubQuery = event.target.value; return; }
      if (field === 'hub-tag') { this.hubTag = event.target.value; }
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


    switchScope(scope) {
      const next = scope === 'online' ? 'online' : 'local';
      if (this.scope === next) return;
      this.scope = next;
      this.render();
      if (next === 'online') {
        void this.searchHub();
        if (!this.hubTags.length) void this.loadHubTags();
      }
    }

    hubEnabled() {
      const state = this.hub.state || {};
      return state.mode !== 'off' && state.enabled !== false;
    }

    async searchHub() {
      if (!this.available || this.hubLoading) return;
      this.hubLoading = true;
      this.render();
      try {
        // query 必须走 params：拼进 endpoint 会被服务端当路径编码（? -> %3F），
        // 导致路由匹配失败。
        const params = {q: this.hubQuery, limit: '50'};
        if (this.hubTag) params.tag = this.hubTag;
        const payload = await this.bridge.get('webui/templates/hub', params);
        if (this.destroyed) return;
        this.hub = {
          templates: Array.isArray(payload?.templates) ? payload.templates : [],
          notice: payload?.notice || '',
          state: payload?.state || null
        };
      } catch (_) {
        if (!this.destroyed) this.hub = {...this.hub, notice: this.t('在线检索失败，请稍后重试。')};
      } finally {
        if (!this.destroyed) { this.hubLoading = false; this.render(); }
      }
    }

    async syncHub(force = false) {
      if (!this.available) return;
      this.hubLoading = true;
      this.message = this.t('正在同步在线模板…');
      this.render();
      try {
        const state = await this.bridge.post('webui/templates/hub/sync', {force});
        if (!this.destroyed) {
          this.hub = {...this.hub, state};
          this.message = this.t('已同步，缓存 {v0} 条在线模板。', {
            v0: state?.count ?? 0
          });
        }
      } catch (_) {
        if (!this.destroyed) this.message = this.t('同步失败，请稍后重试。');
      } finally {
        if (!this.destroyed) { this.hubLoading = false; this.render(); }
        if (!this.destroyed) void this.searchHub();
      }
    }

    async importHub(id) {
      if (!this.available || !id) return;
      try {
        const result = await this.bridge.post('webui/templates/hub/import', {ids: [id]});
        this.message = result?.imported
          ? this.t('已导入为本地模板。')
          : this.t('导入失败，请检查缓存后重试。');
        await this.load();
      } catch (_) {
        this.message = this.t('导入失败，请稍后重试。');
        this.render();
      }
    }

    async toggleHubMine() {
      this.hubMine = !this.hubMine;
      if (this.hubMine) await this.loadHubMine();
      else void this.searchHub();
      this.render();
    }

    async loadHubMine() {
      this.hubLoading = true;
      this.render();
      try {
        const payload = await this.bridge.get('webui/templates/hub/mine', {limit: '50'});
        if (this.destroyed) return;
        this.hub = {
          templates: Array.isArray(payload?.templates) ? payload.templates : [],
          notice: '',
          state: payload?.state || this.hub.state
        };
      } catch (_) {
        // 没有密钥时后端返回 400，这里只提示不切换视图。
        if (!this.destroyed) {
          this.hub = {...this.hub, notice: this.t('在线操作失败，请稍后重试。')};
        }
      } finally {
        if (!this.destroyed) { this.hubLoading = false; this.render(); }
      }
    }

    async loadHubTags() {
      try {
        const payload = await this.bridge.get('webui/templates/hub/tags', {limit: '50'});
        if (!this.destroyed && Array.isArray(payload?.tags)) {
          this.hubTags = payload.tags;
          this.render();
        }
      } catch (_) { /* 标签拉取失败不影响检索 */ }
    }

    async reportHub(id) {
      const hit = this.hub.templates.find(item => item.id === id);
      if (!hit) return;
      const remoteId = hit.remote_id || hit.id;
      this.modal.openCustom({
        title: this.t('举报在线模板'),
        renderBody: body => {
          body.appendChild(this.el('p', {className: 'tpl-note'}, [
            this.t('说明举报原因，服务端会人工复核。')
          ]));
          body.appendChild(this.el('textarea', {
            className: 'comic-input comic-textarea tpl-package-input',
            rows: 5, 'data-tpl-report': 'reason'
          }));
        },
        renderFooter: (footer, confirm, cancel) => {
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline', onClick: cancel
          }, [this.t('取消')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--danger',
            onClick: async () => {
              const node = document.querySelector('[data-tpl-report="reason"]');
              const reason = node ? String(node.value || '').trim() : '';
              if (!reason) {
                this.message = this.t('举报需要填写原因');
                this.render();
                return;
              }
              const result = await this.request('POST', 'webui/templates/hub', {
                action: 'report', remote_id: remoteId, reason
              });
              this.message = result ? this.t('已提交举报。') : this.t('在线操作失败，请稍后重试。');
              confirm();
              this.render();
            }
          }, [this.t('提交举报')]));
        }
      });
    }

    viewHub(id) {
      // 缓存已在本地，查看正文不需要联网。
      const hit = this.hub.templates.find(item => item.id === id);
      if (!hit) return;
      const meta = [
        hit.author ? this.t('标签') + ': ' + hit.author : '',
        hit.source_url ? 'source: ' + hit.source_url : ''
      ].filter(Boolean).join('\n');
      this.openTextDialog(
        hit.name,
        meta ? meta + '\n\n' + (hit.content || '') : (hit.content || '')
      );
    }

    useHub(id) {
      const hit = this.hub.templates.find(item => item.id === id);
      if (!hit) return;
      this.onUsed({prompt: hit.content, template: hit});
      this.message = this.t('已把「{v0}」填入工作台。', {v0: hit.name});
      this.render();
    }

    async uploadHub(id) {
      if (!this.available || !id) return;
      // 上传需要密钥，未配置时先引导填写，避免点了才报 401。
      if (!(this.hub.state || {}).has_key) {
        this.message = this.t('请先填写在线模板密钥。');
        void this.openKeyDialog();
        return;
      }
      let preview = null;
      try {
        preview = await this.bridge.post('webui/templates/hub', {action: 'preview', id});
      } catch (_) {
        this.message = this.t('读取待上传内容失败。');
        this.render();
        return;
      }
      const ok = await this.modal.confirm({
        title: this.t('上传到在线模板库'),
        content: this.t('将公开上传以下内容（不含预览图和用户信息）：\n\n{v0}', {
          v0: preview?.content || ''
        }),
        confirmText: this.t('上传'),
        danger: true
      });
      if (!ok) return;
      try {
        await this.bridge.post('webui/templates/hub', {action: 'upload', id});
        this.message = this.t('已上传，模板会立即公开。');
      } catch (_) {
        this.message = this.t('上传失败，请稍后重试。');
      }
      this.render();
    }

    async openKeyDialog() {
      // 打开前先取当前是否已配置，用于展示掩码而不回显完整密钥。
      let maskedInfo = {configured: false, masked: ''};
      try {
        const info = await this.bridge.get('webui/templates/hub/key');
        maskedInfo = {configured: !!info?.configured, masked: String(info?.masked || '')};
      } catch (_) { /* 取不到就按未配置处理 */ }
      let keyVisible = false;
      this.modal.openCustom({
        title: this.t('在线模板密钥'),
        renderBody: body => {
          body.appendChild(this.el('p', {className: 'tpl-note'}, [
            this.t('申请或重置密钥只填邮箱：提交后去邮箱打开验证链接并点确认，密钥会发送到邮箱。')
          ]));
          body.appendChild(this.el('div', {className: 'tpl-field'}, [
            this.el('label', {className: 'tpl-field-label'}, [this.t('邮箱')]),
            this.el('input', {
              type: 'text', className: 'comic-input', 'data-tpl-key': 'email',
              placeholder: 'you@example.com'
            })
          ]));
          // 已配置时展示掩码，避免把完整密钥回显到页面。
          if (maskedInfo.configured) {
            body.appendChild(this.el('p', {className: 'tpl-note'}, [
              this.t('已配置密钥：{v0}', {v0: maskedInfo.masked})
            ]));
          }
          body.appendChild(this.el('div', {className: 'tpl-field'}, [
            this.el('label', {className: 'tpl-field-label'}, [this.t('在线模板密钥')]),
            this.el('div', {className: 'tpl-key-row'}, [
              this.el('input', {
                type: keyVisible ? 'text' : 'password',
                className: 'comic-input', 'data-tpl-key': 'key',
                placeholder: maskedInfo.configured ? this.t('留空保持原值') : 'iph_...'
              }),
              this.el('button', {
                type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
                'data-tpl-key': 'toggle',
                onClick: () => {
                  keyVisible = !keyVisible;
                  const node = document.querySelector('[data-tpl-key="key"]');
                  if (node) node.type = keyVisible ? 'text' : 'password';
                }
              }, [keyVisible ? this.t('隐藏') : this.t('显示')])
            ])
          ]));
        },
        renderFooter: (footer, confirm, cancel) => {
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline', onClick: cancel
          }, [this.t('关闭')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--danger',
            onClick: async () => {
              await this.request('POST', 'webui/templates/hub/key', {action: 'save', key: ''});
              await this.request('DELETE', 'webui/templates/hub/key');
              this.message = this.t('已吊销当前密钥。');
              confirm();
              void this.loadHubState();
            }
          }, [this.t('吊销密钥')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline',
            onClick: async () => {
              const result = await this.request('POST', 'webui/templates/hub/key', {
                action: 'test'
              });
              this.message = result
                ? this.t('密钥可用。')
                : this.t('在线操作失败，请稍后重试。');
              this.render();
            }
          }, [this.t('测试连接')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline',
            onClick: async () => {
              const value = this._keyEmail();
              await this.request('POST', 'webui/templates/hub/key', {email: value});
              this.message = this.t('已提交，请到邮箱确认后等待密钥。');
              confirm();
              this.render();
            }
          }, [this.t('申请密钥')]));
          footer.appendChild(this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--cta',
            onClick: async () => {
              const email = this._keyEmail();
              if (email) {
                await this.request('POST', 'webui/templates/hub/key', {email});
              }
              const key = this._keyValue();
              if (key) {
                const saved = await this.request('POST', 'webui/templates/hub/key', {
                  action: 'save', key
                });
                if (saved) this.message = this.t('已保存密钥。');
              }
              confirm();
              void this.loadHubState();
            }
          }, [this.t('完成')]));
        }
      });
    }

    _keyEmail() {
      const node = document.querySelector('[data-tpl-key="email"]');
      return node ? String(node.value || '').trim() : '';
    }

    _keyValue() {
      const node = document.querySelector('[data-tpl-key="key"]');
      return node ? String(node.value || '').trim() : '';
    }

    async loadHubState() {
      try {
        const state = await this.bridge.get('webui/templates/hub', {q: '', limit: '1'});
        if (!this.destroyed && state?.state) {
          this.hub = {...this.hub, state: state.state};
        }
      } catch (_) { /* 状态刷新失败不影响主流程 */ }
      this.render();
    }


    async request(method, path, body = null) {
      try {
        if (method === 'GET') return await this.bridge.get(path);
        if (method === 'DELETE') return await this.bridge.delete(path);
        return await this.bridge.post(path, body || {});
      } catch (_) {
        if (!this.destroyed) {
          this.message = this.t('在线操作失败，请稍后重试。');
          this.render();
        }
        return null;
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

      const scopeTabs = this.el('div', {className: 'tpl-tabs', role: 'tablist'}, [
        this.el('button', {
          type: 'button',
          className: `comic-btn comic-btn--sm ${this.scope === 'local' ? 'comic-btn--cta' : 'comic-btn--outline'}`,
          'data-tpl-action': 'scope', 'data-tpl-scope': 'local',
          'aria-selected': this.scope === 'local'
        }, [this.t('本地模板')]),
        this.el('button', {
          type: 'button',
          className: `comic-btn comic-btn--sm ${this.scope === 'online' ? 'comic-btn--cta' : 'comic-btn--outline'}`,
          'data-tpl-action': 'scope', 'data-tpl-scope': 'online',
          'aria-selected': this.scope === 'online'
        }, [this.t('在线模板')])
      ]);

      const grid = this.el('div', {className: 'tpl-grid', id: 'tpl-grid'});
      const toolbar = this.scope === 'online'
        ? this.hubToolbar()
        : this.el('div', {className: 'tpl-toolbar'}, [this.localSearch(), this.localTagSelect(), this.localActions()]);

      this.root.replaceChildren(
        this.el('div', {className: 'tpl-heading'}, [
          this.el('h2', {className: 'card-title'}, [this.t('提示词广场')]),
          this.el('p', {className: 'tpl-note'}, [
            this.t('管理内置与本地模板：新建、编辑、导入导出，并把模板填入工作台。')
          ])
        ]),
        status,
        scopeTabs,
        toolbar,
        grid
      );
      if (this.scope === 'online') this.renderHubGrid();
      else this.renderGrid();
    }

    localSearch() {
      return this.el('input', {
        type: 'text', className: 'comic-input tpl-search',
        'data-tpl-field': 'query', value: this.query,
        placeholder: this.t('搜索名称、说明或标签'),
        'aria-label': this.t('搜索名称、说明或标签')
      });
    }

    localTagSelect() {
      return this.el('select', {
        className: 'comic-select', 'data-tpl-field': 'tag',
        'aria-label': this.t('按标签筛选')
      }, [
        this.el('option', {value: ''}, [this.t('全部标签')]),
        ...this.tags().map(tag => this.el('option', {value: tag, selected: tag === this.tag}, [tag]))
      ]);
    }

    localActions() {
      return this.el('div', {className: 'tpl-actions'}, [
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
    }

    hubToolbar() {
      const state = this.hub.state || {};
      const summary = this.el('p', {className: 'tpl-hub-state'}, [
        state.hash
          ? this.t('缓存 {v0} 条，哈希 {v1}', {
            v0: state.count ?? 0,
            v1: String(state.hash).slice(0, 12)
          })
          : this.t('尚未同步在线模板。')
      ]);
      return this.el('div', {className: 'tpl-toolbar'}, [
        this.el('input', {
          type: 'text', className: 'comic-input tpl-search',
          'data-tpl-field': 'hub-query', value: this.hubQuery,
          placeholder: this.t('搜索在线模板'),
          'aria-label': this.t('搜索在线模板')
        }),
        this.el('select', {
          // 标签用在线真实列表，避免手打拼错。
          className: 'comic-select', 'data-tpl-field': 'hub-tag',
          'aria-label': this.t('按标签筛选')
        }, [
          this.el('option', {value: ''}, [this.t('全部标签')]),
          ...this.hubTags.map(item => this.el('option', {
            value: item.tag, selected: item.tag === this.hubTag
          }, [this.hubMine ? item.tag : `${item.tag} (${item.count})`]))
        ]),
        this.el('div', {className: 'tpl-actions'}, [
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--cta',
            'data-tpl-action': 'hub-search', disabled: this.hubLoading
          }, [this.t('检索')]),
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline',
            'data-tpl-action': 'hub-sync', disabled: this.hubLoading
          }, [this.t('立即同步')]),
          this.el('button', {
            type: 'button',
            className: this.hubMine
              ? 'comic-btn comic-btn--cta' : 'comic-btn comic-btn--outline',
            'data-tpl-action': 'hub-mine', disabled: this.hubLoading
          }, [this.t('我的模板')]),
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--outline',
            'data-tpl-action': 'hub-key'
          }, [this.t('密钥')])
        ]),
        summary
      ]);
    }

    renderHubGrid() {
      const grid = this.root.querySelector('#tpl-grid');
      if (!grid) return;
      const notice = this.hub.notice;
      const children = [];
      if (notice) {
        children.push(this.el('p', {className: 'tpl-status'}, [notice]));
      }
      if (!this.hub.templates.length) {
        children.push(this.el('p', {className: 'tpl-empty'}, [
          this.hubLoading ? this.t('正在检索在线模板…') : this.t('没有符合条件的在线模板。')
        ]));
        grid.replaceChildren(...children);
        return;
      }
      children.push(...this.hub.templates.map(item => this.hubCard(item)));
      grid.replaceChildren(...children);
    }

    hubCard(template) {
      const tags = this.el('div', {className: 'tpl-card-tags'},
        (template.tags || []).map(tag => this.el('span', {className: 'meta-pill'}, [tag])));
      const defaults = [];
      if (template.defaults?.resolution) defaults.push(template.defaults.resolution);
      if (template.defaults?.aspect_ratio) defaults.push(template.defaults.aspect_ratio);
      return this.el('article', {className: 'tpl-card comic-card'}, [
        this.el('div', {className: 'tpl-card-head'}, [
          this.el('h3', {className: 'tpl-card-title'}, [template.name]),
          this.el('span', {className: 'meta-pill'}, [
            template.found_via === 'online' ? this.t('实时检索') : this.t('缓存')
          ])
        ]),
        this.el('p', {className: 'tpl-card-desc'}, [template.ai_description || '']),
        tags,
        this.el('p', {className: 'tpl-card-meta'}, [
          this.referenceLabel(template.reference),
          defaults.length ? ` · ${defaults.join(' · ')}` : ''
        ]),
        template.author ? this.el('p', {className: 'tpl-card-meta'}, [template.author]) : this.el('span'),
        this.el('div', {className: 'tpl-card-actions'}, [
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
            'data-tpl-action': 'hub-view', 'data-tpl-id': template.id
          }, [this.t('查看')]),
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
            'data-tpl-action': 'hub-report', 'data-tpl-id': template.id
          }, [this.t('举报')]),
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--cta',
            'data-tpl-action': 'hub-use', 'data-tpl-id': template.id
          }, [this.t('填入工作台')]),
          this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
            'data-tpl-action': 'hub-import', 'data-tpl-id': template.id
          }, [this.t('导入为本地')])
        ])
      ]);
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
        !builtin && this.hubEnabled()
          ? this.el('button', {
            type: 'button', className: 'comic-btn comic-btn--sm comic-btn--outline',
            'data-tpl-action': 'hub-upload', 'data-tpl-id': template.id,
            disabled: !this.writable
          }, [this.t('上传到在线模板库')])
          : null,
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
