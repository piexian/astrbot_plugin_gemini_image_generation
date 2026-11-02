import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import TemplatesView from "../TemplatesView.vue";
import type { HubState, StudioTemplate } from "../types";

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{v(\d+)\}/g, (_, i: string) => String(params?.[`v${i}`] ?? ""));

function template(id: string, overrides: Partial<StudioTemplate> = {}): StudioTemplate {
  return { id, name: `模板${id}`, content: `内容${id}`, tags: [], ...overrides };
}

function makeBridge(routes: Record<string, unknown>) {
  return {
    get: vi.fn((path: string) => Promise.resolve(routes[path])),
    post: vi.fn((path: string) => Promise.resolve(routes[path])),
  };
}

const modalBody = document.createElement("div");
const modalFooter = document.createElement("div");
const clipboard = { copy: vi.fn(async () => {}) };
const modal = {
  confirm: vi.fn(async () => true),
  openCustom: vi.fn((options: Record<string, unknown>) => {
    modal.options = options;
    (options.renderBody as ((b: HTMLElement) => void) | undefined)?.(modalBody);
    (
      options.renderFooter as ((f: HTMLElement, c: () => void, x: () => void) => void) | undefined
    )?.(
      modalFooter,
      () => (modal as unknown as { close: (accepted?: boolean) => void }).close(true),
      () => (modal as unknown as { close: (accepted?: boolean) => void }).close(false),
    );
  }),
  close: function (accepted?: boolean) {
    const current = modal.options;
    modal.options = null;
    modalBody.replaceChildren();
    modalFooter.replaceChildren();
    (current?.onClose as (() => void) | undefined)?.();
    void accepted;
  },
  options: null as Record<string, unknown> | null,
};

function mountView(bridge: ReturnType<typeof makeBridge>) {
  const onUsed = vi.fn();
  const wrapper = mount(TemplatesView, {
    props: { bridge, modal, t, clipboard, onUsed, onSaved: async () => {} },
    attachTo: document.body,
  });
  return { wrapper, onUsed };
}

beforeEach(() => {
  document.body.innerHTML = "";
  clipboard.copy.mockClear();
  modalBody.replaceChildren();
  modalFooter.replaceChildren();
  // 渲染目标挂到 body，querySelector 才能命中
  document.body.appendChild(modalBody);
  document.body.appendChild(modalFooter);
  modal.options = null;
});

describe("TemplatesView", () => {
  it("首帧未加载时显示占位提示，加载后渲染本地卡片并拉取远程缓存", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": {
        templates: [{ ...template("b"), found_via: "cache" }],
        notice: "",
        state: { hash: "abc", count: 1 } as HubState,
      },
      "webui/templates/hub/tags": { tags: [] },
    });
    const { wrapper } = mountView(bridge);
    expect(wrapper.text()).toContain("打开提示词广场后加载。");

    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    expect(bridge.get).toHaveBeenCalledWith("webui/templates");
    expect(wrapper.text()).toContain("模板a");
    // 合并网格：远程缓存卡片也出现在本地页
    expect(wrapper.text()).toContain("模板b");
    expect(wrapper.text()).toContain("缓存");
    expect(wrapper.text()).toContain("已加载 1 个模板。");
  });

  it("未绑密钥点击我的模板时不发请求，直接弹密钥弹窗", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [] },
      "webui/templates/hub": {
        templates: [],
        notice: "",
        state: { hash: "abc", count: 0, has_key: false } as HubState,
      },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": { configured: false, masked: "" },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const mineGetCalls = () =>
      bridge.get.mock.calls.filter(([path]) => path === "webui/templates/hub/mine").length;
    const tabButtons = wrapper.findAll(".tpl-tabs button");
    await tabButtons[1]!.trigger("click");
    await flushPromises();

    expect(mineGetCalls()).toBe(0);
    expect(modal.openCustom).toHaveBeenCalledWith(
      expect.objectContaining({ title: "在线模板密钥" }),
    );
    expect(wrapper.text()).toContain("请先填写在线模板密钥。");
  });

  it("我的模板页渲染修改删除按钮，本地卡不出现", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/mine": {
        templates: [{ ...template("m"), remote_id: "r1" }],
        state: null,
      },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();
    const modalCallsBeforeMine = modal.openCustom.mock.calls.length;

    await wrapper.findAll(".tpl-tabs button")[1]!.trigger("click");
    await flushPromises();

    expect(bridge.get).toHaveBeenCalledWith("webui/templates/hub/mine", { limit: "50" });
    expect(modal.openCustom.mock.calls.length).toBe(modalCallsBeforeMine);
    expect(wrapper.text()).toContain("模板m");
    const mineCard = wrapper.findAll(".tpl-card").find((c) => c.text().includes("模板m"));
    expect(mineCard?.text()).toContain("修改");
    expect(mineCard?.text()).toContain("删除");
    expect(mineCard?.text()).not.toContain("举报");
  });

  it("上游拒绝密钥时显示错误且不重复弹窗", async () => {
    const routes: Record<string, unknown> = {
      "webui/templates": { templates: [] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
    };
    const bridge = makeBridge(routes);
    bridge.get.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/mine") throw new Error("forbidden");
      return routes[path];
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const modalCallsBeforeMine = modal.openCustom.mock.calls.length;
    await wrapper.findAll(".tpl-tabs button")[1]!.trigger("click");
    await flushPromises();

    expect(modal.openCustom.mock.calls.length).toBe(modalCallsBeforeMine);
    expect(wrapper.text()).toContain("读取我的模板失败，请检查密钥。");
    wrapper.unmount();
  });

  it("填入工作台把模板正文交给 onUsed", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: null },
      "webui/templates/hub/tags": { tags: [] },
    });
    const { wrapper, onUsed } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    await wrapper.find(".tpl-card .comic-btn--cta").trigger("click");
    expect(onUsed).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "内容a", template: expect.objectContaining({ id: "a" }) }),
    );
  });
});

describe("TemplatesView 密钥与导出弹窗", () => {
  const q = (selector: string) => document.querySelector(selector) as HTMLElement | null;

  it("已配置密钥时回填密码输入框并保留掩码占位", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": {
        configured: true,
        masked: "iph_***n2ta",
        key: "iph_abcdefghijklmnop",
      },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    // 打开密钥弹窗（工具栏「密钥」按钮）
    const keyButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "密钥");
    await keyButton!.trigger("click");
    await flushPromises();

    const keyInput = q('[data-tpl-key="key"]') as HTMLInputElement;
    expect(keyInput).toBeTruthy();
    expect(keyInput.value).toBe("iph_abcdefghijklmnop");
    expect(keyInput.placeholder).toContain("iph_***n2ta");
    expect(keyInput.placeholder).toContain("清空保存可移除");
    wrapper.unmount();
  });

  it("关闭密钥弹窗后迟到的掩码响应不再触碰 DOM", async () => {
    let resolveKey: (value: unknown) => void = () => {};
    const deferred = new Promise((resolve) => {
      resolveKey = resolve;
    });
    const bridge = makeBridge({
      "webui/templates": { templates: [] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: false } as HubState },
      "webui/templates/hub/tags": { tags: [] },
    });
    // 仅密钥路由挂起：模拟用户在响应返回前就关闭弹窗
    bridge.get.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/key") return deferred;
      const routes: Record<string, unknown> = {
        "webui/templates": { templates: [] },
        "webui/templates/hub": { templates: [], notice: "", state: { has_key: false } },
        "webui/templates/hub/tags": { tags: [] },
      };
      return routes[path] ?? {};
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();
    await wrapper.findAll(".tpl-tabs button")[1]!.trigger("click");
    await flushPromises();

    // 无密钥直接弹窗；用户立刻关闭
    expect(q('[data-tpl-key="key"]')).toBeTruthy();
    (modal as unknown as { close: (accepted?: boolean) => void }).close(false);
    resolveKey({ configured: true, masked: "iph_late" });
    await flushPromises();
    expect(modal.options).toBeNull();
    expect(q('[data-tpl-key="key"]')).toBeNull();
    wrapper.unmount();
  });

  it("已回填密钥未修改时提示未变化且不发 POST", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": {
        configured: true,
        masked: "iph_***n2ta",
        key: "iph_abcdefghijklmnop",
      },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const keyButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "密钥");
    await keyButton!.trigger("click");
    await flushPromises();

    const keyInput = q('[data-tpl-key="key"]') as HTMLInputElement;
    expect(keyInput.value).toBe("iph_abcdefghijklmnop");

    const saveButton = Array.from(modalBody.querySelectorAll("button")).find(
      (b) => b.textContent === "保存密钥",
    )!;
    (saveButton as HTMLButtonElement).click();
    await flushPromises();

    expect(bridge.post).not.toHaveBeenCalledWith("webui/templates/hub/key", expect.anything());
    expect(modalBody.textContent).toContain("密钥未变化。");
    wrapper.unmount();
  });

  it("已回填密钥被清空后保存会发送空 key", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": {
        configured: true,
        masked: "iph_***n2ta",
        key: "iph_abcdefghijklmnop",
      },
    });
    bridge.post.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/key") return { masked: "" };
      return {};
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const keyButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "密钥");
    await keyButton!.trigger("click");
    await flushPromises();

    const keyInput = q('[data-tpl-key="key"]') as HTMLInputElement;
    keyInput.value = "";
    keyInput.dispatchEvent(new Event("input", { bubbles: true }));

    const saveButton = Array.from(modalBody.querySelectorAll("button")).find(
      (b) => b.textContent === "保存密钥",
    )!;
    (saveButton as HTMLButtonElement).click();
    await flushPromises();

    const saveCall = bridge.post.mock.calls.find(
      ([path]) => path === "webui/templates/hub/key",
    ) as unknown as [string, Record<string, unknown>] | undefined;
    expect(saveCall?.[1]).toEqual({ action: "save", key: "" });
    wrapper.unmount();
  });

  it("密钥响应迟到时不覆盖用户已输入内容", async () => {
    let resolveKey: (value: unknown) => void = () => {};
    const deferred = new Promise((resolve) => {
      resolveKey = resolve;
    });
    const bridge = makeBridge({
      "webui/templates": { templates: [] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
    });
    bridge.get.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/key") return deferred;
      const routes: Record<string, unknown> = {
        "webui/templates": { templates: [] },
        "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } },
        "webui/templates/hub/tags": { tags: [] },
      };
      return routes[path] ?? {};
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const keyButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "密钥");
    await keyButton!.trigger("click");
    await flushPromises();

    const keyInput = q('[data-tpl-key="key"]') as HTMLInputElement;
    keyInput.value = "iph_user_typed";
    keyInput.dispatchEvent(new Event("input", { bubbles: true }));

    resolveKey({ configured: true, masked: "iph_***late", key: "iph_server_value" });
    await flushPromises();

    expect(keyInput.value).toBe("iph_user_typed");
    expect(keyInput.placeholder).toContain("iph_***late");
    wrapper.unmount();
  });

  it("补填密钥后加载成功会清掉旧的未配置提示", async () => {
    const routes: Record<string, unknown> = {
      "webui/templates": { templates: [] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: false } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": { configured: false, masked: "" },
    };
    const bridge = makeBridge(routes);
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    await wrapper.findAll(".tpl-tabs button")[1]!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("请先填写在线模板密钥。");
    (modal as unknown as { close: (accepted?: boolean) => void }).close(false);

    // 用户补填密钥后再次打开我的模板：状态与列表都成功。
    routes["webui/templates/hub"] = {
      templates: [],
      notice: "",
      state: { has_key: true } as HubState,
    };
    routes["webui/templates/hub/mine"] = { templates: [{ ...template("m1") }], state: null };
    await (wrapper.vm as unknown as { openMine: () => Promise<void> }).openMine();
    await flushPromises();

    expect(wrapper.text()).not.toContain("请先填写在线模板密钥。");
    expect(wrapper.text()).toContain("模板m1");
    wrapper.unmount();
  });

  it("我的模板加载期间重复点击不重入，失败只渲染一处错误", async () => {
    const routes: Record<string, unknown> = {
      "webui/templates": { templates: [] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
    };
    let releaseMine: (() => void) | null = null;
    const bridge = makeBridge(routes);
    bridge.get.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/mine") {
        await new Promise<void>((resolve) => {
          releaseMine = resolve;
        });
        throw new Error("forbidden");
      }
      return routes[path] ?? {};
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const openMine = (wrapper.vm as unknown as { openMine: () => Promise<void> }).openMine;
    const first = openMine();
    await flushPromises();
    await openMine();
    await flushPromises();

    const mineCalls = () =>
      bridge.get.mock.calls.filter(([path]) => path === "webui/templates/hub/mine").length;
    expect(mineCalls()).toBe(1);

    releaseMine!();
    await first;
    await flushPromises();

    const text = wrapper.text();
    expect(text.split("读取我的模板失败，请检查密钥。").length - 1).toBe(1);
    wrapper.unmount();
  });

  it("导出弹窗带复制与关闭按钮", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: null },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/export": { templates: [] },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const exportButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "导出");
    await exportButton!.trigger("click");
    await flushPromises();

    const footerText = modalFooter.textContent || "";
    expect(footerText).toContain("复制");
    expect(footerText).toContain("关闭");
    const textarea = q(".tpl-package-input") as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    expect(textarea.readOnly).toBe(true);
    wrapper.unmount();
  });
});

describe("TemplatesView 我的模板编辑与复制", () => {
  const q = (selector: string) => document.querySelector(selector) as HTMLElement | null;

  function mineBridge() {
    return makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: true } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/mine": {
        templates: [
          {
            ...template("m1"),
            remote_id: "r1",
            name: "旧名",
            ai_description: "旧说明",
            content: "旧正文",
            tags: ["t1"],
            reference: "optional",
          },
        ],
        state: null,
      },
    });
  }

  it("修改打开分块表单，保存提交各字段", async () => {
    const bridge = mineBridge();
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();
    await wrapper.findAll(".tpl-tabs button")[1]!.trigger("click");
    await flushPromises();

    const mineCard = wrapper.findAll(".tpl-card").find((c) => c.text().includes("旧名"))!;
    const editButton = Array.from(mineCard.findAll("button")).find((b) => b.text() === "修改")!;
    await editButton.trigger("click");
    await flushPromises();

    // 分块表单：名称/说明/正文/参考图/标签各自独立字段
    for (const name of ["name", "ai_description", "content", "reference", "tags"]) {
      expect(q(`[data-tpl-editor="${name}"]`)).toBeTruthy();
    }
    const nameInput = q('[data-tpl-editor="name"]') as HTMLInputElement;
    expect(nameInput.value).toBe("旧名");

    nameInput.value = "新名";
    nameInput.dispatchEvent(new Event("input", { bubbles: true }));
    const save = Array.from(modalFooter.querySelectorAll("button")).find(
      (b) => b.textContent === "保存修改",
    )!;
    (save as HTMLButtonElement).click();
    await flushPromises();

    const updateCall = bridge.post.mock.calls.find(
      ([path]) => path === "webui/templates/hub",
    ) as unknown as [string, Record<string, unknown>] | undefined;
    expect(updateCall).toBeTruthy();
    const changes = (updateCall![1] as Record<string, unknown>).changes as Record<string, unknown>;
    expect(changes.name).toBe("新名");
    expect(changes.content).toBe("旧正文");
    expect(changes.tags).toEqual(["t1"]);
    // 保存后回到我的模板列表
    expect(
      bridge.get.mock.calls.filter(([path]) => path === "webui/templates/hub/mine").length,
    ).toBeGreaterThan(1);
    wrapper.unmount();
  });

  it("保存密钥后先关弹窗再刷新背景", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: { has_key: false } as HubState },
      "webui/templates/hub/tags": { tags: [] },
      "webui/templates/hub/key": { configured: false, masked: "" },
    });
    // 保存密钥的 POST 同路由，返回成功掩码
    bridge.post.mockImplementation(async (path: string) => {
      if (path === "webui/templates/hub/key") return { masked: "iph_saved" };
      return {};
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const keyButton = Array.from(wrapper.findAll("button")).find((b) => b.text() === "密钥")!;
    await keyButton.trigger("click");
    await flushPromises();
    const keyInput = q('[data-tpl-key="key"]') as HTMLInputElement;
    keyInput.value = "iph_newkey";
    keyInput.dispatchEvent(new Event("input", { bubbles: true }));

    const saveButton = Array.from(modalBody.querySelectorAll("button")).find(
      (b) => b.textContent === "保存密钥",
    )!;
    (saveButton as HTMLButtonElement).click();
    await flushPromises();

    // 弹窗已关闭，且背景刷新（hub state）发生
    expect(modal.options).toBeNull();
    expect(
      bridge.get.mock.calls.filter(([path]) => path === "webui/templates/hub").length,
    ).toBeGreaterThan(1);
    wrapper.unmount();
  });

  it("本地卡片复制=提示词正文进剪贴板", async () => {
    const bridge = makeBridge({
      "webui/templates": { templates: [template("a")] },
      "webui/templates/hub": { templates: [], notice: "", state: null },
      "webui/templates/hub/tags": { tags: [] },
    });
    const { wrapper } = mountView(bridge);
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce();
    await flushPromises();

    const card = wrapper.findAll(".tpl-card").find((c) => c.text().includes("模板a"))!;
    const copyButton = Array.from(card.findAll("button")).find((b) => b.text() === "复制")!;
    await copyButton.trigger("click");
    expect(clipboard.copy).toHaveBeenCalledWith("内容a", "已复制到剪贴板");
    wrapper.unmount();
  });
});
