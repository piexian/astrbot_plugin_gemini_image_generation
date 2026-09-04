/**
 * Gemini 图像生成 Studio - 原生前端控制器 (app.js)
 * 纯静态无构建 / 原生 ES2020 / XSS 绝对防护 / 极简漫画令牌体系
 */

// ==========================================================================
// 1. IconSet (极简漫画 2px 手绘 SVG 图标字典，静态安全常量)
// ==========================================================================
const IconSet = {
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
  sourceLabel(source) {
    const labels = {
      command: '指令',
      llm_tool: 'LLM 工具',
      llm_batch: 'LLM 批量',
      webui: '工作台',
      legacy: '历史'
    };
    return labels[source] || source || '未知';
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
      img.src = cached;
      return;
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
        throw new Error('图片接口返回了无效数据');
      }
      const dataUri = `data:${mime};base64,${b64}`;
      this._remember(cacheKey, dataUri);
      if (img.imageLoaderRequestId === requestId) {
        img.src = dataUri;
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
// 3. BridgeClient (封装 window.AstrBotPluginPage 通信 SDK)
// ==========================================================================
class BridgeClient {
  static async init() {
    if (!window.AstrBotPluginPage) {
      throw new Error('未检测到 AstrBotPluginPage SDK，请在 AstrBot Dashboard 中运行');
    }
    return await window.AstrBotPluginPage.ready();
  }

  static isAvailable() {
    return !!window.AstrBotPluginPage;
  }

  static getContext() {
    return window.AstrBotPluginPage?.getContext?.() ?? null;
  }

  static onContext(handler) {
    return window.AstrBotPluginPage?.onContext?.(handler);
  }

  static async get(endpoint, params = {}) {
    if (!window.AstrBotPluginPage) {
      throw new Error('AstrBotPluginPage SDK 未加载');
    }
    return await window.AstrBotPluginPage.apiGet(endpoint, params);
  }

  static async post(endpoint, body = {}) {
    if (!window.AstrBotPluginPage) {
      throw new Error('AstrBotPluginPage SDK 未加载');
    }
    return await window.AstrBotPluginPage.apiPost(endpoint, body);
  }

  static async upload(endpoint, file) {
    if (!window.AstrBotPluginPage) {
      throw new Error('AstrBotPluginPage SDK 未加载');
    }
    return await window.AstrBotPluginPage.upload(endpoint, file);
  }

  static async download(endpoint, params = {}, filename = '') {
    if (!window.AstrBotPluginPage) {
      throw new Error('AstrBotPluginPage SDK 未加载');
    }
    return await window.AstrBotPluginPage.download(endpoint, params, filename);
  }

  static async subscribeSSE(endpoint, handlers = {}, params = {}) {
    if (!window.AstrBotPluginPage) {
      throw new Error('AstrBotPluginPage SDK 未加载');
    }
    return await window.AstrBotPluginPage.subscribeSSE(endpoint, handlers, params);
  }

  static async unsubscribeSSE(subscriptionId) {
    if (subscriptionId && window.AstrBotPluginPage?.unsubscribeSSE) {
      return await window.AstrBotPluginPage.unsubscribeSSE(subscriptionId);
    }
  }
}

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

  show(error, fallback = '操作失败') {
    const message = this.message(error);
    let display = message ? `${fallback}：${message}` : fallback;
    if (this.isAuth(error)) {
      display = '登录状态已失效，请刷新 Dashboard 并重新登录';
    } else if (/(?:\b429\b|并发.*(?:已满|上限)|队列已满)/i.test(message)) {
      display = '服务器并发处理中（任务队列已满），请稍候重试';
    } else if (/(?:\b507\b|暂存空间已满|存储空间不足)/i.test(message)) {
      display = '上传暂存空间已满，请等待自动清理或稍后再试';
    } else if (/(?:\b503\b|正在卸载|已关闭|重新加载|尚未初始化)/i.test(message)) {
      display = '插件正在重新加载，请稍后重试';
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
      Toast.error('复制失败，请检查浏览器剪贴板权限');
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

  confirm({ title = '确认操作', content = '', confirmText = '确认', cancelText = '取消', danger = false }) {
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

  openCustom({ title = '对话框', renderBody, renderFooter }) {
    if (this.backdrop.style.display !== 'none') {
      this.close(false);
    }
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
      const preferred = this.footerEl.querySelector('button:not(:disabled)') || this.closeBtn;
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
        const focusable = [this.closeBtn, this.prevBtn, this.nextBtn].filter((element) => element && element.style.display !== 'none');
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
      'aria-label': '原图加载中'
    });
    this.imgWrap?.replaceChildren(spinner, this.img);
    installImageFallback(this.img, this.imgWrap, '图片加载失败或已清理');
    this.img.addEventListener('load', () => {
      spinner.remove();
      this.img.style.display = 'block';
    }, { once: true });
    ImageLoader.attach(this.img, item.imageName, { thumb: false });
    SafeDOM.setText(this.promptEl, item.prompt || '无提示词记录');

    this.tagsEl.replaceChildren();
    if (item.provider) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`供应商: ${item.provider}`]));
    }
    if (item.model) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`模型: ${item.model}`]));
    }
    if (item.resolution) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`分辨率: ${item.resolution}`]));
    }
    if (item.aspect_ratio) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`比例: ${item.aspect_ratio}`]));
    }
    if (item.duration_ms) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`耗时: ${(item.duration_ms / 1000).toFixed(1)}s`]));
    }
    if (item.source) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`来源: ${SafeDOM.sourceLabel(item.source)}`]));
    }
    if (item.user_name) {
      this.tagsEl.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`用户: ${item.user_name}`]));
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
      user_id: ''
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
      const aRunning = a.status === 'running';
      const bRunning = b.status === 'running';
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
      if (job.status === 'running') count++;
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
          this.updateStatusIndicator('error', '登录状态已失效');
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

    this.updateStatusIndicator('connecting', '正在连接实时通道...');

    try {
      const subscriptionId = await BridgeClient.subscribeSSE('webui/jobs/stream', {
        onOpen: () => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.reconnectAttempts = 0;
          this.lastActiveTime = Date.now();
          this.updateStatusIndicator('connected', '实时连接就绪');
        },
        onMessage: (msg) => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.lastActiveTime = Date.now();
          this.handleMessage(msg);
        },
        onError: (error) => {
          if (this.destroyed || generation !== this.connectionGeneration) return;
          this.updateStatusIndicator('error', '连接异常，准备重试...');
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
        this.updateStatusIndicator('error', '登录状态已失效');
      } else {
        this.updateStatusIndicator('error', '连接异常，准备重试...');
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
class WorkbenchView {
  constructor(store, onJobSubmitted) {
    this.store = store;
    this.onJobSubmitted = onJobSubmitted;
    this.batchCounter = 0;
    this.batchBudgetLimit = 0;
    this.batchMaxTasks = 0;
    this.uploadMaxMb = 0;
    this.formAvailable = false;
    this.isSubmitting = false;
    this.isUploading = false;
    this.batchBudgetValid = true;
    this.referencesValid = true;
    this.moreParamsExpanded = false;

    // DOM 缓存
    this.promptInput = document.getElementById('input-prompt');
    this.promptCharCount = document.getElementById('prompt-char-count');
    this.modeBtnSingle = document.getElementById('mode-btn-single');
    this.modeBtnBatch = document.getElementById('mode-btn-batch');
    this.singleContainer = document.getElementById('single-form-container');
    this.batchContainer = document.getElementById('batch-form-container');
    this.batchList = document.getElementById('batch-items-list');
    this.btnAddBatchItem = document.getElementById('btn-add-batch-item');
    this.batchTotalCount = document.getElementById('batch-total-count');
    this.batchMaxBudget = document.getElementById('batch-max-budget');
    this.batchBudgetInfo = document.getElementById('batch-budget-info');

    this.selectModel = document.getElementById('select-model');
    this.selectResolution = document.getElementById('select-resolution');
    this.selectAspectRatio = document.getElementById('select-aspect-ratio');
    this.itemQuality = document.getElementById('item-quality');
    this.selectQuality = document.getElementById('select-quality');
    this.itemSeed = document.getElementById('item-seed');
    this.inputSeed = document.getElementById('input-seed');
    this.itemNegativePrompt = document.getElementById('item-negative-prompt');
    this.inputNegativePrompt = document.getElementById('input-negative-prompt');
    this.negCharCount = document.getElementById('neg-char-count');
    this.inputImageCount = document.getElementById('input-image-count');
    this.moreParamsDisclosure = document.getElementById('advanced-params-disclosure');
    this.btnToggleMoreParams = document.getElementById('btn-toggle-more-params');
    this.moreParamsPanel = document.getElementById('more-params-panel');
    this.moreParamsToggleIcon = document.getElementById('more-params-toggle-icon');

    this.btnSubmit = document.getElementById('btn-submit-generate');
    this.submitBtnText = document.getElementById('submit-btn-text');

    this.fileUploadInput = document.getElementById('file-upload-input');
    this.uploadLimitHint = document.getElementById('upload-max-size');
    this.btnTriggerUpload = document.getElementById('btn-trigger-upload');
    this.btnPickGallery = document.getElementById('btn-pick-gallery');
    this.referenceCounter = document.getElementById('reference-counter');
    this.referenceGrid = document.getElementById('reference-tray-grid');
    this.emptyTrayTip = document.getElementById('empty-tray-tip');

    this.globalBannerContainer = document.getElementById('global-banner-container');
    this.globalBannerText = document.getElementById('global-banner-text');

    this.initEvents();
  }

  initEvents() {
    // 字符数统计
    this.promptInput.addEventListener('input', () => {
      const len = this.promptInput.value.length;
      SafeDOM.setText(this.promptCharCount, `${len} / 2000`);
      if (len > 2000) {
        this.promptCharCount.classList.add('char-count--overflow');
      } else {
        this.promptCharCount.classList.remove('char-count--overflow');
      }
    });

    this.inputNegativePrompt.addEventListener('input', () => {
      const len = this.inputNegativePrompt.value.length;
      SafeDOM.setText(this.negCharCount, `${len} / 500`);
    });

    // 模式切换
    this.modeBtnSingle.addEventListener('click', () => this.switchMode('single'));
    this.modeBtnBatch.addEventListener('click', () => this.switchMode('batch'));
    [this.modeBtnSingle, this.modeBtnBatch].forEach((button) => {
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        const nextMode = this.store.mode === 'single' ? 'batch' : 'single';
        this.switchMode(nextMode);
        (nextMode === 'single' ? this.modeBtnSingle : this.modeBtnBatch).focus();
      });
    });

    // 批量添加子任务
    this.btnAddBatchItem.addEventListener('click', () => this.addBatchItem());

    // 模型（供应商·模型扁平选项）联动
    this.selectModel.addEventListener('change', () => this.handleModelChange());

    this.btnToggleMoreParams.addEventListener('click', () => {
      this.moreParamsExpanded = !this.moreParamsExpanded;
      this.syncMoreParamsPanel(!this.btnToggleMoreParams.hidden);
    });

    // 上传与画廊拾取
    this.btnTriggerUpload.addEventListener('click', () => this.fileUploadInput.click());
    this.fileUploadInput.addEventListener('change', (e) => this.handleFileUpload(e));
    this.btnPickGallery.addEventListener('click', () => this.openGalleryPicker());

    // 提交
    this.btnSubmit.addEventListener('click', () => this.submit());
  }

  async loadCapabilities() {
    try {
      const data = await BridgeClient.get('webui/capabilities');
      const models = Array.isArray(data?.models) ? data.models : [];
      this.store.capabilities = models;
      const limits = data?.limits;
      if (!limits || !['batch_total_budget', 'batch_max_tasks', 'upload_max_mb'].every(
        (key) => Number.isSafeInteger(limits[key]) && limits[key] > 0
      )) {
        throw new Error('模型能力响应缺少有效限制');
      }
      this.batchBudgetLimit = limits.batch_total_budget;
      this.batchMaxTasks = limits.batch_max_tasks;
      this.uploadMaxMb = limits.upload_max_mb;
      SafeDOM.setText(this.uploadLimitHint, `${this.uploadMaxMb}MB`);
      SafeDOM.setText(this.batchMaxBudget, String(this.batchBudgetLimit));

      if (models.length === 0) {
        this.showGlobalBanner('未检测到可用的图像生成供应商，请在插件配置中添加 provider_candidates 后刷新');
        this.setFormDisabled(true);
        return;
      }

      this.hideGlobalBanner();
      this.setFormDisabled(false);
      this.populateModels(models);
      this.calculateBatchBudget();
    } catch (err) {
      console.error('[WorkbenchView] 加载 capabilities 失败:', err);
      const message = ErrorFeedback.isAuth(err)
        ? '登录状态已失效，请刷新 Dashboard 并重新登录'
        : '加载模型能力失败，请检查插件状态或网络连接';
      this.showGlobalBanner(message);
      this.setFormDisabled(true);
    }
  }

  showGlobalBanner(msg) {
    SafeDOM.setText(this.globalBannerText, msg);
    this.globalBannerContainer.style.display = 'block';
  }

  hideGlobalBanner() {
    this.globalBannerContainer.style.display = 'none';
  }

  setFormDisabled(disabled) {
    this.formAvailable = !disabled;
    [
      this.promptInput,
      this.selectModel,
      this.selectResolution,
      this.selectAspectRatio,
      this.selectQuality,
      this.inputSeed,
      this.inputNegativePrompt,
      this.inputImageCount,
      this.modeBtnSingle,
      this.modeBtnBatch,
      this.btnAddBatchItem
    ].forEach((element) => {
      element.disabled = disabled;
    });
    this.updateReferenceCounter();
    this.updateActionState();
  }

  updateActionState() {
    this.btnSubmit.disabled = !this.formAvailable
      || this.isSubmitting
      || this.isUploading
      || !this.batchBudgetValid
      || !this.referencesValid
      || (this.store.mode === 'batch' && this.store.batchItems.length === 0);
  }

  populateModels(models) {
    // 扁平候选列表按供应商分组渲染 optgroup，option value 为候选下标
    this.selectModel.replaceChildren();
    const groups = new Map();
    models.forEach((entry, index) => {
      const groupLabel = entry.provider_display || entry.provider || '其他';
      if (!groups.has(groupLabel)) groups.set(groupLabel, []);
      groups.get(groupLabel).push({ entry, index });
    });
    for (const [label, items] of groups) {
      const group = SafeDOM.el('optgroup', { label });
      for (const { entry, index } of items) {
        const modelName = entry.model || '默认模型';
        const displayName = entry.model_alias ? `${modelName}（${entry.model_alias}）` : modelName;
        const text = `${displayName} · ${entry.candidate_id}`;
        group.appendChild(SafeDOM.el('option', { value: String(index) }, [text]));
      }
      this.selectModel.appendChild(group);
    }
    this.handleModelChange();
  }

  selectedModel() {
    const index = Number(this.selectModel.value);
    if (!Number.isInteger(index)) return null;
    return this.store.capabilities[index] || null;
  }

  handleModelChange() {
    const entry = this.selectedModel();
    if (!entry) return;

    // 分辨率
    this.selectResolution.replaceChildren();
    const resolutions = entry.resolutions || [];
    if (resolutions.length > 0) {
      document.getElementById('item-resolution').style.display = 'block';
      for (const r of resolutions) {
        this.selectResolution.appendChild(SafeDOM.el('option', { value: r }, [r]));
      }
    } else {
      document.getElementById('item-resolution').style.display = 'none';
    }

    // 画面比例
    this.selectAspectRatio.replaceChildren();
    const ratios = entry.aspect_ratios || [];
    if (ratios.length > 0) {
      document.getElementById('item-aspect-ratio').style.display = 'block';
      for (const ratio of ratios) {
        this.selectAspectRatio.appendChild(SafeDOM.el('option', { value: ratio }, [ratio]));
      }
    } else {
      document.getElementById('item-aspect-ratio').style.display = 'none';
    }

    // 画质 quality
    const params = entry.parameters || {};
    const supportsQuality = Boolean(
      params.quality && Array.isArray(params.quality.enum) && params.quality.enum.length > 0
    );
    if (supportsQuality) {
      this.itemQuality.style.display = 'block';
      this.selectQuality.replaceChildren();
      for (const q of params.quality.enum) {
        this.selectQuality.appendChild(SafeDOM.el('option', { value: q }, [q]));
      }
    } else {
      this.itemQuality.style.display = 'none';
    }

    // 随机种子
    const supportsSeed = params.seed?.type === 'integer';
    if (supportsSeed) {
      this.itemSeed.style.display = 'block';
      for (const bound of ['minimum', 'maximum']) {
        const value = params.seed[bound];
        const attribute = bound === 'minimum' ? 'min' : 'max';
        if (Number.isSafeInteger(value)) {
          this.inputSeed.setAttribute(attribute, String(value));
        } else {
          this.inputSeed.removeAttribute(attribute);
        }
      }
    } else {
      this.itemSeed.style.display = 'none';
      this.inputSeed.value = '';
      this.inputSeed.removeAttribute('min');
      this.inputSeed.removeAttribute('max');
    }

    // 负向提示词
    const supportsNegativePrompt = Boolean(params.negative_prompt);
    if (supportsNegativePrompt) {
      this.itemNegativePrompt.style.display = 'block';
    } else {
      this.itemNegativePrompt.style.display = 'none';
      this.inputNegativePrompt.value = '';
    }
    this.syncMoreParamsPanel(
      supportsQuality || supportsSeed || supportsNegativePrompt
    );

    // 单任务张数最大值
    if (params.image_count && typeof params.image_count.maximum === 'number') {
      this.inputImageCount.max = String(params.image_count.maximum);
    } else {
      this.inputImageCount.max = '10';
    }
    const maxImageCount = Number(this.inputImageCount.max) || 10;
    this.inputImageCount.value = String(Math.min(Math.max(parseInt(this.inputImageCount.value, 10) || 1, 1), maxImageCount));

    this.updateReferenceCounter();
  }

  syncMoreParamsPanel(hasAdvancedParameters) {
    this.moreParamsDisclosure.hidden = !hasAdvancedParameters;
    this.btnToggleMoreParams.hidden = !hasAdvancedParameters;
    const isExpanded = hasAdvancedParameters && this.moreParamsExpanded;
    this.btnToggleMoreParams.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
    this.moreParamsPanel.hidden = !isExpanded;
    SafeDOM.setSvgIcon(
      this.moreParamsToggleIcon,
      isExpanded ? 'chevronUp' : 'chevronDown'
    );
  }

  switchMode(mode) {
    this.store.mode = mode;
    if (mode === 'single') {
      this.modeBtnSingle.classList.add('mode-btn--active');
      this.modeBtnSingle.setAttribute('aria-checked', 'true');
      this.modeBtnSingle.tabIndex = 0;
      this.modeBtnBatch.classList.remove('mode-btn--active');
      this.modeBtnBatch.setAttribute('aria-checked', 'false');
      this.modeBtnBatch.tabIndex = -1;

      this.singleContainer.style.display = 'block';
      this.batchContainer.style.display = 'none';
    } else {
      this.modeBtnBatch.classList.add('mode-btn--active');
      this.modeBtnBatch.setAttribute('aria-checked', 'true');
      this.modeBtnBatch.tabIndex = 0;
      this.modeBtnSingle.classList.remove('mode-btn--active');
      this.modeBtnSingle.setAttribute('aria-checked', 'false');
      this.modeBtnSingle.tabIndex = -1;

      this.singleContainer.style.display = 'none';
      this.batchContainer.style.display = 'block';

      if (this.store.batchItems.length === 0) {
        this.addBatchItem();
      }
    }
    this.calculateBatchBudget();
  }

  addBatchItem(initialName = '', initialPrompt = '', initialCount = 1) {
    if (this.store.batchItems.length >= this.batchMaxTasks) {
      Toast.warning(`批量任务数不能超过 ${this.batchMaxTasks}`);
      return;
    }
    this.batchCounter++;
    const itemId = `batch_item_${this.batchCounter}`;
    const name = initialName || `子任务 ${this.batchCounter}`;
    const item = {
      id: itemId,
      name,
      prompt: initialPrompt,
      image_count: initialCount
    };
    this.store.batchItems.push(item);
    this.renderBatchItem(item);
    this.calculateBatchBudget();
  }

  renderBatchItem(item) {
    const row = SafeDOM.el('div', {
      className: 'batch-item-row',
      id: `row-${item.id}`
    });

    const head = SafeDOM.el('div', { className: 'batch-item-head' });
    const nameInput = SafeDOM.el('input', {
      type: 'text',
      className: 'comic-input batch-item-name',
      value: item.name,
      placeholder: '子任务名称',
      maxlength: '64',
      onInput: (e) => {
        item.name = e.target.value;
      }
    });

    const countWrap = SafeDOM.el('div', { className: 'batch-item-count-wrap' }, ['张数:']);
    const countInput = SafeDOM.el('input', {
      type: 'number',
      className: 'comic-input batch-item-count',
      min: '1',
      max: '10',
      value: String(item.image_count),
      onInput: (e) => {
        const val = parseInt(e.target.value, 10) || 1;
        item.image_count = Math.max(1, Math.min(10, val));
        this.calculateBatchBudget();
      }
    });
    countWrap.appendChild(countInput);

    const delBtn = SafeDOM.el('button', {
      type: 'button',
      className: 'comic-btn comic-btn--danger comic-btn--sm',
      onClick: () => {
        this.removeBatchItem(item.id);
      }
    });
    SafeDOM.setSvgIcon(delBtn, 'trash');

    head.appendChild(nameInput);
    head.appendChild(countWrap);
    head.appendChild(delBtn);

    const promptTextarea = SafeDOM.el('textarea', {
      className: 'comic-input comic-textarea',
      rows: '2',
      maxlength: '2000',
      placeholder: '子任务提示词...',
      value: item.prompt,
      onInput: (e) => {
        item.prompt = e.target.value;
      }
    });

    row.appendChild(head);
    row.appendChild(promptTextarea);
    this.batchList.appendChild(row);
  }

  removeBatchItem(itemId) {
    this.store.batchItems = this.store.batchItems.filter((it) => it.id !== itemId);
    const row = document.getElementById(`row-${itemId}`);
    if (row && row.parentNode) {
      row.parentNode.removeChild(row);
    }
    this.calculateBatchBudget();
  }

  calculateBatchBudget() {
    const total = this.store.batchItems.reduce((acc, it) => acc + (it.image_count || 1), 0);
    SafeDOM.setText(this.batchTotalCount, String(total));
    this.batchBudgetValid = this.store.mode !== 'batch'
      || (total > 0 && total <= this.batchBudgetLimit && this.store.batchItems.length <= this.batchMaxTasks);
    this.btnAddBatchItem.disabled = !this.formAvailable || this.store.batchItems.length >= this.batchMaxTasks;

    if (total > this.batchBudgetLimit) {
      this.batchBudgetInfo.classList.add('batch-budget-indicator--danger');
      Toast.warning(`总预算消耗 ${total} 张超出上限 ${this.batchBudgetLimit} 张`);
    } else {
      this.batchBudgetInfo.classList.remove('batch-budget-indicator--danger');
    }
    this.updateActionState();
  }

  getMaxReferenceImages() {
    const maximum = this.selectedModel()?.max_reference_images;
    return Number.isSafeInteger(maximum) && maximum >= 0 ? maximum : 0;
  }

  updateReferenceCounter() {
    const max = this.getMaxReferenceImages();
    const current = this.store.referenceTray.length;
    SafeDOM.setText(this.referenceCounter, `${current} / ${max}`);

    this.referencesValid = current <= max;
    this.referenceCounter.classList.toggle('tray-counter--danger', !this.referencesValid);
    const isFull = current >= max;
    this.btnTriggerUpload.disabled = !this.formAvailable || this.isSubmitting || this.isUploading || isFull;
    this.btnPickGallery.disabled = !this.formAvailable || this.isSubmitting || this.isUploading || isFull;
    this.updateActionState();
  }

  renderReferenceTray() {
    this.referenceGrid.replaceChildren();

    if (this.store.referenceTray.length === 0) {
      this.referenceGrid.appendChild(this.emptyTrayTip);
      this.emptyTrayTip.style.display = 'flex';
      this.updateReferenceCounter();
      return;
    }

    this.emptyTrayTip.style.display = 'none';

    this.store.referenceTray.forEach((item, index) => {
      const itemEl = SafeDOM.el('div', { className: 'tray-item' });

      const isLocalBlob = item.type === 'upload' && typeof item.previewUrl === 'string' && item.previewUrl.startsWith('blob:');
      if (!isLocalBlob && !SafeDOM.isSafeImageName(item.name)) return;

      const imgEl = SafeDOM.el('img', { alt: '参考图' });
      if (isLocalBlob) {
        imgEl.src = item.previewUrl;
      } else {
        installImageFallback(imgEl, itemEl, '图片加载失败或已清理');
        ImageLoader.attach(imgEl, item.name, { thumb: true });
      }

      const badgeEl = SafeDOM.el('span', { className: 'tray-item-badge' }, [
        item.type === 'upload' ? '本地' : '画廊'
      ]);

      const removeBtn = SafeDOM.el('button', {
        type: 'button',
        className: 'tray-item-remove',
        'aria-label': '移除参考图',
        onClick: () => {
          this.removeReferenceItem(index);
        }
      });
      SafeDOM.setSvgIcon(removeBtn, 'x');

      itemEl.appendChild(imgEl);
      itemEl.appendChild(badgeEl);
      itemEl.appendChild(removeBtn);
      this.referenceGrid.appendChild(itemEl);
    });

    this.updateReferenceCounter();
  }

  removeReferenceItem(index) {
    const item = this.store.referenceTray[index];
    if (item && item.type === 'upload' && typeof item.previewUrl === 'string' && item.previewUrl.startsWith('blob:')) {
      try {
        URL.revokeObjectURL(item.previewUrl);
      } catch (e) { /* ignore */ }
    }
    this.store.referenceTray.splice(index, 1);
    this.renderReferenceTray();
  }

  async handleFileUpload(e) {
    if (this.isUploading) return;
    const files = Array.from(e.target.files || []);
    this.fileUploadInput.value = '';
    if (files.length === 0) return;

    const max = this.getMaxReferenceImages();
    const current = this.store.referenceTray.length;
    const remaining = max - current;

    if (remaining <= 0) {
      Toast.warning('参考图数量已达上限');
      return;
    }

    this.isUploading = true;
    this.updateReferenceCounter();
    try {
      const toUpload = files.slice(0, remaining);
      if (files.length > toUpload.length) {
        Toast.warning(`仅上传前 ${toUpload.length} 张，参考图数量不能超过 ${max} 张`);
      }
      for (const file of toUpload) {
        if (file.type && !['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp'].includes(file.type)) {
          Toast.error(`文件 ${file.name} 不是支持的图片格式`);
          continue;
        }
        if (file.size > this.uploadMaxMb * 1024 * 1024) {
          Toast.error(`文件 ${file.name} 大小超过 ${this.uploadMaxMb}MB 限制`);
          continue;
        }

        try {
          Toast.info(`正在上传 ${file.name}...`);
          const res = await BridgeClient.upload('webui/upload', file);
          const names = Array.isArray(res?.names)
            ? res.names.filter((name) => SafeDOM.isSafeImageName(name))
            : [];
          if (names.length === 0) {
            throw new Error('上传接口未返回有效图片文件名');
          }
          for (const name of names) {
            if (this.store.referenceTray.length >= max) break;
            const blobUrl = URL.createObjectURL(file);
            this.store.referenceTray.push({
              id: `ref_${Date.now()}_${Math.random()}`,
              type: 'upload',
              name,
              previewUrl: blobUrl,
              file
            });
          }
        } catch (err) {
          console.error('[WorkbenchView] 上传参考图失败:', err);
          ErrorFeedback.show(err, '上传失败');
        }
      }
    } finally {
      this.isUploading = false;
      this.renderReferenceTray();
    }
  }

  async openGalleryPicker() {
    const max = this.getMaxReferenceImages();
    const current = this.store.referenceTray.length;
    const remaining = max - current;
    if (remaining <= 0) {
      Toast.warning('参考图数量已达上限');
      return;
    }

    try {
      const historyData = await BridgeClient.get('webui/history', { page: 1, size: 24 });
      const items = Array.isArray(historyData?.items) ? historyData.items : [];

      // 提取有有效图片的项
      const imageItems = [];
      for (const job of items) {
        if (Array.isArray(job.images)) {
          for (const imgName of job.images) {
            if (SafeDOM.isSafeImageName(imgName)) {
              if (this.store.referenceTray.some((entry) => entry.type === 'gallery' && entry.name === imgName)) {
                continue;
              }
              imageItems.push({
                job_id: job.job_id,
                imageName: imgName,
                prompt: job.prompt
              });
            }
          }
        }
      }

      if (imageItems.length === 0) {
        Toast.info('历史画廊中暂无可用图片');
        return;
      }

      const selectedNames = new Set();

      Modal.openCustom({
        title: `从历史画廊拾取参考图 (最多可选 ${remaining} 张)`,
        renderBody: (container) => {
          const grid = SafeDOM.el('div', {
            style: {
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))',
              gap: '10px',
              maxHeight: '360px',
              overflowY: 'auto',
              padding: '4px'
            }
          });

          imageItems.forEach((item, index) => {
            const card = SafeDOM.el('button', {
              type: 'button',
              className: 'gallery-picker-item',
              'aria-label': `选择第 ${index + 1} 张参考图`,
              'aria-pressed': 'false'
            });

            const img = SafeDOM.el('img', {
              alt: '历史参考图',
              style: { width: '100%', height: '100%', objectFit: 'cover' }
            });
            installImageFallback(img, card, '图片加载失败或已清理');
            ImageLoader.attach(img, item.imageName, { thumb: true });

            const checkBadge = SafeDOM.el('span', {
              style: {
                position: 'absolute',
                top: '4px',
                right: '4px',
                width: '18px',
                height: '18px',
                backgroundColor: 'var(--pop)',
                border: '1px solid var(--ink)',
                display: 'none',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '11px',
                fontWeight: 'bold'
              }
            });
            SafeDOM.setSvgIcon(checkBadge, 'check');

            card.addEventListener('click', () => {
              if (selectedNames.has(item.imageName)) {
                selectedNames.delete(item.imageName);
                card.style.borderColor = 'var(--ink)';
                checkBadge.style.display = 'none';
                card.setAttribute('aria-pressed', 'false');
              } else {
                if (selectedNames.size >= remaining) {
                  Toast.warning(`单次最多还能选择 ${remaining} 张参考图`);
                  return;
                }
                selectedNames.add(item.imageName);
                card.style.borderColor = 'var(--pop)';
                checkBadge.style.display = 'flex';
                card.setAttribute('aria-pressed', 'true');
              }
            });

            card.appendChild(img);
            card.appendChild(checkBadge);
            grid.appendChild(card);
          });

          container.appendChild(grid);
        },
        renderFooter: (footer, onConfirm, onCancel) => {
          const cancelBtn = SafeDOM.el('button', {
            type: 'button',
            className: 'comic-btn comic-btn--outline',
            onClick: onCancel
          }, ['取消']);

          const confirmBtn = SafeDOM.el('button', {
            type: 'button',
            className: 'comic-btn comic-btn--cta',
            onClick: () => {
              for (const name of selectedNames) {
                this.store.referenceTray.push({
                  id: `ref_${Date.now()}_${Math.random()}`,
                  type: 'gallery',
                  name,
                  previewUrl: ''
                });
              }
              this.renderReferenceTray();
              onConfirm();
            }
          }, ['确认添加选中项']);

          footer.appendChild(cancelBtn);
          footer.appendChild(confirmBtn);
        }
      });
    } catch (err) {
      console.error('[WorkbenchView] 获取画廊列表失败:', err);
      ErrorFeedback.show(err, '获取画廊图片失败');
    }
  }

  // 外部直接添加画廊参考图（从画廊卡片操作触发）
  addGalleryReference(imageName) {
    if (!this.formAvailable) {
      Toast.warning('当前没有可用的图像生成供应商');
      return false;
    }
    if (!SafeDOM.isSafeImageName(imageName)) {
      Toast.error('图片文件名无效，无法设为参考图');
      return false;
    }
    if (this.store.referenceTray.some((item) => item.type === 'gallery' && item.name === imageName)) {
      Toast.info('该图片已在参考图托盘中');
      return false;
    }
    const max = this.getMaxReferenceImages();
    if (this.store.referenceTray.length >= max) {
      Toast.warning('参考图数量已达上限');
      return false;
    }
    this.store.referenceTray.push({
      id: `ref_${Date.now()}_${Math.random()}`,
      type: 'gallery',
      name: imageName,
      previewUrl: ''
    });
    this.renderReferenceTray();
    Toast.success('已添加到参考图托盘');
    return true;
  }

  // 回填工作台参数（再次生成）
  refillParameters(prompt, params = {}) {
    this.switchMode('single');
    this.promptInput.value = prompt || '';
    SafeDOM.setText(this.promptCharCount, `${this.promptInput.value.length} / 2000`);

    let fellBack = false;
    if (params.provider || params.model || params.candidate_id) {
      const matchIndex = this.store.capabilities.findIndex(
        (entry) => (!params.provider || entry.provider === params.provider)
          && (!params.model || entry.model === params.model)
          && (!params.candidate_id || entry.candidate_id === params.candidate_id)
      );
      if (matchIndex >= 0) {
        this.selectModel.value = String(matchIndex);
        this.handleModelChange();
      } else {
        fellBack = true;
      }
    }
    if (params.resolution) {
      if (Array.from(this.selectResolution.options).some((o) => o.value === params.resolution)) {
        this.selectResolution.value = params.resolution;
      } else {
        fellBack = true;
      }
    }
    if (params.aspect_ratio) {
      if (Array.from(this.selectAspectRatio.options).some((o) => o.value === params.aspect_ratio)) {
        this.selectAspectRatio.value = params.aspect_ratio;
      } else {
        fellBack = true;
      }
    }
    if (params.quality && this.itemQuality.style.display !== 'none') {
      if (Array.from(this.selectQuality.options).some((o) => o.value === params.quality)) {
        this.selectQuality.value = params.quality;
      } else {
        fellBack = true;
      }
    }
    if (params.seed !== null && params.seed !== undefined
        && this.itemSeed.style.display !== 'none') {
      const seed = Number(params.seed);
      const minimum = this.inputSeed.hasAttribute('min')
        ? Number(this.inputSeed.min)
        : Number.NEGATIVE_INFINITY;
      const maximum = this.inputSeed.hasAttribute('max')
        ? Number(this.inputSeed.max)
        : Number.POSITIVE_INFINITY;
      if (Number.isSafeInteger(seed) && seed >= minimum && seed <= maximum) {
        this.inputSeed.value = String(seed);
      } else {
        this.inputSeed.value = '';
        fellBack = true;
      }
    }
    if (params.image_count) {
      const maximum = Number(this.inputImageCount.max) || 10;
      const count = Math.min(Math.max(Number(params.image_count) || 1, 1), maximum);
      this.inputImageCount.value = String(count);
      if (count !== Number(params.image_count)) fellBack = true;
    }
    this.promptInput.focus();
    if (fellBack) {
      Toast.warning('已回填可用参数；原供应商、模型或画质不可用，已回退当前默认值');
    } else {
      Toast.info('已回填历史任务参数');
    }
  }

  async submit() {
    if (this.isSubmitting || !this.formAvailable) return;
    this.calculateBatchBudget();
    this.updateReferenceCounter();
    if (!this.batchBudgetValid || !this.referencesValid || this.isUploading) return;
    const isBatch = this.store.mode === 'batch';
    let promptVal = this.promptInput.value.trim();
    let batchPayload = null;

    if (!isBatch) {
      if (!promptVal) {
        Toast.warning('请输入提示词');
        this.promptInput.focus();
        return;
      }
    } else {
      if (this.store.batchItems.length === 0) {
        Toast.warning('请至少添加一个批量子任务');
        return;
      }
      for (const item of this.store.batchItems) {
        if (!item.prompt || !item.prompt.trim()) {
          Toast.warning(`子任务 "${item.name}" 缺少提示词`);
          return;
        }
      }
      batchPayload = this.store.batchItems.map((it) => ({
        name: it.name.trim() || '子任务',
        prompt: it.prompt.trim(),
        image_count: it.image_count || 1
      }));
    }

    const uploadNames = [];
    const referenceNames = [];
    for (const it of this.store.referenceTray) {
      if (it.type === 'upload') {
        uploadNames.push(it.name);
      } else {
        referenceNames.push(it.name);
      }
    }

    const selectedModel = this.selectedModel();
    let seed = null;
    if (this.itemSeed.style.display !== 'none' && this.inputSeed.value.trim()) {
      seed = Number(this.inputSeed.value);
      const minimum = this.inputSeed.hasAttribute('min')
        ? Number(this.inputSeed.min)
        : Number.NEGATIVE_INFINITY;
      const maximum = this.inputSeed.hasAttribute('max')
        ? Number(this.inputSeed.max)
        : Number.POSITIVE_INFINITY;
      if (!Number.isSafeInteger(seed) || seed < minimum || seed > maximum) {
        Toast.warning('随机种子必须是有效范围内的整数');
        this.inputSeed.focus();
        return;
      }
    }
    const payload = {
      provider: selectedModel?.provider || null,
      model: selectedModel?.model || null,
      candidate_id: selectedModel?.candidate_id || null,
      resolution: this.selectResolution.value || null,
      aspect_ratio: this.selectAspectRatio.value || null,
      quality: this.itemQuality.style.display !== 'none' ? this.selectQuality.value : null,
      seed,
      negative_prompt: this.itemNegativePrompt.style.display !== 'none' ? this.inputNegativePrompt.value.trim() || null : null,
      image_count: parseInt(this.inputImageCount.value, 10) || 1,
      reference_names: referenceNames,
      upload_names: uploadNames
    };
    if (isBatch) {
      payload.batch = batchPayload;
    } else {
      payload.prompt = promptVal;
      payload.batch = null;
    }

    this.isSubmitting = true;
    this.updateReferenceCounter();
    SafeDOM.setText(this.submitBtnText, '正在提交任务...');

    try {
      const res = await BridgeClient.post('webui/generate', payload);
      if (res?.warning) {
        Toast.warning(res.warning, 5000);
      }
      Toast.success('任务已成功提交入队');

      const newJobId = res?.job_id;
      if (newJobId) {
        this.store.highlightJobId = newJobId;
      }

      if (typeof this.onJobSubmitted === 'function') {
        this.onJobSubmitted(newJobId);
      }
    } catch (err) {
      console.error('[WorkbenchView] 提交生成任务失败:', err);
      ErrorFeedback.show(err, '提交失败');
    } finally {
      this.isSubmitting = false;
      this.updateReferenceCounter();
      SafeDOM.setText(this.submitBtnText, '立即生成图像');
    }
  }

  destroy() {
    for (const item of this.store.referenceTray) {
      if (item.type === 'upload' && typeof item.previewUrl === 'string' && item.previewUrl.startsWith('blob:')) {
        URL.revokeObjectURL(item.previewUrl);
      }
    }
  }
}

// ==========================================================================
// 11. ProgressView (任务进度面板：卡片渲染、父子聚合、状态秒表)
// ==========================================================================
class ProgressView {
  constructor(store, onRefillRequest, onSetReference) {
    this.store = store;
    this.onRefillRequest = onRefillRequest;
    this.onSetReference = onSetReference;
    this.unsubscribeJobs = null;
    this.highlightTimer = null;

    this.container = document.getElementById('jobs-stream-container');
    this.emptyTip = document.getElementById('empty-jobs-tip');
    this.statActive = document.getElementById('stat-active-count');
    this.statRecent = document.getElementById('stat-recent-count');
    this.tabBadge = document.getElementById('active-jobs-count');
    this.btnRefresh = document.getElementById('btn-refresh-jobs');

    this.initEvents();
  }

  initEvents() {
    this.unsubscribeJobs = this.store.on('jobs_updated', () => this.render());
    this.btnRefresh.addEventListener('click', async () => {
      try {
        const records = await BridgeClient.get('webui/jobs');
        if (Array.isArray(records)) {
          this.store.applySnapshot(records);
          Toast.success('任务状态已刷新');
        }
      } catch (err) {
        ErrorFeedback.show(err, '刷新任务状态失败');
      }
    });
  }

  render() {
    const runningCount = this.store.getRunningJobsCount();
    const recentCount = this.store.getRecentJobsCount();

    SafeDOM.setText(this.statActive, String(runningCount));
    SafeDOM.setText(this.statRecent, String(recentCount));

    if (runningCount > 0) {
      this.tabBadge.style.display = 'inline-block';
      SafeDOM.setText(this.tabBadge, String(runningCount));
    } else {
      this.tabBadge.style.display = 'none';
    }

    const allJobs = this.store.getJobsList();
    if (allJobs.length === 0) {
      this.container.replaceChildren();
      this.container.appendChild(this.emptyTip);
      this.emptyTip.style.display = 'flex';
      return;
    }

    this.emptyTip.style.display = 'none';

    // 树形结构归类：将具备 parent_job_id 的子任务归入父卡片
    const parents = [];
    const childrenMap = new Map(); // parent_job_id -> JobRecord[]
    for (const job of allJobs) {
      if (job.parent_job_id) {
        if (!childrenMap.has(job.parent_job_id)) {
          childrenMap.set(job.parent_job_id, []);
        }
        childrenMap.get(job.parent_job_id).push(job);
      } else {
        parents.push(job);
      }
    }

    const fragment = document.createDocumentFragment();

    for (const parent of parents) {
      const children = childrenMap.get(parent.job_id) || [];
      const card = children.length > 0
        ? this.createBatchParentCard(parent, children)
        : this.createSingleJobCard(parent);
      fragment.appendChild(card);
    }

    // 容灾：处理父任务不在列表但子任务在的情况
    for (const [pId, cList] of childrenMap.entries()) {
      if (!parents.some((p) => p.job_id === pId)) {
        for (const child of cList) {
          fragment.appendChild(this.createSingleJobCard(child));
        }
      }
    }

    this.container.replaceChildren(fragment);

    // 检查高亮滚动
    if (this.store.highlightJobId) {
      const highlightEl = document.getElementById(`job-${this.store.highlightJobId}`);
      if (highlightEl) {
        highlightEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (this.highlightTimer) {
          clearTimeout(this.highlightTimer);
        }
        const highlightedJobId = this.store.highlightJobId;
        this.highlightTimer = setTimeout(() => {
          highlightEl.classList.remove('job-card--highlight');
          if (this.store.highlightJobId === highlightedJobId) {
            this.store.highlightJobId = null;
          }
          this.highlightTimer = null;
        }, 2500);
      }
    }
  }

  createStatusPill(status) {
    const allowedStatuses = new Set(['running', 'succeeded', 'partial_success', 'failed', 'interrupted']);
    const normalizedStatus = allowedStatuses.has(status) ? status : 'interrupted';
    const pill = SafeDOM.el('span', {
      className: `status-pill status-pill--${normalizedStatus}`
    });
    const dot = SafeDOM.el('span', { className: 'status-dot' });
    const textMap = {
      running: '进行中',
      succeeded: '已完成',
      partial_success: '部分成功',
      failed: '失败',
      interrupted: '已中断'
    };
    pill.appendChild(dot);
    pill.appendChild(SafeDOM.text(textMap[status] || status || '未知状态'));
    return pill;
  }

  createSingleJobCard(record) {
    const isHighlight = record.job_id === this.store.highlightJobId;
    const card = SafeDOM.el('div', {
      className: `job-card ${isHighlight ? 'job-card--highlight' : ''}`,
      id: `job-${record.job_id}`,
      dataset: {
        status: record.status,
        createdAt: record.created_at || ''
      }
    });

    // 头部
    const header = SafeDOM.el('div', { className: 'job-header' });
    const headerLeft = SafeDOM.el('div', { className: 'job-header-left' });

    const sourceTag = SafeDOM.el('span', { className: 'job-source-tag' }, [SafeDOM.sourceLabel(record.source)]);
    const idSpan = SafeDOM.el('button', {
      type: 'button',
      className: 'job-id-text',
      'aria-label': '复制 Job ID',
      onClick: () => {
        void Clipboard.copy(record.job_id, `已复制 Job ID: ${record.job_id}`);
      }
    }, [record.job_id]);
    const copyIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(copyIcon, 'copy');
    idSpan.appendChild(copyIcon);

    headerLeft.appendChild(sourceTag);
    headerLeft.appendChild(idSpan);

    const headerRight = SafeDOM.el('div', { className: 'job-header-right' });
    const statusPill = this.createStatusPill(record.status);

    let durationText = '00:00';
    if (record.status === 'running') {
      const createdAt = new Date(record.created_at || '').getTime();
      const diff = Number.isFinite(createdAt)
        ? Math.max(0, Math.floor((Date.now() - createdAt) / 1000))
        : 0;
      const mins = String(Math.floor(diff / 60)).padStart(2, '0');
      const secs = String(diff % 60).padStart(2, '0');
      durationText = `${mins}:${secs}`;
    } else if (record.duration_ms) {
      durationText = `${(record.duration_ms / 1000).toFixed(1)}s`;
    }
    const durationSpan = SafeDOM.el('span', { className: 'job-duration' }, [durationText]);

    headerRight.appendChild(statusPill);
    headerRight.appendChild(durationSpan);

    header.appendChild(headerLeft);
    header.appendChild(headerRight);
    card.appendChild(header);

    // 提示词区域
    if (record.prompt) {
      const promptBox = SafeDOM.el('div', { className: 'job-prompt-box' }, [record.prompt]);
      card.appendChild(promptBox);
    }

    // 元数据行
    const metaRow = SafeDOM.el('div', { className: 'job-meta-row' });
    const p = record.params || {};
    if (p.provider) metaRow.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`供应商: ${p.provider}`]));
    if (p.model) metaRow.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`模型: ${p.model}`]));
    if (p.resolution) metaRow.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`分辨率: ${p.resolution}`]));
    if (p.aspect_ratio) metaRow.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`比例: ${p.aspect_ratio}`]));
    metaRow.appendChild(SafeDOM.el('span', { className: 'meta-pill' }, [`张数: ${record.generated_images || 0} / ${record.requested_images || 1}`]));
    card.appendChild(metaRow);

    // 生成图片网格
    const safeImages = Array.isArray(record.images)
      ? record.images.filter((name) => SafeDOM.isSafeImageName(name))
      : [];
    if (safeImages.length > 0) {
      const thumbsGrid = SafeDOM.el('div', { className: 'job-thumbs-grid' });
      safeImages.forEach((imgName, idx) => {
        const thumbItem = SafeDOM.el('button', {
          type: 'button',
          className: 'job-thumb-item',
          'aria-label': `查看生成结果 ${idx + 1}`,
          onClick: () => {
            const previewItems = safeImages.map((name) => ({
              imageName: name,
              prompt: record.prompt,
              provider: p.provider,
              model: p.model,
              resolution: p.resolution,
              aspect_ratio: p.aspect_ratio,
              duration_ms: record.duration_ms,
              source: record.source,
              user_name: record.requester?.user_name
            }));
            Lightbox.open(previewItems, idx);
          }
        });
        const img = SafeDOM.el('img', {
          loading: 'lazy',
          alt: '生成结果'
        });
        installImageFallback(img, thumbItem, '图片加载失败或已清理');
        ImageLoader.attach(img, imgName, { thumb: true });
        thumbItem.appendChild(img);
        thumbsGrid.appendChild(thumbItem);
      });
      card.appendChild(thumbsGrid);
    }

    // 错误面板
    if (record.error) {
      const errBox = SafeDOM.el('div', { className: 'job-error-box' });
      const errTitle = SafeDOM.el('div', { className: 'job-error-title' }, ['执行失败详情']);
      const errText = SafeDOM.el('div', {}, [record.error]);
      errBox.appendChild(errTitle);
      errBox.appendChild(errText);
      card.appendChild(errBox);
    }

    // 底部操作行
    const actionsRow = SafeDOM.el('div', { className: 'job-actions-row' });
    if (safeImages.length > 0) {
      const referenceBtn = SafeDOM.el('button', {
        type: 'button',
        className: 'comic-btn comic-btn--sm comic-btn--outline',
        onClick: () => this.onSetReference?.(safeImages[0])
      });
      const imageIcon = SafeDOM.el('span', { className: 'btn-icon' });
      SafeDOM.setSvgIcon(imageIcon, 'image');
      referenceBtn.appendChild(imageIcon);
      referenceBtn.appendChild(SafeDOM.text('设为参考图'));
      actionsRow.appendChild(referenceBtn);
    }
    const reuseBtn = SafeDOM.el('button', {
      type: 'button',
      className: 'comic-btn comic-btn--sm comic-btn--outline',
      onClick: () => {
        if (typeof this.onRefillRequest === 'function') {
          this.onRefillRequest(record.prompt, record.params);
        }
      }
    });
    const sparkleIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(sparkleIcon, 'sparkle');
    reuseBtn.appendChild(sparkleIcon);
    reuseBtn.appendChild(SafeDOM.text('再次生成'));
    actionsRow.appendChild(reuseBtn);
    card.appendChild(actionsRow);

    return card;
  }

  createBatchParentCard(parent, children) {
    const isHighlight = parent.job_id === this.store.highlightJobId;
    const card = SafeDOM.el('div', {
      className: `job-card ${isHighlight ? 'job-card--highlight' : ''}`,
      id: `job-${parent.job_id}`,
      dataset: {
        status: parent.status,
        createdAt: parent.created_at || ''
      }
    });

    // 头部
    const header = SafeDOM.el('div', { className: 'job-header' });
    const headerLeft = SafeDOM.el('div', { className: 'job-header-left' });

    const batchTag = SafeDOM.el('span', { className: 'job-source-tag' }, ['批量聚合任务']);
    const idSpan = SafeDOM.el('button', {
      type: 'button',
      className: 'job-id-text',
      'aria-label': '复制 Job ID',
      onClick: () => {
        void Clipboard.copy(parent.job_id, `已复制 Job ID: ${parent.job_id}`);
      }
    }, [parent.job_id]);

    headerLeft.appendChild(batchTag);
    headerLeft.appendChild(idSpan);

    const headerRight = SafeDOM.el('div', { className: 'job-header-right' });
    const statusPill = this.createStatusPill(parent.status);

    let durationText = '00:00';
    if (parent.status === 'running') {
      const createdAt = new Date(parent.created_at || '').getTime();
      const diff = Number.isFinite(createdAt)
        ? Math.max(0, Math.floor((Date.now() - createdAt) / 1000))
        : 0;
      const mins = String(Math.floor(diff / 60)).padStart(2, '0');
      const secs = String(diff % 60).padStart(2, '0');
      durationText = `${mins}:${secs}`;
    } else if (parent.duration_ms) {
      durationText = `${(parent.duration_ms / 1000).toFixed(1)}s`;
    }
    const durationSpan = SafeDOM.el('span', { className: 'job-duration' }, [durationText]);

    headerRight.appendChild(statusPill);
    headerRight.appendChild(durationSpan);

    header.appendChild(headerLeft);
    header.appendChild(headerRight);
    card.appendChild(header);

    // 进度条统计
    const totalRequested = children.reduce((acc, c) => acc + (c.requested_images || 1), 0);
    const totalGenerated = children.reduce((acc, c) => acc + (c.generated_images || 0), 0);
    const percent = totalRequested > 0 ? Math.min(100, Math.round((totalGenerated / totalRequested) * 100)) : 0;

    const progressInfoRow = SafeDOM.el('div', {
      style: { display: 'flex', justifyContent: 'space-between', fontSize: '12px', fontWeight: 'bold' }
    }, [
      SafeDOM.el('span', {}, [`子任务项: ${children.length} 个`]),
      SafeDOM.el('span', {}, [`完成进度: ${totalGenerated} / ${totalRequested} 张 (${percent}%)`])
    ]);
    card.appendChild(progressInfoRow);

    const progressWrap = SafeDOM.el('div', { className: 'comic-progress-bar-wrap' });
    const progressFill = SafeDOM.el('div', {
      className: 'comic-progress-bar-fill',
      style: { width: `${percent}%` }
    });
    progressWrap.appendChild(progressFill);
    card.appendChild(progressWrap);

    // 子任务折叠抽屉
    const drawer = SafeDOM.el('div', { className: 'job-children-drawer' });
    const childrenListId = `children-${parent.job_id}`;
    const toggleHeader = SafeDOM.el('button', {
      type: 'button',
      className: 'job-children-toggle',
      'aria-expanded': 'false',
      'aria-controls': childrenListId
    });
    const toggleTitle = SafeDOM.el('span', {}, [`查看 ${children.length} 个子任务详情`]);
    const toggleIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(toggleIcon, 'chevronDown');
    toggleHeader.appendChild(toggleTitle);
    toggleHeader.appendChild(toggleIcon);

    const childrenList = SafeDOM.el('div', {
      id: childrenListId,
      className: 'job-children-list',
      style: { display: 'none' }
    });

    let isOpen = false;
    toggleHeader.addEventListener('click', () => {
      isOpen = !isOpen;
      childrenList.style.display = isOpen ? 'flex' : 'none';
      toggleHeader.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      SafeDOM.setSvgIcon(toggleIcon, isOpen ? 'chevronUp' : 'chevronDown');
    });

    for (const child of children) {
      const childItem = SafeDOM.el('div', { className: 'job-child-item' });
      const cHeader = SafeDOM.el('div', { className: 'job-child-header' });
      const cName = SafeDOM.el('strong', {}, [child.item_name || child.job_id]);
      const cPill = this.createStatusPill(child.status);
      cHeader.appendChild(cName);
      cHeader.appendChild(cPill);
      childItem.appendChild(cHeader);

      if (child.prompt) {
        childItem.appendChild(SafeDOM.el('div', { className: 'job-prompt-box' }, [child.prompt]));
      }

      const safeChildImages = Array.isArray(child.images)
        ? child.images.filter((name) => SafeDOM.isSafeImageName(name))
        : [];
      if (safeChildImages.length > 0) {
        const cThumbs = SafeDOM.el('div', { className: 'job-thumbs-grid' });
        safeChildImages.forEach((imgName, idx) => {
          const thumb = SafeDOM.el('button', {
            type: 'button',
            className: 'job-thumb-item',
            'aria-label': `查看子任务结果 ${idx + 1}`,
            onClick: () => {
              const previewItems = safeChildImages.map((name) => ({
                imageName: name,
                prompt: child.prompt,
                duration_ms: child.duration_ms,
                source: child.source,
                user_name: child.requester?.user_name
              }));
              Lightbox.open(previewItems, idx);
            }
          });
          const image = SafeDOM.el('img', {
            loading: 'lazy',
            alt: '子任务结果'
          });
          installImageFallback(image, thumb, '图片加载失败或已清理');
          ImageLoader.attach(image, imgName, { thumb: true });
          thumb.appendChild(image);
          cThumbs.appendChild(thumb);
        });
        childItem.appendChild(cThumbs);
      }

      if (child.error) {
        childItem.appendChild(SafeDOM.el('div', { className: 'job-error-box' }, [child.error]));
      }
      const childActions = SafeDOM.el('div', { className: 'job-child-actions' });
      if (safeChildImages.length > 0) {
        const childReferenceBtn = SafeDOM.el('button', {
          type: 'button',
          className: 'comic-btn comic-btn--sm comic-btn--outline',
          onClick: () => this.onSetReference?.(safeChildImages[0])
        }, ['设为参考图']);
        childActions.appendChild(childReferenceBtn);
      }
      const childReuseBtn = SafeDOM.el('button', {
        type: 'button',
        className: 'comic-btn comic-btn--sm comic-btn--outline',
        onClick: () => this.onRefillRequest?.(child.prompt, child.params)
      }, ['再次生成']);
      childActions.appendChild(childReuseBtn);
      childItem.appendChild(childActions);
      childrenList.appendChild(childItem);
    }

    drawer.appendChild(toggleHeader);
    drawer.appendChild(childrenList);
    card.appendChild(drawer);

    return card;
  }

  destroy() {
    this.unsubscribeJobs?.();
    this.unsubscribeJobs = null;
    if (this.highlightTimer) {
      clearTimeout(this.highlightTimer);
      this.highlightTimer = null;
    }
  }
}

// ==========================================================================
// 12. GalleryView (历史画廊：服务端分页、多维过滤、批量删除、灯箱)
// ==========================================================================
class GalleryView {
  constructor(store, onRefillRequest, onSetReference) {
    this.store = store;
    this.onRefillRequest = onRefillRequest;
    this.onSetReference = onSetReference;

    this.searchInput = document.getElementById('gallery-search-input');
    this.sourceSelect = document.getElementById('gallery-source-select');
    this.groupInput = document.getElementById('gallery-group-input');
    this.userInput = document.getElementById('gallery-user-input');
    this.btnSearch = document.getElementById('btn-search-gallery');
    this.btnReset = document.getElementById('btn-reset-gallery');

    this.selectAll = document.getElementById('gallery-select-all');
    this.selectedCountEl = document.getElementById('selected-gallery-count');
    this.btnBatchDelete = document.getElementById('btn-batch-delete');

    this.grid = document.getElementById('gallery-grid');
    this.emptyTip = document.getElementById('empty-gallery-tip');
    this.emptyTitle = this.emptyTip.querySelector('.empty-title');
    this.emptyDescription = this.emptyTip.querySelector('.empty-desc');

    this.btnPagePrev = document.getElementById('btn-page-prev');
    this.btnPageNext = document.getElementById('btn-page-next');
    this.paginationInfo = document.getElementById('pagination-info');
    this.fetchCounter = 0;
    this.maxSelection = 20;

    this.initEvents();
  }

  initEvents() {
    this.btnSearch.addEventListener('click', () => {
      this.store.galleryQuery.keyword = this.searchInput.value.trim();
      this.store.galleryQuery.source = this.sourceSelect.value;
      this.store.galleryQuery.group_id = this.groupInput.value.trim();
      this.store.galleryQuery.user_id = this.userInput.value.trim();
      this.store.galleryQuery.page = 1;
      this.fetchGallery();
    });

    [this.searchInput, this.groupInput, this.userInput].forEach((input) => {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') this.btnSearch.click();
      });
    });

    this.btnReset.addEventListener('click', () => {
      this.searchInput.value = '';
      this.sourceSelect.value = '';
      this.groupInput.value = '';
      this.userInput.value = '';
      this.store.galleryQuery.keyword = '';
      this.store.galleryQuery.source = '';
      this.store.galleryQuery.group_id = '';
      this.store.galleryQuery.user_id = '';
      this.store.galleryQuery.page = 1;
      this.fetchGallery();
    });

    this.selectAll.addEventListener('change', () => {
      const isChecked = this.selectAll.checked;
      const items = this.store.galleryData.items || [];
      if (isChecked) {
        let reachedLimit = false;
        for (const item of items) {
          if (this.store.selectedGalleryJobs.has(item.job_id)) continue;
          if (this.store.selectedGalleryJobs.size >= this.maxSelection) {
            reachedLimit = true;
            break;
          }
          this.store.selectedGalleryJobs.add(item.job_id);
        }
        if (reachedLimit) {
          Toast.warning(`单次最多选择 ${this.maxSelection} 个历史任务`);
        }
      } else {
        items.forEach((it) => this.store.selectedGalleryJobs.delete(it.job_id));
      }
      this.updateSelectionUI();
    });

    this.btnBatchDelete.addEventListener('click', () => this.handleBatchDelete());

    this.btnPagePrev.addEventListener('click', () => {
      if (this.store.galleryQuery.page > 1) {
        this.store.galleryQuery.page--;
        this.fetchGallery();
      }
    });

    this.btnPageNext.addEventListener('click', () => {
      const maxPage = Math.ceil((this.store.galleryData.total || 0) / this.store.galleryQuery.size);
      if (this.store.galleryQuery.page < maxPage) {
        this.store.galleryQuery.page++;
        this.fetchGallery();
      }
    });
  }

  async fetchGallery() {
    const requestId = ++this.fetchCounter;
    try {
      const data = await BridgeClient.get('webui/history', { ...this.store.galleryQuery });
      if (requestId !== this.fetchCounter) return;
      const items = Array.isArray(data?.items) ? data.items : [];
      const page = Number.isInteger(data?.page) ? data.page : 1;
      const size = Number.isInteger(data?.size) && data.size > 0 ? data.size : 20;
      const total = Number.isInteger(data?.total) && data.total >= 0 ? data.total : 0;

      if (items.length === 0 && page > 1 && total > 0) {
        const lastPage = Math.max(1, Math.ceil(total / size));
        if (lastPage !== this.store.galleryQuery.page) {
          this.store.galleryQuery.page = lastPage;
          await this.fetchGallery();
          return;
        }
      }

      this.store.galleryData = {
        items,
        page,
        size,
        total
      };
      this.store.galleryQuery.page = page;
      this.render();
    } catch (err) {
      if (requestId !== this.fetchCounter) return;
      console.error('[GalleryView] 拉取画廊数据失败:', err);
      ErrorFeedback.show(err, '拉取历史画廊数据失败');
    }
  }

  updateSelectionUI() {
    const count = this.store.selectedGalleryJobs.size;
    SafeDOM.setText(this.selectedCountEl, String(count));
    this.btnBatchDelete.disabled = count === 0;

    const checkboxes = this.grid.querySelectorAll('.gallery-card-checkbox');
    checkboxes.forEach((cb) => {
      const jobId = cb.jobId;
      cb.checked = this.store.selectedGalleryJobs.has(jobId);
    });

    const items = this.store.galleryData.items || [];
    if (items.length > 0) {
      const allSelected = items.every((it) => this.store.selectedGalleryJobs.has(it.job_id));
      const someSelected = items.some((it) => this.store.selectedGalleryJobs.has(it.job_id));
      this.selectAll.checked = allSelected;
      this.selectAll.indeterminate = someSelected && !allSelected;
    } else {
      this.selectAll.checked = false;
      this.selectAll.indeterminate = false;
    }
  }

  render() {
    const { items, page, size, total } = this.store.galleryData;
    const maxPage = Math.max(1, Math.ceil(total / size));

    SafeDOM.setText(this.paginationInfo, `第 ${page} 页 / 共 ${maxPage} 页 (总数 ${total})`);
    this.btnPagePrev.disabled = page <= 1;
    this.btnPageNext.disabled = page >= maxPage;

    if (!items || items.length === 0) {
      this.grid.replaceChildren();
      const hasFilters = Object.entries(this.store.galleryQuery)
        .some(([key, value]) => !['page', 'size'].includes(key) && Boolean(value));
      SafeDOM.setText(this.emptyTitle, hasFilters ? '未找到匹配的生成任务' : '暂无历史任务记录');
      SafeDOM.setText(
        this.emptyDescription,
        hasFilters ? '请调整筛选条件或点击重置后重试。' : '已生成的图片与任务记录将归档在此处，支持管理与检索。'
      );
      this.emptyTip.style.display = 'flex';
      this.updateSelectionUI();
      return;
    }

    this.emptyTip.style.display = 'none';
    const fragment = document.createDocumentFragment();

    items.forEach((item) => {
      fragment.appendChild(this.createCard(item));
    });

    this.grid.replaceChildren(fragment);
    this.updateSelectionUI();
  }

  createCard(item) {
    const card = SafeDOM.el('div', { className: 'gallery-card' });

    // 左上角勾选框
    const selectWrap = SafeDOM.el('div', { className: 'gallery-card-select-wrap' });
    const checkbox = SafeDOM.el('input', {
      type: 'checkbox',
      className: 'comic-checkbox gallery-card-checkbox',
      'aria-label': '选择历史任务',
      onChange: (e) => {
        if (e.target.checked) {
          if (this.store.selectedGalleryJobs.size >= this.maxSelection
              && !this.store.selectedGalleryJobs.has(item.job_id)) {
            e.target.checked = false;
            Toast.warning(`单次最多选择 ${this.maxSelection} 个历史任务`);
          } else {
            this.store.selectedGalleryJobs.add(item.job_id);
          }
        } else {
          this.store.selectedGalleryJobs.delete(item.job_id);
        }
        this.updateSelectionUI();
      }
    });
    checkbox.jobId = item.job_id;
    selectWrap.appendChild(checkbox);
    card.appendChild(selectWrap);

    // 封面图容器
    const safeImages = Array.isArray(item.images)
      ? item.images.filter((name) => SafeDOM.isSafeImageName(name))
      : [];
    const firstImgName = safeImages[0] || null;
    const imgWrap = firstImgName
      ? SafeDOM.el('button', {
          type: 'button',
          className: 'gallery-card-img-wrap',
          'aria-label': '查看大图'
        })
      : SafeDOM.el('div', { className: 'gallery-card-img-wrap' });

    if (firstImgName) {
      const img = SafeDOM.el('img', {
        loading: 'lazy',
        alt: '画廊图片'
      });
      installImageFallback(img, imgWrap, '图片加载失败或已清理');
      ImageLoader.attach(img, firstImgName, { thumb: true });

      imgWrap.addEventListener('click', () => {
        const previewItems = safeImages.map((name) => ({
          imageName: name,
          prompt: item.prompt,
          provider: item.params?.provider,
          model: item.params?.model,
          resolution: item.params?.resolution,
          aspect_ratio: item.params?.aspect_ratio,
          duration_ms: item.duration_ms,
          source: item.source,
          user_name: item.requester?.user_name
        }));
        Lightbox.open(previewItems, 0);
      });

      imgWrap.appendChild(img);
    } else {
      const placeholder = SafeDOM.el('div', { className: 'comic-img-placeholder' });
      const label = SafeDOM.el('span', {}, ['暂无生成图片']);
      placeholder.appendChild(label);
      imgWrap.appendChild(placeholder);
    }
    card.appendChild(imgWrap);

    // 信息区
    const info = SafeDOM.el('div', { className: 'gallery-card-info' });
    const promptEl = SafeDOM.el('div', { className: 'gallery-card-prompt' }, [item.prompt || '（历史图片，无提示词记录）']);

    const meta = SafeDOM.el('div', { className: 'gallery-card-meta' });
    const sourceEl = SafeDOM.el('span', {}, [`[${SafeDOM.sourceLabel(item.source)}]`]);
    const timeEl = SafeDOM.el('span', {}, [
      item.created_at ? new Date(item.created_at).toLocaleDateString('zh-CN') : ''
    ]);
    meta.appendChild(sourceEl);
    meta.appendChild(timeEl);

    info.appendChild(promptEl);
    info.appendChild(meta);
    card.appendChild(info);

    // 悬停操作工具栏
    const actions = SafeDOM.el('div', { className: 'gallery-card-actions' });

    // 1. 下载原图
    if (firstImgName) {
      const btnDownload = SafeDOM.el('button', {
        type: 'button',
        className: 'comic-btn comic-btn--outline',
        title: '下载原图',
        'aria-label': '下载原图',
        onClick: async (e) => {
          e.stopPropagation();
          try {
            await BridgeClient.download(`webui/image/${encodeURIComponent(firstImgName)}`, { download: '1' }, firstImgName);
            Toast.success('已发起图片下载');
          } catch (err) {
            ErrorFeedback.show(err, '下载失败');
          }
        }
      });
      const dlIcon = SafeDOM.el('span', { className: 'btn-icon' });
      SafeDOM.setSvgIcon(dlIcon, 'download');
      btnDownload.appendChild(dlIcon);
      actions.appendChild(btnDownload);
    }

    // 2. 复制提示词
    const btnCopy = SafeDOM.el('button', {
      type: 'button',
      className: 'comic-btn comic-btn--outline',
      title: '复制提示词',
      'aria-label': '复制提示词',
      onClick: async (e) => {
        e.stopPropagation();
        await Clipboard.copy(item.prompt || '', '提示词已复制到剪贴板');
      }
    });
    const cpIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(cpIcon, 'copy');
    btnCopy.appendChild(cpIcon);
    actions.appendChild(btnCopy);

    // 3. 再次生成 (回填工作台；历史图片无提示词记录时禁用)
    const btnReuse = SafeDOM.el('button', {
      type: 'button',
      className: 'comic-btn comic-btn--outline',
      title: item.prompt ? '回填参数再次生成' : '历史图片无提示词记录，无法回填',
      'aria-label': '再次生成',
      disabled: !item.prompt,
      onClick: (e) => {
        e.stopPropagation();
        if (typeof this.onRefillRequest === 'function') {
          this.onRefillRequest(item.prompt, item.params);
        }
      }
    });
    const sparkleIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(sparkleIcon, 'sparkle');
    btnReuse.appendChild(sparkleIcon);
    actions.appendChild(btnReuse);

    // 4. 用作参考图
    if (firstImgName) {
      const btnRef = SafeDOM.el('button', {
        type: 'button',
        className: 'comic-btn comic-btn--outline',
        title: '设为工作台参考图',
        'aria-label': '设为工作台参考图',
        onClick: (e) => {
          e.stopPropagation();
          if (typeof this.onSetReference === 'function') {
            this.onSetReference(firstImgName);
          }
        }
      });
      const imgIcon = SafeDOM.el('span', { className: 'btn-icon' });
      SafeDOM.setSvgIcon(imgIcon, 'image');
      btnRef.appendChild(imgIcon);
      actions.appendChild(btnRef);
    }

    // 5. 单项删除
    const btnDel = SafeDOM.el('button', {
      type: 'button',
      className: 'comic-btn comic-btn--danger',
      title: '删除记录与图片',
      'aria-label': '删除记录与图片',
      onClick: async (e) => {
        e.stopPropagation();
        const ok = await Modal.confirm({
          title: '确认删除该任务记录？',
          content: `删除后将彻底清理服务器磁盘中的原始图片（Job: ${item.job_id}），不可恢复。`,
          confirmText: '确认删除',
          danger: true
        });
        if (ok) {
          await this.deleteJobs([item.job_id]);
        }
      }
    });
    const trashIcon = SafeDOM.el('span', { className: 'btn-icon' });
    SafeDOM.setSvgIcon(trashIcon, 'trash');
    btnDel.appendChild(trashIcon);
    actions.appendChild(btnDel);

    card.appendChild(actions);
    return card;
  }

  async handleBatchDelete() {
    const count = this.store.selectedGalleryJobs.size;
    if (count === 0) return;

    const ok = await Modal.confirm({
      title: '批量删除确认',
      content: `您已选中 ${count} 个历史任务，删除将彻底清理服务器磁盘中的原始图片文件，且不可恢复。确认继续？`,
      confirmText: '确认删除',
      danger: true
    });

    if (ok) {
      const jobIds = Array.from(this.store.selectedGalleryJobs);
      await this.deleteJobs(jobIds);
    }
  }

  async deleteJobs(jobIds) {
    const normalizedJobIds = Array.from(new Set(
      (Array.isArray(jobIds) ? jobIds : []).filter((jobId) => typeof jobId === 'string' && jobId.trim())
    ));
    if (normalizedJobIds.length === 0) return;
    if (normalizedJobIds.length > this.maxSelection) {
      Toast.error(`单次最多删除 ${this.maxSelection} 个历史任务`);
      return;
    }
    try {
      const res = await BridgeClient.post('webui/history/delete', { job_ids: normalizedJobIds });
      const deleted = Array.isArray(res?.deleted) ? res.deleted : [];
      const failed = Array.isArray(res?.failed) ? res.failed : [];

      if (deleted.length > 0) {
        Toast.success(`成功删除 ${deleted.length} 个历史任务`);
      }
      if (failed.length > 0) {
        const details = failed.map((entry) => `${entry?.job_id || '未知任务'}(${entry?.error || '未知错误'})`);
        Toast.warning(`部分删除失败: ${details.join(', ')}`);
      }

      for (const id of deleted) {
        this.store.selectedGalleryJobs.delete(id);
      }
      this.store.removeJobs(deleted);
      await this.fetchGallery();
    } catch (err) {
      console.error('[GalleryView] 删除任务失败:', err);
      ErrorFeedback.show(err, '删除失败');
    }
  }

  destroy() {
    this.fetchCounter++;
  }
}

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
    this.handleUnload = () => {
      void this.destroy();
    };

    this.tabBtns = {
      workbench: document.getElementById('tab-btn-workbench'),
      progress: document.getElementById('tab-btn-progress'),
      gallery: document.getElementById('tab-btn-gallery')
    };

    this.panels = {
      workbench: document.getElementById('panel-workbench'),
      progress: document.getElementById('panel-progress'),
      gallery: document.getElementById('panel-gallery')
    };
  }

  async init() {
    // 1. 初始化静态图标字典
    SafeDOM.renderStaticIcons(document);

    // 2. 初始化全局模态框与灯箱
    Toast.init();
    Modal.init();
    Lightbox.init();

    // 3. 页面主题同步
    this.initThemeSync();

    // 4. 初始化子视图
    this.workbench = new WorkbenchView(this.store, (newJobId) => {
      this.switchTab('progress');
      if (newJobId) {
        void this.sse.afterJobSubmitted();
      }
    });

    this.progress = new ProgressView(
      this.store,
      (prompt, params) => {
        this.switchTab('workbench');
        this.workbench.refillParameters(prompt, params);
      },
      (imageName) => {
        const added = this.workbench.addGalleryReference(imageName);
        if (added) this.switchTab('workbench');
      }
    );

    this.gallery = new GalleryView(
      this.store,
      (prompt, params) => {
        this.switchTab('workbench');
        this.workbench.refillParameters(prompt, params);
      },
      (imageName) => {
        const added = this.workbench.addGalleryReference(imageName);
        if (added) {
          this.switchTab('workbench');
        }
      }
    );

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
      this.workbench.showGlobalBanner('未检测到 AstrBot 页面桥接，请在 Dashboard 插件页中打开 Studio');
      this.workbench.setFormDisabled(true);
      this.sse.updateStatusIndicator('error', '页面桥接不可用');
      return;
    }

    await this.workbench.loadCapabilities();
    await this.sse.start();

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
