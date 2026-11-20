import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import KeywordsView from "../KeywordsView.vue";

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`));

let keywordsPayload: Record<string, unknown> = {
  revision: "rev-1",
  settings: {
    mode: "command",
    wrapper_left: "",
    wrapper_right: "",
    max_refs: 3,
  },
  bindings: [
    {
      name: "画风",
      words: ["水彩"],
      ref_ids: ["ref-aaaaaaaaaaaa"],
      prompt_append: "转为水彩",
      enabled: true,
      refs: [{ id: "ref-aaaaaaaaaaaa", label: "水彩参考", missing: false }],
    },
  ],
};
let previewPayload: Record<string, unknown> = {
  mode: "command",
  hits: [{ name: "画风", words: ["水彩"], prompt_append: "转为水彩" }],
  refs: [
    { id: "ref-aaaaaaaaaaaa", label: "水彩参考", missing: false },
    { id: "ref-bbbbbbbbbbbb", label: "缺失参考", missing: true },
  ],
  dropped: [{ binding: "画风", id: "ref-cccccccccccc" }],
  prompt_append: "转为水彩",
  slots: 3,
};
let saveShouldFail = false;

const toast = { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() };
const errorFeedback = { show: vi.fn() };
const modal = { confirm: vi.fn(), openCustom: vi.fn() };
const bridge = {
  isAvailable: () => true,
  get: vi.fn(async (route: string) => {
    if (route === "webui/keywords") return keywordsPayload;
    if (route === "webui/references") return { references: [] };
    return {};
  }),
  post: vi.fn(async (route: string, _body?: unknown) => {
    if (route === "webui/keywords") {
      if (saveShouldFail) throw new Error("conflict");
      return { ...keywordsPayload, revision: "rev-2" };
    }
    if (route === "webui/keywords/preview") return previewPayload;
    return {};
  }),
  upload: vi.fn(async (_route: string, _file: File) => ({
    path: "files/keyword_trigger/bindings/templates/binding/ref_files/test.png",
  })),
};

function mountView() {
  return mount(KeywordsView, {
    props: { bridge, modal, t, toast, errorFeedback },
    attachTo: document.body,
  });
}

const q = (selector: string) => document.querySelector(selector) as HTMLElement | null;
const qa = (selector: string) => Array.from(document.querySelectorAll(selector));

beforeEach(() => {
  vi.clearAllMocks();
  saveShouldFail = false;
});

describe("KeywordsView", () => {
  it("挂载即读取配置并渲染绑定", async () => {
    const wrapper = mountView();
    await flushPromises();

    expect(bridge.get).toHaveBeenCalledWith("webui/keywords");
    expect((wrapper.find("select").element as HTMLSelectElement).value).toBe("command");
    expect(qa(".keywords-binding").length).toBe(1);
    const inputs = qa(".keywords-binding .form-input") as HTMLInputElement[];
    expect(inputs[0]!.value).toBe("画风");
    expect(inputs[1]!.value).toBe("水彩");
    wrapper.unmount();
  });

  it("保存提交归一化后的绑定并应用返回的 revision", async () => {
    const wrapper = mountView();
    await flushPromises();
    bridge.post.mockClear();

    await wrapper.find(".keywords-save").trigger("click");
    await flushPromises();

    expect(bridge.post).toHaveBeenCalledWith("webui/keywords", {
      revision: "rev-1",
      settings: {
        mode: "command",
        wrapper_left: "",
        wrapper_right: "",
        max_refs: 3,
      },
      bindings: [
        {
          name: "画风",
          words: ["水彩"],
          ref_ids: ["ref-aaaaaaaaaaaa"],
          ref_files: [],
          ref_addresses: [],
          prompt_append: "转为水彩",
          enabled: true,
        },
      ],
    });
    expect(toast.success).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("空关键词绑定时保存被拦截", async () => {
    const wrapper = mountView();
    await flushPromises();
    bridge.post.mockClear();

    const firstRow = qa(".keywords-binding")[0] as HTMLElement;
    const wordsInput = firstRow.querySelectorAll(".form-input")[1] as HTMLInputElement;
    wordsInput.value = "  ";
    wordsInput.dispatchEvent(new Event("input"));

    await wrapper.find(".keywords-save").trigger("click");
    await flushPromises();

    expect(bridge.post).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("保存失败时提示并重新读取配置", async () => {
    saveShouldFail = true;
    const wrapper = mountView();
    await flushPromises();
    bridge.get.mockClear();

    await wrapper.find(".keywords-save").trigger("click");
    await flushPromises();

    expect(errorFeedback.show).toHaveBeenCalled();
    expect(bridge.get).toHaveBeenCalledWith("webui/keywords");
    wrapper.unmount();
  });

  it("预演调用 preview 接口并渲染命中、缺失与丢弃", async () => {
    const wrapper = mountView();
    await flushPromises();

    const textInput = wrapper.find(".keywords-preview-form .form-input");
    await textInput.setValue("把图改成水彩");
    await wrapper.find(".keywords-preview-form .comic-btn--cta").trigger("click");
    await flushPromises();

    expect(bridge.post).toHaveBeenCalledWith("webui/keywords/preview", {
      text: "把图改成水彩",
      manual_count: 0,
    });
    const result = q(".keywords-preview-result")!;
    expect(result.textContent).toContain("画风");
    expect(result.textContent).toContain("水彩参考");
    expect(result.textContent).toContain("缺失参考");
    expect(result.textContent).toContain("转为水彩");
    wrapper.unmount();
  });
});
