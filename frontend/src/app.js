/**
 * Gemini 图像生成 Studio - 原生前端控制器 (app.js)
 * 纯静态无构建 / 原生 ES2020 / XSS 绝对防护 / 极简漫画令牌体系
 */

// 1. IconSet (极简漫画 2px 手绘 SVG 图标字典，静态安全常量)
// ==========================================================================
const IconSet = {
  sliders: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg>`,
  brush: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9.06 11.9 8.07-8.06a2.85 2.85 0 1 1 4.03 4.03l-8.06 8.08"/><path d="M7.07 14.94c-1.66 0-3 1.35-3 3.02 0 1.33-2.5 1.52-2 2.04 1.5.54 4.5 1 6.5-1 1-1 1.5-2.06 1.5-3.06 0-1.67-1.34-3-3-3z"/></svg>`,
  clock: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
  grid: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>`,
  sparkle: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>`,
  upload: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`,
  image: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`,
  trash: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
  download: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
  refresh: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`,
  check: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
  x: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  alert: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  search: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
  plus: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
  inbox: `<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`,
  copy: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
  chevronDown: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`,
  chevronUp: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>`
};

// ==========================================================================
// 2. SafeDOM (纯 DOM 创建、XSS 绝密防护、安全路径处理)
// ==========================================================================
const SafeDOM = {
  // 安全创建 DOM 元素并绑定属性与子节点
  el(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null) continue;
      if (key === 'className') {
        element.className = val;
      } else if (key === 'style') {
        if (typeof val === 'object') Object.assign(element.style, val);
      } else if (key.startsWith('on')) {
        if (typeof val === 'function') element.addEventListener(key.slice(2).toLowerCase(), val);
      } else if (key === 'dataset') {
        if (typeof val === 'object') {
          for (const [dataKey, dataValue] of Object.entries(val)) {
            if (/^[A-Za-z][A-Za-z0-9_]*$/.test(dataKey)) {
              element.dataset[dataKey] = String(dataValue ?? '');
            }
          }
        }
      } else if (key === 'src') {
        if (tag.toLowerCase() === 'img' && this.isSafeImageSource(val)) {
          element.setAttribute('src', val);
        }
      } else if (key === 'href' || key === 'srcdoc') {
        continue;
      } else if (key === 'disabled') {
        element.toggleAttribute('disabled', Boolean(val));
      } else {
        element.setAttribute(key, String(val));
      }
    }

    const childList = Array.isArray(children) ? children : [children];
    for (const child of childList) {
      if (child === null || child === undefined) continue;
      if (typeof child === 'string' || typeof child === 'number') {
        element.appendChild(document.createTextNode(String(child)));
      } else if (child instanceof Node) {
        element.appendChild(child);
      }
    }
    return element;
  },

  // 创建纯文本节点
  text(str) {
    return document.createTextNode(String(str ?? ''));
  },

  // 严格安全文本赋值
  setText(element, str) {
    if (element) {
      element.textContent = String(str ?? '');
    }
  },

  // 图片文件名白名单校验正则（防路径穿越与非法字符）
  SAFE_IMAGE_NAME_REGEX: /^[A-Za-z0-9_.-]+$/,

  isSafeImageName(imageName) {
    return typeof imageName === 'string'
      && imageName !== '.'
      && imageName !== '..'
      && this.SAFE_IMAGE_NAME_REGEX.test(imageName);
  },

  isSafeImageSource(source) {
    return typeof source === 'string' && source.startsWith('blob:');
  },

  previewImage(source) {
    // Preview SSE contains a server-decoded, bounded PNG thumbnail, never a URL.
    if (typeof source !== 'string' || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(source)) return null;
    const image = this.el('img', {alt: t('生成中的预览，非最终结果'), style: {maxWidth: '100%', maxHeight: '256px', objectFit: 'contain'}});
    image.src = source;
    return image;
  },

  // 仅供注入静态常量池内联 SVG（禁止外部不可信输入）
  setSvgIcon(element, iconName) {
    const svgCode = IconSet[iconName];
    if (svgCode && element) {
      element.innerHTML = svgCode;
    }
  },

  // 批量渲染包含 data-icon 的占位元素
  renderStaticIcons(root = document) {
    root.querySelectorAll('[data-icon]').forEach((el) => {
      const iconName = el.getAttribute('data-icon');
      if (iconName && IconSet[iconName]) {
        this.setSvgIcon(el, iconName);
      }
    });
  },

  // 来源标识 -> 中文标签
  sourceLabel(source, caller) {
    if (source === 'plugin') return t('插件调用 · {v0}', { v0: caller?.plugin_name || caller?.plugin_id || t('插件') });
    const labels = {
      command: t('指令'),
      llm_tool: t('LLM 工具'),
      llm_batch: t('LLM 批量'),
      webui: t('工作台'),
      legacy: t('历史')
    };
    return labels[source] || source || t('未知');
  },

  displayRoute(record) {
    const stats = record.stats || {};
    const params = record.params || {};
    const model = stats.model || params.model || '';
    return {
      provider: stats.provider || params.provider || '',
      model: stats.model && stats.alias ? `${stats.alias}（${model}）` : model
    };
  },

  routeMeta(record) {
    const route = this.displayRoute(record);
    const labels = [];
    if (route.provider) labels.push(t('供应商: {v0}', { v0: route.provider }));
    if (route.model) labels.push(t('模型: {v0}', { v0: route.model }));
    return this.el('div', { className: 'job-meta-row' },
      labels.map((label) => this.el('span', { className: 'meta-pill' }, [label]))
    );
  },

  referenceStrip(record, onSelect) {
    const names = Array.isArray(record.reference_names)
      ? record.reference_names.filter((name) => this.isSafeImageName(name)) : [];
    if (!names.length) return null;
    const section = this.el('div', { className: 'reference-lineage' }, [
      this.el('span', { className: 'reference-lineage-label' }, [t('参考图 → 当前结果')])
    ]);
    const images = this.el('div', { className: 'reference-lineage-images' });
    for (const name of names) {
      const button = this.el('button', { type: 'button', className: 'reference-lineage-item',
        title: t('将这张历史图片设为参考图'), 'aria-label': t('设为参考图：{v0}', { v0: name }),
        onClick: async () => {
          try {
            const detail = await BridgeClient.get(`webui/history/${encodeURIComponent(record.job_id)}`);
            if (!(detail.available_reference_names || []).includes(name)) {
              button.disabled = true;
              SafeDOM.setText(button, t('参考图已清理'));
              Toast.warning(t('这张参考图已清理，请选择其它图片'));
              return;
            }
            onSelect?.(name);
          } catch (error) {
            ErrorFeedback.show(error, t('确认参考图失败'));
          }
        }
      });
      const img = this.el('img', { loading: 'lazy', alt: t('历史参考图') });
      installImageFallback(img, button, t('参考图加载失败或已清理'));
      img.addEventListener('error', () => { button.disabled = true; });
      ImageLoader.attach(img, name, { thumb: true });
      button.appendChild(img);
      images.appendChild(button);
    }
    section.appendChild(images);
    return section;
  },

  requesterLabels(requester, source) {
    const { user_id: userId, user_name: userName, group_id: groupId, chat_type: chatType } = requester || {};
    let chatLabel = t('会话类型未知');
    if (source === 'webui') {
      chatLabel = t('工作台提交');
    } else if (chatType === 'private') {
      chatLabel = t('私聊');
    } else if (chatType === 'group' || groupId) {
      // 旧记录没有会话类型时，已有群号仍足以确认群聊；缺少群号不能反推私聊。
      chatLabel = groupId ? t('群聊 · 群号: {v0}', { v0: groupId }) : t('群聊 · 群号未记录');
    }
    const labels = [chatLabel];
    if (source === 'plugin' && chatType !== 'private' && chatType !== 'group' && !groupId) labels.length = 0;
    if (source === 'plugin' && requester?.umo) labels.push(t('会话: {v0}', { v0: requester.umo }));
    if (userName && userId) labels.push(t('用户: {v0}（ID: {v1}）', { v0: userName, v1: userId }));
    else if (userId) labels.push(t('用户 ID: {v0}', { v0: userId }));
    else if (userName) labels.push(t('用户: {v0}', { v0: userName }));
    return labels;
  },

  requesterMeta(record) {
    const labels = this.requesterLabels(record.requester, record.source);
    if (record.source === 'plugin' && record.caller?.plugin_id) labels.unshift(t('插件 ID: {v0}', { v0: record.caller.plugin_id }));
    const callbackLabels = { pending: t('待通知'), running: t('通知中'), removed: t('已取消通知'), succeeded: t('已通知'), failed: t('通知失败'), interrupted: t('通知已中断') };
    if (callbackLabels[record.callback_status]) labels.push(callbackLabels[record.callback_status]);
    return this.el('div', { className: 'job-meta-row requester-meta' },
      labels.map((label) =>
        this.el('span', { className: 'meta-pill' }, [label])
      )
    );
  }
};

const ImageLoader = {
  // 页面生命周期内的字节缓存：name|thumb -> data URI，避免灯箱反复拉取同一张图
  _cache: new Map(),
  _maxCacheEntries: 32,

  _cached(key) {
    const hit = this._cache.get(key);
    if (hit) {
      // LRU：命中后挪到最新位置
      this._cache.delete(key);
      this._cache.set(key, hit);
    }
    return hit || null;
  },

  _remember(key, value) {
    this._cache.set(key, value);
    while (this._cache.size > this._maxCacheEntries) {
      const oldest = this._cache.keys().next().value;
      this._cache.delete(oldest);
    }
  },

  async attach(img, imageName, { thumb = true } = {}) {
    if (!SafeDOM.isSafeImageName(imageName)) {
      img.dispatchEvent(new Event('error'));
      return;
    }
    const requestId = Symbol(imageName);
    img.imageLoaderRequestId = requestId;
    const cacheKey = `${imageName}|${thumb ? 'thumb' : 'full'}`;
    const cached = this._cached(cacheKey);
    if (cached) {
      img.src = cached.dataUri;
      return cached;
    }
    try {
      const payload = await BridgeClient.get(
        `webui/image_b64/${encodeURIComponent(imageName)}`,
        thumb ? { thumb: '1' } : {}
      );
      const mime = String(payload?.mime || '').toLowerCase();
      const b64 = String(payload?.b64 || '');
      if (!/^image\/(?:png|jpeg|webp|gif|bmp)$/.test(mime)
          || !b64
          || !/^[A-Za-z0-9+/]*={0,2}$/.test(b64)) {
        throw new Error(t('图片接口返回了无效数据'));
      }
      const dataUri = `data:${mime};base64,${b64}`;
      const imageData = { dataUri, preview: payload.preview === true };
      this._remember(cacheKey, imageData);
      if (img.imageLoaderRequestId === requestId) {
        img.src = dataUri;
        return imageData;
      }
    } catch (error) {
      console.warn(`[ImageLoader] 加载图片失败 ${imageName}:`, error);
      if (img.imageLoaderRequestId === requestId) {
        img.dispatchEvent(new Event('error'));
      }
    }
  }
};

// ==========================================================================
// 3. BridgeClient (封装 window.AstrBotPluginView 通信 SDK)
// ==========================================================================
class BridgeClient {
  static async init() {
    if (!window.AstrBotPluginView) {
      throw new Error(t('未检测到 AstrBotPluginView SDK，请在 AstrBot Dashboard 中运行'));
    }
    return await window.AstrBotPluginView.ready();
  }

  static isAvailable() {
    return !!window.AstrBotPluginView;
  }

  static getContext() {
    return window.AstrBotPluginView?.getContext?.() ?? null;
  }

  static onContext(handler) {
    return window.AstrBotPluginView?.onContext?.(handler);
  }

  static async get(endpoint, params = {}) {
    if (!window.AstrBotPluginView) {
      throw new Error(t('AstrBotPluginView SDK 未加载'));
    }
    return await window.AstrBotPluginView.apiGet(endpoint, params);
  }

  static async post(endpoint, body = {}) {
    if (!window.AstrBotPluginView) {
      throw new Error(t('AstrBotPluginView SDK 未加载'));
    }
    return await window.AstrBotPluginView.apiPost(endpoint, body);
  }

  static async upload(endpoint, file) {
    if (!window.AstrBotPluginView) {
      throw new Error(t('AstrBotPluginView SDK 未加载'));
    }
    return await window.AstrBotPluginView.upload(endpoint, file);
  }

  static async download(endpoint, params = {}, filename = '') {
    if (!window.AstrBotPluginView) {
      throw new Error(t('AstrBotPluginView SDK 未加载'));
    }
    return await window.AstrBotPluginView.download(endpoint, params, filename);
  }

  static async subscribeSSE(endpoint, handlers = {}, params = {}) {
    if (!window.AstrBotPluginView) {
      throw new Error(t('AstrBotPluginView SDK 未加载'));
    }
    return await window.AstrBotPluginView.subscribeSSE(endpoint, handlers, params);
  }

  static async unsubscribeSSE(subscriptionId) {
    if (subscriptionId && window.AstrBotPluginView?.unsubscribeSSE) {
      return await window.AstrBotPluginView.unsubscribeSSE(subscriptionId);
    }
  }
}

// ==========================================================================
// 3.5 I18n (跟随 Dashboard 语言；中文原文即 key，翻译表经 bridge 上下文下发)
// ==========================================================================
const I18n = {
  locale: 'zh-CN',
  initialLocale: null,
  reloading: false,
  tables: {},

  t(key, vars) {
    const lookup = (loc) => {
      const hit = this.tables?.[loc]?.strings?.[key];
      return typeof hit === 'string' ? hit : null;
    };
    // 中文原文即 key：zh 系 locale 表 miss 直接用原文，不落到 en-US
    const direct = lookup(this.locale);
    if (direct !== null) return I18n.format(direct, vars);
    if (String(this.locale).startsWith('zh')) return I18n.format(key, vars);
    const en = lookup('en-US');
    if (en !== null) return I18n.format(en, vars);
    return I18n.format(key, vars);
  },

  format(text, vars) {
    if (!vars) return String(text);
    return String(text).replace(/\{(\w+)\}/g, (m, k) => (
      vars[k] === undefined || vars[k] === null ? m : String(vars[k])
    ));
  },

  sync(ctx) {
    if (!ctx) return;
    this.tables = ctx.i18n && typeof ctx.i18n === 'object' ? ctx.i18n : {};
    const raw = typeof ctx.locale === 'string' && ctx.locale ? ctx.locale : 'zh-CN';
    const known = raw === 'zh-CN' || (this.tables[raw] && typeof this.tables[raw] === 'object');
    const next = known ? raw : raw.split('-')[0];
    if (this.initialLocale === null) {
      this.initialLocale = next;
      this.locale = next;
      this.applyDom();
      return;
    }
    if (next !== this.initialLocale && !this.reloading) {
      // 语言切换：全量重载，避免逐视图重渲染的遗漏
      this.reloading = true;
      location.reload();
    }
  },

  applyDom() {
    document.documentElement.lang = this.locale;
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      el.textContent = I18n.t(el.getAttribute('data-i18n'));
    });
    const attrMap = {
      'data-i18n-placeholder': 'placeholder',
      'data-i18n-title': 'title',
      'data-i18n-aria-label': 'aria-label',
      'data-i18n-alt': 'alt',
    };
    for (const [marker, attr] of Object.entries(attrMap)) {
      document.querySelectorAll(`[${marker}]`).forEach((el) => {
        el.setAttribute(attr, I18n.t(el.getAttribute(marker)));
      });
    }
  },
};

const t = (key, vars) => I18n.t(key, vars);

// ==========================================================================
// 4. Toast (极简漫画风格全局浮动通知)
// ==========================================================================
const Toast = {
  container: null,

  init() {
    this.container = document.getElementById('toast-container');
  },

  show(message, type = 'info', duration = 3500) {
    if (!this.container) {
      this.init();
    }
    const iconName = type === 'success' ? 'check' : (type === 'warning' || type === 'error' ? 'alert' : 'sparkle');
    const iconEl = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(iconEl, iconName);

    const textEl = SafeDOM.el('span', {}, [message]);
    const toastEl = SafeDOM.el('div', {
      className: `comic-toast comic-toast--${type}`
    }, [iconEl, textEl]);

    this.container.appendChild(toastEl);

    setTimeout(() => {
      toastEl.style.opacity = '0';
      toastEl.style.transform = 'translateY(-8px)';
      setTimeout(() => {
        if (toastEl.parentNode) {
          toastEl.parentNode.removeChild(toastEl);
        }
      }, 150);
    }, duration);
  },

  success(msg, duration) { this.show(msg, 'success', duration); },
  warning(msg, duration) { this.show(msg, 'warning', duration); },
  error(msg, duration) { this.show(msg, 'error', duration); },
  info(msg, duration) { this.show(msg, 'info', duration); }
};

const ErrorFeedback = {
  message(error) {
    return String(error?.message || '').trim();
  },

  isAuth(error) {
    return /(?:\b401\b|unauthorized|未授权|未认证|登录.*(?:失效|过期)|请.*登录)/i.test(this.message(error));
  },

  show(error, fallback = t('操作失败')) {
    const message = this.message(error);
    let display = message ? `${fallback}：${message}` : fallback;
    if (this.isAuth(error)) {
      display = t('登录状态已失效，请刷新 Dashboard 并重新登录');
    } else if (/(?:\b429\b|并发.*(?:已满|上限)|队列已满)/i.test(message)) {
      display = t('服务器并发处理中（任务队列已满），请稍候重试');
    } else if (/(?:\b507\b|暂存空间已满|存储空间不足)/i.test(message)) {
      display = t('上传暂存空间已满，请等待自动清理或稍后再试');
    } else if (/(?:\b503\b|正在卸载|已关闭|重新加载|尚未初始化)/i.test(message)) {
      display = t('插件正在重新加载，请稍后重试');
    }
    Toast.error(display);
    return display;
  }
};

const Clipboard = {
  async copy(value, successMessage) {
    try {
      await navigator.clipboard.writeText(String(value ?? ''));
      Toast.success(successMessage);
    } catch (error) {
      console.warn('[Clipboard] 写入剪贴板失败:', error);
      Toast.error(t('复制失败，请检查浏览器剪贴板权限'));
    }
  }
};

function createImagePlaceholder(message) {
  const placeholder = SafeDOM.el('span', { className: 'comic-img-placeholder' });
  const icon = SafeDOM.el('span', { className: 'btn-icon' });
  SafeDOM.setSvgIcon(icon, 'inbox');
  placeholder.appendChild(icon);
  placeholder.appendChild(SafeDOM.el('span', {}, [message]));
  return placeholder;
}

function installImageFallback(image, container, message) {
  image.addEventListener('error', () => {
    if (image.parentNode === container) {
      image.replaceWith(createImagePlaceholder(message));
    }
  }, { once: true });
}

// ==========================================================================
// 5. Modal (自绘二次确认与自定义对话框)
// ==========================================================================
const Modal = {
  backdrop: null,
  titleEl: null,
  bodyEl: null,
  footerEl: null,
  closeBtn: null,
  currentResolve: null,
  currentClose: null,
  onKeydown: null,
  previousFocus: null,

  init() {
    this.backdrop = document.getElementById('app-modal-backdrop');
    this.titleEl = document.getElementById('modal-title');
    this.bodyEl = document.getElementById('modal-body');
    this.footerEl = document.getElementById('modal-footer');
    this.closeBtn = document.getElementById('modal-close-btn');

    this.closeBtn.addEventListener('click', () => this.close(false));
    this.backdrop.addEventListener('click', (e) => {
      if (e.target === this.backdrop) {
        this.close(false);
      }
    });
  },

  confirm({ title = t('确认操作'), content = '', confirmText = t('确认'), cancelText = t('取消'), danger = false }) {
    return new Promise((resolve) => {
      if (this.backdrop.style.display !== 'none') {
        this.close(false);
      }
      this.currentResolve = resolve;
      SafeDOM.setText(this.titleEl, title);

      this.bodyEl.replaceChildren();
      this.footerEl.replaceChildren();

      const p = SafeDOM.el('p', {}, [content]);
      this.bodyEl.appendChild(p);

      const cancelBtn = SafeDOM.el('button', {
        type: 'button',
        className: 'comic-btn comic-btn--outline',
        onClick: () => this.close(false)
      }, [cancelText]);

      const confirmBtn = SafeDOM.el('button', {
        type: 'button',
        className: `comic-btn ${danger ? 'comic-btn--danger' : 'comic-btn--cta'}`,
        onClick: () => this.close(true)
      }, [confirmText]);

      this.footerEl.appendChild(cancelBtn);
      this.footerEl.appendChild(confirmBtn);

      this.open();
    });
  },

  openCustom({ title = t('对话框'), renderBody, renderFooter, onClose, variant = '' }) {
    if (this.backdrop.style.display !== 'none') {
      this.close(false);
    }
    this.currentClose = typeof onClose === 'function' ? onClose : null;
    this.backdrop.classList.toggle('comic-modal-backdrop--parameters', variant === 'parameters');
    SafeDOM.setText(this.titleEl, title);
    this.bodyEl.replaceChildren();
    this.footerEl.replaceChildren();

    if (typeof renderBody === 'function') {
      renderBody(this.bodyEl);
    }
    if (typeof renderFooter === 'function') {
      renderFooter(this.footerEl, () => this.close(true), () => this.close(false));
    }
    this.open();
  },

  open() {
    if (this.onKeydown) {
      document.removeEventListener('keydown', this.onKeydown);
    }
    this.previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.backdrop.style.display = 'flex';
    this.backdrop.setAttribute('aria-hidden', 'false');
    document.body.classList.add('overlay-open');
    this.onKeydown = (e) => {
      if (e.key === 'Escape') {
        this.close(false);
      } else if (e.key === 'Tab') {
        const focusable = Array.from(this.backdrop.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'));
        if (focusable.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', this.onKeydown);
    requestAnimationFrame(() => {
      if (this.backdrop.style.display === 'none') return;
      const preferred = this.footerEl.querySelector('[data-dialog-initial-focus]') || this.footerEl.querySelector('button:not(:disabled)') || this.closeBtn;
      preferred?.focus();
    });
  },

  close(result) {
    this.backdrop.style.display = 'none';
    this.backdrop.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('overlay-open');
    if (this.onKeydown) {
      document.removeEventListener('keydown', this.onKeydown);
      this.onKeydown = null;
    }
    if (this.currentResolve) {
      const res = this.currentResolve;
      this.currentResolve = null;
      res(result);
    }
    this.bodyEl.replaceChildren();
    this.footerEl.replaceChildren();
    if (this.previousFocus?.isConnected) {
      this.previousFocus.focus();
    }
    this.previousFocus = null;
    this.backdrop.classList.remove('comic-modal-backdrop--parameters');
    const onClose = this.currentClose;
    this.currentClose = null;
    onClose?.(result);
  }
};

// ==========================================================================
// 6. Lightbox (全屏大图预览器与键盘导航)
// ==========================================================================
const Lightbox = {
  el: null,
  backdrop: null,
  img: null,
  closeBtn: null,
  prevBtn: null,
  nextBtn: null,
  promptEl: null,
  tagsEl: null,
  previewNotice: null,
  downloadBtn: null,
  imgWrap: null,
  items: [],
  currentIndex: 0,
  onKeydown: null,
  previousFocus: null,

  init() {
    this.el = document.getElementById('app-lightbox');
    this.backdrop = document.getElementById('lightbox-backdrop');
    this.img = document.getElementById('lightbox-img');
    this.imgWrap = this.img.closest('.lightbox-img-wrap');
    this.closeBtn = document.getElementById('lightbox-close-btn');
    this.prevBtn = document.getElementById('lightbox-prev-btn');
    this.nextBtn = document.getElementById('lightbox-next-btn');
    this.promptEl = document.getElementById('lightbox-prompt');
    this.tagsEl = document.getElementById('lightbox-tags');
    this.previewNotice = document.getElementById('lightbox-preview-notice');
    this.downloadBtn = document.getElementById('lightbox-download-btn');
    this.downloadBtn.addEventListener('click', async () => {
      const item = this.items[this.currentIndex];
      if (!item) return;
      try {
        await BridgeClient.download(`webui/image/${encodeURIComponent(item.imageName)}`, { download: '1' }, item.imageName);
        Toast.success(t('已发起原图下载'));
      } catch (error) {
        ErrorFeedback.show(error, t('下载失败'));
      }
    });

    this.closeBtn.addEventListener('click', () => this.close());
    this.backdrop.addEventListener('click', () => this.close());
    this.prevBtn.addEventListener('click', () => this.prev());
    this.nextBtn.addEventListener('click', () => this.next());
  },

  open(items, startIndex = 0) {
    const safeItems = Array.isArray(items)
      ? items.filter((item) => SafeDOM.isSafeImageName(item?.imageName))
      : [];
    if (safeItems.length === 0) return;
    this.items = safeItems;
    this.currentIndex = Math.min(Math.max(Number(startIndex) || 0, 0), safeItems.length - 1);
    if (this.onKeydown) {
      document.removeEventListener('keydown', this.onKeydown);
    }
    this.previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.el.style.display = 'flex';
    this.el.setAttribute('aria-hidden', 'false');
    document.body.classList.add('overlay-open');
    this.renderCurrent();

    this.onKeydown = (e) => {
      if (e.key === 'Escape') {
        this.close();
      } else if (e.key === 'ArrowLeft') {
        this.prev();
      } else if (e.key === 'ArrowRight') {
        this.next();
      } else if (e.key === 'Tab') {
        const focusable = [this.closeBtn, this.prevBtn, this.nextBtn, this.downloadBtn].filter((element) => element && element.style.display !== 'none');
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', this.onKeydown);
    requestAnimationFrame(() => {
      if (this.el.style.display !== 'none') this.closeBtn?.focus();
    });
  },

  renderCurrent() {
    const item = this.items[this.currentIndex];
    if (!item) return;

    this.imgWrap?.classList.remove('lightbox-img-wrap--error');
    // 加载完成前隐藏 img：无 src 的 img 会显示浏览器破图图标
    this.img.style.display = 'none';
    const spinner = SafeDOM.el('div', {
      className: 'comic-spinner',
      role: 'status',
      'aria-label': t('原图加载中')
    });
    this.imgWrap?.replaceChildren(spinner, this.img);
    installImageFallback(this.img, this.imgWrap, t('图片加载失败或已清理'));
    this.img.addEventListener('load', () => {
      spinner.remove();
      this.img.style.display = 'block';
    }, { once: true });
    this.previewNotice.hidden = true;
    void ImageLoader.attach(this.img, item.imageName, { thumb: false }).then((loaded) => {
      if (this.items[this.currentIndex] === item) {
        this.previewNotice.hidden = !loaded?.preview;
      }
    });
    SafeDOM.setText(this.promptEl, item.prompt || t('无提示词记录'));

    this.tagsEl.replaceChildren();
    if (item.provider) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('供应商: {v0}', { v0: item.provider })]));
    }
    if (item.model) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('模型: {v0}', { v0: item.model })]));
    }
    if (item.resolution) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('分辨率: {v0}', { v0: item.resolution })]));
    }
    if (item.aspect_ratio) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('比例: {v0}', { v0: item.aspect_ratio })]));
    }
    if (item.duration_ms) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('耗时: {v0}s', { v0: (item.duration_ms / 1000).toFixed(1) })]));
    }
    if (item.source) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('来源: {v0}', { v0: SafeDOM.sourceLabel(item.source, item.caller) })]));
    }
    if (item.caller?.plugin_id) this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [t('插件 ID: {v0}', { v0: item.caller.plugin_id })]));
    for (const label of SafeDOM.requesterLabels(item.requester, item.source)) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [label]));
    }

    const showNav = this.items.length > 1;
    this.prevBtn.style.display = showNav ? 'flex' : 'none';
    this.nextBtn.style.display = showNav ? 'flex' : 'none';
  },

  prev() {
    if (this.items.length <= 1) return;
    this.currentIndex = (this.currentIndex - 1 + this.items.length) % this.items.length;
    this.renderCurrent();
  },

  next() {
    if (this.items.length <= 1) return;
    this.currentIndex = (this.currentIndex + 1) % this.items.length;
    this.renderCurrent();
  },

  close() {
    this.el.style.display = 'none';
    this.el.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('overlay-open');
    this.img.src = '';
    this.items = [];
    this.currentIndex = 0;
    if (this.onKeydown) {
      document.removeEventListener('keydown', this.onKeydown);
      this.onKeydown = null;
    }
    if (this.previousFocus?.isConnected) {
      this.previousFocus.focus();
    }
    this.previousFocus = null;
  }
};

// ==========================================================================
// 7. Store (全局响应式单向数据中心与 Pub/Sub 事件总线)
// ==========================================================================
class Store {
  constructor() {
    this.activeJobs = new Map(); // job_id -> JobRecord
    this.capabilities = []; // ProviderCapability[]
    this.currentTab = 'workbench';
    this.mode = 'single'; // 'single' | 'batch'
    this.referenceTray = []; // ReferenceItem[]: { id, type: 'upload'|'gallery', name, previewUrl, file? }
    this.batchItems = []; // BatchItem[]: { id, name, prompt, image_count }
    this.selectedGalleryJobs = new Set();
    this.galleryQuery = {
      page: 1,
      size: 20,
      keyword: '',
      source: '',
      group_id: '',
      user_id: '',
      plugin_id: ''
    };
    this.galleryData = {
      items: [],
      page: 1,
      size: 20,
      total: 0
    };
    this.highlightJobId = null;
    this.listeners = new Map();
  }

  on(event, handler) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event).add(handler);
    return () => this.listeners.get(event)?.delete(handler);
  }

  emit(event, data) {
    this.listeners.get(event)?.forEach((fn) => {
      try {
        fn(data);
      } catch (err) {
        console.error(`[Store emit ${event}]`, err);
      }
    });
  }

  applySnapshot(records) {
    if (!Array.isArray(records)) return;
    this.activeJobs.clear();
    for (const rec of records) {
      if (rec && rec.job_id) {
        this.activeJobs.set(rec.job_id, rec);
      }
    }
    this.emit('jobs_updated', this.getJobsList());
  }

  patchJob(record) {
    if (!record || !record.job_id) return;
    this.activeJobs.set(record.job_id, record);
    this.emit('jobs_updated', this.getJobsList());
  }

  removeJobs(jobIds) {
    if (!Array.isArray(jobIds)) return;
    for (const id of jobIds) {
      this.activeJobs.delete(id);
    }
    this.emit('jobs_updated', this.getJobsList());
  }

  getJobsList() {
    return Array.from(this.activeJobs.values()).sort((a, b) => {
      const aRunning = ['queued', 'running'].includes(a.status);
      const bRunning = ['queued', 'running'].includes(b.status);
      if (aRunning && !bRunning) return -1;
      if (!aRunning && bRunning) return 1;
      const aTime = new Date(a.created_at || 0).getTime();
      const bTime = new Date(b.created_at || 0).getTime();
      return bTime - aTime;
    });
  }

  getRunningJobsCount() {
    let count = 0;
    for (const job of this.activeJobs.values()) {
      if (['queued', 'running'].includes(job.status)) count++;
    }
    return count;
  }

  getRecentJobsCount() {
    return this.activeJobs.size;
  }
}

// ==========================================================================
// 8. SSEController (SSE 全生命周期、看门狗与弹性重连)
// ==========================================================================
class SSEController {
  constructor(store) {
    this.store = store;
    this.subscriptionId = null;
    this.reconnectAttempts = 0;
    this.maxReconnectDelay = 30000;
    this.reconnectTimer = null;
    this.watchdogTimer = null;
    this.lastActiveTime = Date.now();
    this.isPageVisible = !document.hidden;
    this.connectPromise = null;
    this.snapshotPromise = null;
    this.connectionGeneration = 0;
    this.destroyed = false;
    this.visibilityHandler = null;

    this.initVisibilityListener();
  }

  async start() {
    if (this.destroyed) return;
    await this.fetchSnapshot();
    await this.connect();
    this.startWatchdog();
  }

  async fetchSnapshot() {
    if (this.destroyed) return false;
    if (this.snapshotPromise) return await this.snapshotPromise;

    const request = (async () => {
      try {
        const records = await BridgeClient.get('webui/jobs');
        if (Array.isArray(records) && !this.destroyed) {
          this.store.applySnapshot(records);
          return true;
        }
      } catch (err) {
        console.warn('[SSEController] 拉取 jobs 快照失败:', err);
        if (ErrorFeedback.isAuth(err)) {
          this.updateStatusIndicator('error', t('登录状态已失效'));
        }
      }
      return false;
    })();

    this.snapshotPromise = request;
    try {
      return await request;
    } finally {
      if (this.snapshotPromise === request) {
        this.snapshotPromise = null;
      }
    }
  }

  async connect() {
    if (this.destroyed) return;
    if (this.connectPromise) return await this.connectPromise;

    const request = this.performConnect();
    this.connectPromise = request;
    try {
      await request;
    } finally {
      if (this.connectPromise === request) {
        this.connectPromise = null;
      }
    }
  }

  async performConnect() {
    await this.disconnect();
    if (this.destroyed) return;

    const generation = ++this.connectionGeneration;

    this.updateStatusIndicator('connecting', t('正在连接实时通道...'));

    try {
      const subscriptionId = await BridgeClient.subscribeSSE('webui/jobs/stream', {
        onOpen: () => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.reconnectAttempts = 0;
          this.lastActiveTime = Date.now();
          this.updateStatusIndicator('connected', t('实时连接就绪'));
        },
        onMessage: (msg) => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.lastActiveTime = Date.now();
          this.handleMessage(msg);
        },
        onError: (error) => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.updateStatusIndicator('error', t('连接异常，准备重试...'));
          if (!ErrorFeedback.isAuth(error)) {
            this.scheduleReconnect();
          }
        }
      });

      if (this.destroyed || generation !== this.connectionGeneration) {
        await BridgeClient.unsubscribeSSE(subscriptionId).catch(() => {});
        return;
      }
      this.subscriptionId = subscriptionId;
    } catch (err) {
      console.error('[SSEController] 订阅发起失败:', err);
      if (this.destroyed || generation !== this.connectionGeneration) return;
      if (ErrorFeedback.isAuth(err)) {
        this.updateStatusIndicator('error', t('登录状态已失效'));
      } else {
        this.updateStatusIndicator('error', t('连接异常，准备重试...'));
        this.scheduleReconnect();
      }
    }
  }

  handleMessage(msg) {
    const payload = msg?.parsed || msg?.data;
    if (!payload || typeof payload !== 'object') return;

    switch (payload.type) {
      case 'snapshot':
        if (Array.isArray(payload.data)) {
          this.store.applySnapshot(payload.data);
        }
        break;
      case 'job':
        if (payload.data && payload.data.job_id) {
          this.store.patchJob(payload.data);
        }
        break;
      case 'resync':
        void this.fetchSnapshot();
        break;
      default:
        break;
    }
  }

  scheduleReconnect() {
    if (this.destroyed || this.reconnectTimer) return;
    const delay = Math.min(1000 * (2 ** this.reconnectAttempts), this.maxReconnectDelay);
    this.reconnectAttempts++;

    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null;
      if (this.isPageVisible && !this.destroyed) {
        await this.connect();
      }
    }, delay);
  }

  startWatchdog() {
    if (this.watchdogTimer || this.destroyed) return;
    this.watchdogTimer = setInterval(() => {
      if (!this.isPageVisible || this.destroyed) return;
      const hasRunningJobs = this.store.getRunningJobsCount() > 0;
      if (hasRunningJobs && (Date.now() - this.lastActiveTime > 30000)) {
        console.warn('[SSEController] 超过 30 秒无帧更新且有运行中任务，执行快照重同步与重连');
        void this.recover();
      }
    }, 10000);
  }

  async recover() {
    await this.fetchSnapshot();
    await this.connect();
  }

  async afterJobSubmitted() {
    await this.fetchSnapshot();
    if (!this.subscriptionId || this.reconnectAttempts > 0 || (Date.now() - this.lastActiveTime > 30000)) {
      await this.connect();
    }
  }

  initVisibilityListener() {
    this.visibilityHandler = () => {
      this.isPageVisible = !document.hidden;
      if (this.isPageVisible && !this.destroyed) {
        void this.fetchSnapshot();
        if (!this.subscriptionId || this.reconnectAttempts > 0 || (Date.now() - this.lastActiveTime > 30000)) {
          void this.connect();
        }
      }
    };
    document.addEventListener('visibilitychange', this.visibilityHandler);
  }

  async disconnect() {
    this.connectionGeneration++;
    if (this.subscriptionId) {
      const id = this.subscriptionId;
      this.subscriptionId = null;
      try {
        await BridgeClient.unsubscribeSSE(id);
      } catch (e) { /* ignore */ }
    }
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  async destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.visibilityHandler = null;
    }
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
    await this.disconnect();
  }

  updateStatusIndicator(state, text) {
    const pill = document.getElementById('sse-indicator');
    const textEl = document.getElementById('sse-indicator-text');
    if (!pill || !textEl) return;
    pill.className = `status-pill status-pill--${state}`;
    SafeDOM.setText(textEl, text);
  }
}

// ==========================================================================
// 9. StopwatchTimer (本地 1000ms 高精度运行中秒表驱动)
// ==========================================================================
class StopwatchTimer {
  constructor(store) {
    this.store = store;
    this.timer = null;
    this.unsubscribeStore = null;
    this.started = false;
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.unsubscribeStore = this.store.on('jobs_updated', () => this.sync());
    this.sync();
  }

  sync() {
    if (!this.started) return;
    if (this.store.getRunningJobsCount() === 0) {
      this.clearTimer();
      return;
    }
    if (this.timer) return;
    this.tick();
    this.timer = setInterval(() => {
      this.tick();
    }, 1000);
  }

  tick() {
    const now = Date.now();
    document.querySelectorAll('.job-card[data-status="running"]').forEach((card) => {
      const createdAt = new Date(card.getAttribute('data-created-at') || '').getTime();
      const durationEl = card.querySelector('.job-duration');
      if (!Number.isFinite(createdAt) || !durationEl) return;
      const diffSec = Math.max(0, Math.floor((now - createdAt) / 1000));
      const mins = String(Math.floor(diffSec / 60)).padStart(2, '0');
      const secs = String(diffSec % 60).padStart(2, '0');
      SafeDOM.setText(durationEl, `${mins}:${secs}`);
    });
  }

  clearTimer() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
  }

  stop() {
    this.started = false;
    this.clearTimer();
    this.unsubscribeStore?.();
    this.unsubscribeStore = null;
  }
}

// ==========================================================================
// 10. WorkbenchView (工作台控制器)
// ==========================================================================
// ==========================================================================
// 13. 应用入口启动器 (App Bootstrapper)
// ==========================================================================
class StudioApp {
  constructor() {
    this.store = new Store();
    this.sse = new SSEController(this.store);
    this.stopwatch = new StopwatchTimer(this.store);
    this.unsubscribeContext = null;
    this.navCleanups = [];
    this.destroyed = false;
    this.refillRequestId = 0;
    this.handleUnload = (event) => {
      // 取消离开页面时仍须保留可用的编辑器；真正离开由 pagehide 清理。
      if (event?.type === 'beforeunload' && (this.limits?.dirty || this.providers?.dirty || this.providers?.editor)) {
        event.preventDefault();
        event.returnValue = '';
        return;
      }
      void this.destroy();
    };

    this.tabBtns = {
      workbench: document.getElementById('tab-btn-workbench'),
      progress: document.getElementById('tab-btn-progress'),
      gallery: document.getElementById('tab-btn-gallery'),
      limits: document.getElementById('tab-btn-limits'),
      providers: document.getElementById('tab-btn-providers'),
      templates: document.getElementById('tab-btn-templates')
    };

    this.panels = {
      workbench: document.getElementById('panel-workbench'),
      progress: document.getElementById('panel-progress'),
      gallery: document.getElementById('panel-gallery'),
      limits: document.getElementById('panel-limits'),
      providers: document.getElementById('panel-providers'),
      templates: document.getElementById('panel-templates')
    };
  }

  async init() {
    // 1. 初始化静态图标字典
    SafeDOM.renderStaticIcons(document);

    // 1.5 等待 bridge 初始上下文（语言/主题）到达后再首渲染，避免语言闪变
    try {
      await Promise.race([BridgeClient.init(), new Promise((r) => setTimeout(r, 1500))]);
    } catch (_) { /* bridge 不可用时按默认语言渲染 */ }
    I18n.sync(BridgeClient.getContext());

    // 2. 初始化全局模态框与灯箱
    Toast.init();
    Modal.init();
    Lightbox.init();

    // 3. 页面主题同步
    this.initThemeSync();

    // 4. 初始化子视图
    this.workbench = window.__createWorkbenchView(document.getElementById('panel-workbench'), {
      bridge: BridgeClient, modal: Modal, t,
      icons: IconSet, toast: Toast, errorFeedback: ErrorFeedback,
      clipboard: Clipboard, imageLoader: ImageLoader, installImageFallback,
      onJobSubmitted: (newJobId) => {
        this.switchTab('progress');
        if (newJobId) {
          void this.sse.afterJobSubmitted();
        }
      }
    });

    this.progress = window.__createProgressView(document.getElementById('panel-progress'), {
      store: this.store, bridge: BridgeClient, t,
      safeDom: SafeDOM, icons: IconSet,
      toast: Toast, errorFeedback: ErrorFeedback, clipboard: Clipboard,
      imageLoader: ImageLoader, lightbox: Lightbox, installImageFallback,
      onRefillRequest: (_prompt, _params, record) => this.refillJob(record),
      onSetReference: (imageName) => {
        const added = this.workbench.addGalleryReference(imageName);
        if (added) this.switchTab('workbench');
      }
    });

    this.gallery = window.__createGalleryView(document.getElementById('panel-gallery'), {
      store: this.store, bridge: BridgeClient, modal: Modal, t,
      safeDom: SafeDOM, icons: IconSet,
      toast: Toast, errorFeedback: ErrorFeedback, clipboard: Clipboard,
      imageLoader: ImageLoader, lightbox: Lightbox, installImageFallback,
      onRefillRequest: (_prompt, _params, record) => this.refillJob(record),
      onSetReference: (imageName) => {
        const added = this.workbench.addGalleryReference(imageName);
        if (added) this.switchTab('workbench');
      }
    });

    this.limits = window.__createLimitsView(document.getElementById('panel-limits'), {
      bridge: BridgeClient, modal: Modal, t
    });
    const providerRoot = document.getElementById('provider-config-root');
    const createProviderConfigView = window.__createProviderConfigView;
    if (providerRoot && typeof createProviderConfigView === 'function') {
      this.providers = createProviderConfigView(providerRoot, {
        bridge: BridgeClient, modal: Modal, t: t,
        onSaved: async () => {
          if (!this.destroyed) await this.workbench.loadCapabilities();
        }
      });
    } else if (providerRoot) {
      providerRoot.textContent = t('供应商配置组件未能加载，请刷新 Studio。');
    }

    const templatesRoot = document.getElementById('templates-root');
    const createTemplatesView = window.__createTemplatesView;
    if (templatesRoot && typeof createTemplatesView === 'function') {
      this.templates = createTemplatesView(templatesRoot, {
        bridge: BridgeClient, modal: Modal, t: t,
        onUsed: ({prompt, template}) => {
          this.workbench.applyTemplate(prompt, template);
          this.switchTab('workbench');
        }
      });
    } else if (templatesRoot) {
      templatesRoot.textContent = t('提示词广场组件未能加载，请刷新 Studio。');
    }

    // 5. 绑定 Tab 切换事件
    this.initNavTabs();

    // 6. 启动秒表驱动
    this.stopwatch.start();

    // 7. BridgeClient 初始化并启动 SSE / 数据流
    window.addEventListener('beforeunload', this.handleUnload);
    window.addEventListener('pagehide', this.handleUnload);
    try {
      await BridgeClient.init();
    } catch (e) {
      console.error('[StudioApp] Bridge 初始化失败:', e);
      this.workbench.showGlobalBanner(t('未检测到 AstrBot 页面桥接，请在 Dashboard 插件页中打开 Studio'));
      this.workbench.setFormDisabled(true);
      this.sse.updateStatusIndicator('error', t('页面桥接不可用'));
      return;
    }

    await this.workbench.loadCapabilities();
    await this.sse.start();

  }

  async refillJob(record) {
    const requestId = ++this.refillRequestId;
    try {
      const detail = await BridgeClient.get(`webui/history/${encodeURIComponent(record.job_id)}`);
      if (requestId !== this.refillRequestId || this.destroyed) return;
      this.switchTab('workbench');
      this.workbench.refillParameters(detail.prompt, detail.params);
      const available = detail.available_reference_names || [];
      const added = this.workbench.restoreReferences(available);
      const missing = (detail.missing_reference_names || []).length;
      const excess = available.length - added;
      const notices = [];
      if (missing) notices.push(t('{v0} 张历史参考图已清理', { v0: missing }));
      if (excess) notices.push(t('{v0} 张参考图超出当前模型上限', { v0: excess }));
      if (notices.length) Toast.warning(t('{v0}，已跳过', { v0: notices.join('；') }));
    } catch (error) {
      if (requestId === this.refillRequestId && !this.destroyed) ErrorFeedback.show(error, t('读取历史任务失败'));
    }
  }

  initThemeSync() {
    const applyTheme = (isDark) => {
      document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
    };

    const ctx = BridgeClient.getContext();
    if (typeof ctx?.isDark === 'boolean') {
      applyTheme(ctx.isDark);
    }

    this.unsubscribeContext = BridgeClient.onContext((newCtx) => {
      if (typeof newCtx?.isDark === 'boolean') {
        applyTheme(newCtx.isDark);
      }
      I18n.sync(newCtx);
    });
  }

  initNavTabs() {
    Object.keys(this.tabBtns).forEach((tabKey) => {
      const btn = this.tabBtns[tabKey];
      if (btn) {
        const onClick = () => this.switchTab(tabKey);
        const onKeydown = (event) => {
          const keys = Object.keys(this.tabBtns);
          const currentIndex = keys.indexOf(tabKey);
          let nextIndex = currentIndex;
          if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % keys.length;
          else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + keys.length) % keys.length;
          else if (event.key === 'Home') nextIndex = 0;
          else if (event.key === 'End') nextIndex = keys.length - 1;
          else return;
          event.preventDefault();
          const nextKey = keys[nextIndex];
          this.switchTab(nextKey);
          this.tabBtns[nextKey]?.focus();
        };
        btn.addEventListener('click', onClick);
        btn.addEventListener('keydown', onKeydown);
        this.navCleanups.push(() => {
          btn.removeEventListener('click', onClick);
          btn.removeEventListener('keydown', onKeydown);
        });
      }
    });
  }

  switchTab(tabKey) {
    if (!this.panels[tabKey]) return;
    this.store.currentTab = tabKey;

    Object.keys(this.tabBtns).forEach((key) => {
      const btn = this.tabBtns[key];
      const panel = this.panels[key];
      const isActive = key === tabKey;

      if (btn) {
        btn.classList.toggle('tab-btn--active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        btn.setAttribute('tabindex', isActive ? '0' : '-1');
      }

      if (panel) {
        panel.hidden = !isActive;
        panel.setAttribute('aria-hidden', isActive ? 'false' : 'true');
        panel.classList.toggle('studio-panel--active', isActive);
      }
    });

    if (tabKey === 'gallery') {
      this.gallery.fetchGallery();
    } else if (tabKey === 'limits') {
      void this.limits.open();
    } else if (tabKey === 'providers') {
      void this.providers?.open();
    } else if (tabKey === 'templates') {
      void this.templates?.open();
    }
  }

  async destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    window.removeEventListener('beforeunload', this.handleUnload);
    window.removeEventListener('pagehide', this.handleUnload);
    this.unsubscribeContext?.();
    this.unsubscribeContext = null;
    this.navCleanups.forEach((cleanup) => cleanup());
    this.navCleanups = [];
    this.limits?.destroy();
    this.providers?.destroy();
    this.templates?.destroy();
    this.progress?.destroy();
    this.gallery?.destroy();
    this.workbench?.destroy();
    this.stopwatch.stop();
    Modal.close(false);
    Lightbox.close();
    await this.sse.destroy();
  }
}

// 启动应用
document.addEventListener('DOMContentLoaded', () => {
  const app = new StudioApp();
  app.init().catch((err) => {
    console.error('[StudioApp] 启动失败:', err);
  });
});
