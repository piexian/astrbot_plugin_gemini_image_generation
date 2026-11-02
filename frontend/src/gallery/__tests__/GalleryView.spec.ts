import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import GalleryView from "../GalleryView.vue";

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`));

const item = (jobId: string, extra: Record<string, unknown> = {}) => ({
  job_id: jobId,
  prompt: `提示词-${jobId}`,
  images: [`img-${jobId}.png`],
  source: "webui",
  params: {},
  stats: {},
  created_at: "2026-11-02T10:00:00Z",
  ...extra,
});

let historyPayload: Record<string, unknown> = {};
const calls: { method: string; route: string; body?: unknown; params?: unknown }[] = [];
const store = { removeJobs: vi.fn() };
const toast = { success: vi.fn(), warning: vi.fn(), error: vi.fn() };
const errorFeedback = { show: vi.fn() };
const clipboard = { copy: vi.fn() };
const imageLoader = { attach: vi.fn() };
const lightbox = { open: vi.fn() };
const installImageFallback = vi.fn();
const safeDom = {
  isSafeImageName: (name: unknown) => typeof name === "string" && /\.(png|jpg)$/.test(name),
  sourceLabel: (source?: string) => source || "未知",
  displayRoute: () => ({ provider: "prov", model: "model-x" }),
  requesterLabels: () => [],
  referenceStrip: () => null,
};
const onRefillRequest = vi.fn();
const onSetReference = vi.fn();
const modal = {
  confirm: vi.fn(async () => true),
  openCustom: vi.fn(),
};
const bridge = {
  isAvailable: () => true,
  get: vi.fn(async (route: string, params?: unknown) => {
    calls.push({ method: "get", route, params });
    if (route === "webui/history") return historyPayload;
    return {};
  }),
  post: vi.fn(async (route: string, body?: unknown) => {
    calls.push({ method: "post", route, body });
    const ids = ((body as Record<string, unknown>)?.job_ids ?? []) as string[];
    return { deleted: ids, failed: [] };
  }),
  download: vi.fn(async () => {}),
};

function mountView() {
  return mount(GalleryView, {
    props: {
      store,
      bridge,
      modal,
      t,
      safeDom,
      icons: { search: "", trash: "", download: "", copy: "", sparkle: "", image: "", inbox: "" },
      toast,
      errorFeedback,
      clipboard,
      imageLoader,
      lightbox,
      installImageFallback,
      onRefillRequest,
      onSetReference,
    },
    attachTo: document.body,
  });
}

const q = (selector: string) => document.querySelector(selector) as HTMLElement | null;
const qa = (selector: string) => Array.from(document.querySelectorAll(selector));

beforeEach(() => {
  document.body.innerHTML = "";
  calls.length = 0;
  historyPayload = { items: [item("a"), item("b")], page: 1, size: 20, total: 2 };
  store.removeJobs.mockClear();
  toast.success.mockClear();
  toast.warning.mockClear();
  toast.error.mockClear();
  errorFeedback.show.mockClear();
  clipboard.copy.mockClear();
  imageLoader.attach.mockClear();
  lightbox.open.mockClear();
  installImageFallback.mockClear();
  onRefillRequest.mockClear();
  onSetReference.mockClear();
  modal.confirm.mockClear();
  bridge.get.mockClear();
  bridge.post.mockClear();
  bridge.download.mockClear();
});

describe("GalleryView", () => {
  it("拉取历史并渲染卡片，缩略图与灯箱接线", async () => {
    const wrapper = mountView();
    await (wrapper.vm as unknown as { fetchGallery: () => Promise<void> }).fetchGallery();
    await flushPromises();

    expect(qa(".gallery-card").length).toBe(2);
    expect(document.body.textContent).toContain("提示词-a");
    expect(imageLoader.attach).toHaveBeenCalledTimes(2);
    expect(installImageFallback).toHaveBeenCalledTimes(2);
    expect(q("[data-gallery-thumb]")).toBeTruthy();

    const thumb = q("[data-gallery-thumb]") as HTMLElement;
    thumb.parentElement!.click();
    expect(lightbox.open).toHaveBeenCalledTimes(1);
    expect((lightbox.open as any).mock.calls[0][1]).toBe(0);
    wrapper.unmount();
  });

  it("全选受 20 上限约束，删除流程走确认弹窗并刷新", async () => {
    historyPayload = {
      items: Array.from({ length: 25 }, (_, i) => item(`j${i}`)),
      page: 1,
      size: 20,
      total: 25,
    };
    const wrapper = mountView();
    await (wrapper.vm as unknown as { fetchGallery: () => Promise<void> }).fetchGallery();
    await flushPromises();

    const selectAll = q("#gallery-select-all") as HTMLInputElement;
    selectAll.checked = true;
    selectAll.dispatchEvent(new Event("change", { bubbles: true }));
    await flushPromises();
    expect(toast.warning).toHaveBeenCalledWith(
      "单次最多选择 {v0} 个历史任务".replace("{v0}", "20"),
    );
    const checkboxCount = qa(".gallery-card-checkbox:checked").length;
    expect(checkboxCount).toBe(20);

    const batch = q("#btn-batch-delete") as HTMLButtonElement;
    expect(batch.disabled).toBe(false);
    batch.click();
    await flushPromises();
    expect(modal.confirm).toHaveBeenCalledTimes(1);
    expect(bridge.post).toHaveBeenCalledWith("webui/history/delete", {
      job_ids: expect.arrayContaining(["j0"]),
    });
    expect(store.removeJobs).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("成功删除 {v0} 个历史任务".replace("{v0}", "20"));
    wrapper.unmount();
  });

  it("再次生成回填工作台回调", async () => {
    const wrapper = mountView();
    await (wrapper.vm as unknown as { fetchGallery: () => Promise<void> }).fetchGallery();
    await flushPromises();

    const card = qa(".gallery-card").find((node) =>
      node.textContent?.includes("提示词-a"),
    ) as HTMLElement;
    const sparkle = card.querySelector('[aria-label="再次生成"]') as HTMLButtonElement;
    expect(sparkle.disabled).toBe(false);
    sparkle.click();
    expect(onRefillRequest).toHaveBeenCalledTimes(1);
    expect((onRefillRequest as any).mock.calls[0][0]).toBe("提示词-a");

    const reference = card.querySelector('[aria-label="设为工作台参考图"]') as HTMLButtonElement;
    reference.click();
    expect(onSetReference).toHaveBeenCalledWith("img-a.png");
    wrapper.unmount();
  });
});
