<script setup lang="ts">
import { computed, ref } from "vue";
import HubTemplateCard from "./HubTemplateCard.vue";
import LocalTemplateCard from "./LocalTemplateCard.vue";
import type {
  BridgeClientLike,
  HubState,
  HubTemplate,
  KeyInfo,
  ModalLike,
  StudioTemplate,
  Translator,
} from "./types";

const props = defineProps<{
  bridge: BridgeClientLike;
  modal: ModalLike;
  t: Translator;
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void };
  onUsed: (payload: { prompt?: string; template?: StudioTemplate | HubTemplate }) => void;
  onSaved?: () => Promise<void>;
}>();

const REFERENCE_VALUES = ["none", "optional", "required"];

// ---------- 状态 ----------
const templates = ref<StudioTemplate[]>([]);
const loaded = ref(false);
const loading = ref(false);
const saving = ref(false);
const loadFailed = ref(false);
const message = ref(props.t("打开提示词广场后加载。"));
const query = ref("");
const tag = ref("");
const scope = ref<"local" | "online">("local");
const hubTemplates = ref<HubTemplate[]>([]);
const hubNotice = ref("");
const hubState = ref<HubState | null>(null);
const hubTags = ref<{ tag?: string }[]>([]);
const mine = ref<HubTemplate[]>([]);
const hubLoading = ref(false);
let epoch = 0;

// ---------- 派生 ----------
const available = computed(
  () => !!props.bridge && (!props.bridge.isAvailable || props.bridge.isAvailable()),
);
const writable = computed(() => loaded.value && !loading.value && !saving.value && available.value);
const hubEnabled = computed(() => {
  const state = hubState.value || {};
  return state.mode !== "off" && state.enabled !== false;
});

function visibleTemplates() {
  const words = String(query.value || "")
    .trim()
    .toLowerCase();
  const terms = words ? words.split(/\s+/).filter(Boolean) : [];
  return templates.value.filter((item) => {
    if (tag.value && !(item.tags || []).includes(tag.value)) return false;
    if (!terms.length) return true;
    const haystack = [item.name, item.ai_description, (item.tags || []).join(" ")]
      .join(" ")
      .toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

function localTags() {
  const seen: string[] = [];
  for (const item of templates.value) {
    for (const tagValue of item.tags || []) if (!seen.includes(tagValue)) seen.push(tagValue);
  }
  return seen;
}

const mergedTagNames = computed(() => {
  const names = [...localTags()];
  for (const item of hubTags.value) {
    if (item.tag && !names.includes(item.tag)) names.push(item.tag);
  }
  return names;
});

const hubSummary = computed(() => {
  const state = hubState.value || {};
  if (!state.hash) return props.t("尚未同步在线模板。");
  return props.t("缓存 {v0} 条，哈希 {v1}", {
    v0: state.count ?? 0,
    v1: String(state.hash).slice(0, 12),
  });
});

const emptyText = computed(() => {
  if (hubLoading.value) return props.t("正在检索在线模板…");
  if (loaded.value) return props.t("没有符合条件的模板。");
  return props.t("尚未加载模板。");
});

// ---------- 基础动作 ----------
function byId(id: string) {
  return templates.value.find((item) => item.id === id) || null;
}

function hubById(id: string) {
  // 远程缓存与我的模板分属两个列表：查看/填入两边都要能找到。
  return (
    hubTemplates.value.find((item) => item.id === id) ||
    mine.value.find((item) => item.id === id) ||
    null
  );
}

function referenceLabel(value?: string) {
  if (value === "required") return props.t("需要参考图");
  if (value === "optional") return props.t("可选参考图");
  return props.t("不需要参考图");
}

async function load() {
  if (loading.value || saving.value) return;
  if (!available.value) {
    message.value = props.t("桥接不可用，请在 AstrBot Dashboard 中打开。");
    return;
  }
  const current = ++epoch;
  loading.value = true;
  message.value = props.t("正在加载提示词模板…");
  try {
    const payload = (await props.bridge.get("webui/templates")) as {
      templates?: StudioTemplate[];
    } | null;
    if (current !== epoch) return;
    templates.value = Array.isArray(payload?.templates) ? payload.templates : [];
    loaded.value = true;
    loadFailed.value = false;
    message.value = props.t("已加载 {v0} 个模板。", { v0: templates.value.length });
    // 本地页合并远程缓存，本地模板就绪后拉一次缓存检索与标签。
    void searchHub();
    if (!hubTags.value.length) void loadHubTags();
  } catch {
    if (current !== epoch) return;
    loadFailed.value = true;
    message.value = props.t("加载失败，请重新打开本页签。");
  } finally {
    if (current === epoch) loading.value = false;
  }
}

async function loadOnce() {
  if (loaded.value) return;
  await load();
}

async function searchHub() {
  if (!available.value || hubLoading.value) return;
  hubLoading.value = true;
  try {
    // query 必须走 params：拼进 endpoint 会被服务端当路径编码（? -> %3F）。
    const params: Record<string, string> = { q: query.value, limit: "50" };
    if (tag.value) params.tag = tag.value;
    const payload = (await props.bridge.get("webui/templates/hub", params)) as {
      templates?: HubTemplate[];
      notice?: string;
      state?: HubState;
    } | null;
    hubTemplates.value = Array.isArray(payload?.templates) ? payload.templates : [];
    hubNotice.value = payload?.notice || "";
    hubState.value = payload?.state || null;
  } catch {
    hubNotice.value = props.t("在线检索失败，请稍后重试。");
  } finally {
    hubLoading.value = false;
  }
}

async function syncHub(force = false) {
  if (!available.value) return;
  hubLoading.value = true;
  message.value = props.t("正在同步在线模板…");
  try {
    const state = (await props.bridge.post("webui/templates/hub/sync", { force })) as HubState;
    hubState.value = state;
    message.value = props.t("已同步，缓存 {v0} 条在线模板。", { v0: state?.count ?? 0 });
  } catch {
    message.value = props.t("同步失败，请稍后重试。");
  } finally {
    hubLoading.value = false;
    void searchHub();
  }
}

async function importHub(id: string) {
  if (!available.value || !id) return;
  try {
    const result = (await props.bridge.post("webui/templates/hub/import", {
      ids: [id],
    })) as { imported?: boolean } | null;
    message.value = result?.imported
      ? props.t("已导入为本地模板。")
      : props.t("导入失败，请检查缓存后重试。");
    await load();
  } catch {
    message.value = props.t("导入失败，请稍后重试。");
  }
}

// 只清理“我的模板”路径写下的提示：别处的成功反馈要留着。
function clearMineMessage() {
  if (
    message.value === props.t("请先填写在线模板密钥。") ||
    message.value === props.t("读取我的模板失败，请检查密钥。")
  ) {
    message.value = "";
  }
}

async function openMine() {
  // 连点会并发重入：加载期间直接返回，避免重复请求和连续弹窗。
  if (!available.value || hubLoading.value) return;
  hubLoading.value = true;
  try {
    const state = await loadHubState();
    // 只有最新状态明确表示未配置时才弹绑定窗口。
    if (state?.has_key === false) {
      mine.value = [];
      hubNotice.value = "";
      message.value = props.t("请先填写在线模板密钥。");
      void openKeyDialog();
      return;
    }
    const payload = (await props.bridge.get("webui/templates/hub/mine", {
      limit: "50",
    })) as { templates?: HubTemplate[]; state?: HubState } | null;
    mine.value = Array.isArray(payload?.templates) ? payload.templates : [];
    hubState.value = payload?.state || hubState.value;
    hubNotice.value = "";
    // 补填密钥后刷新成功：旧报错不能再挂在顶部。
    clearMineMessage();
  } catch {
    mine.value = [];
    hubNotice.value = "";
    message.value = props.t("读取我的模板失败，请检查密钥。");
  } finally {
    hubLoading.value = false;
  }
}

async function loadHubTags() {
  try {
    const payload = (await props.bridge.get("webui/templates/hub/tags", {
      limit: "50",
    })) as { tags?: { tag?: string }[] } | null;
    if (Array.isArray(payload?.tags)) hubTags.value = payload.tags;
  } catch {
    // 标签拉取失败不影响检索
  }
}

async function loadHubState(): Promise<HubState | null> {
  try {
    const state = (await props.bridge.get("webui/templates/hub", {
      q: "",
      limit: "1",
      state_only: "1",
    })) as { state?: HubState } | null;
    const next = state?.state || null;
    if (next) hubState.value = next;
    return next;
  } catch {
    // 状态刷新失败不影响主流程
    return null;
  }
}

async function request(method: string, path: string, body: unknown = null) {
  try {
    if (method === "GET") return await props.bridge.get(path);
    if (method === "DELETE") return await props.bridge.delete?.(path);
    return await props.bridge.post(path, body || {});
  } catch {
    message.value = props.t("在线操作失败，请稍后重试。");
    return null;
  }
}

function switchScope(next: "local" | "online") {
  if (scope.value === next) return;
  scope.value = next;
  hubNotice.value = "";
  // 本地页合并远程缓存，首次进入补拉在线标签。
  if (next === "local" && !hubTags.value.length) void loadHubTags();
  if (next === "online") void openMine();
}

function use(template?: StudioTemplate | HubTemplate | null) {
  if (!template) return;
  props.onUsed({ prompt: template.content, template });
  message.value = props.t("已把「{v0}」填入工作台。", { v0: template.name });
}

function copyTemplate(template?: StudioTemplate | HubTemplate | null) {
  // 复制=提示词正文进剪贴板，不再打开编辑框。
  if (!template) return;
  void props.clipboard.copy(template.content || "", props.t("已复制到剪贴板"));
}

// ---------- 本地模板维护 ----------
async function mutate(payload: Record<string, unknown>) {
  if (!available.value || saving.value) return;
  saving.value = true;
  message.value = props.t("正在保存…");
  try {
    await props.bridge.post("webui/templates", payload);
    await props.onSaved?.();
    await load();
  } catch {
    message.value = props.t("保存失败，请稍后重试。");
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(template?: StudioTemplate | null) {
  if (!template || !writable.value) return;
  const ok = await props.modal.confirm({
    title: props.t("删除模板"),
    content: props.t("确定删除「{v0}」？此操作不可撤销。", { v0: template.name }),
    confirmText: props.t("删除"),
    danger: true,
  });
  if (ok) await mutate({ action: "delete", id: template.id });
}

async function toggleAi(template: StudioTemplate, value: boolean) {
  if (!writable.value) return;
  await mutate({ action: "set_ai_enabled", id: template.id, ai_enabled: value });
}

// ---------- 在线模板动作 ----------
function editHub(id: string) {
  // 与新建模板同样的分块表单；服务端仅接受 name/ai_description/content/tags/reference。
  const hit = mine.value.find((item) => item.id === id);
  if (!hit) return;
  props.modal.openCustom({
    title: props.t("修改在线模板"),
    renderBody: (body) => {
      body.appendChild(field("name", props.t("名称"), hit.name, { max: 40 }));
      body.appendChild(
        field("ai_description", props.t("一句话说明"), hit.ai_description || "", { max: 80 }),
      );
      body.appendChild(textareaField("content", props.t("提示词正文"), hit.content || ""));
      body.appendChild(
        selectField("reference", props.t("参考图"), hit.reference || "none", referenceOptions()),
      );
      body.appendChild(field("tags", props.t("标签（空格分隔）"), (hit.tags || []).join(" ")));
      body.appendChild(
        el("p", { className: "tpl-note" }, [props.t("在线模板不支持修改默认分辨率与比例。")]),
      );
    },
    renderFooter: (footer, confirm, cancel) => {
      footer.appendChild(
        el(
          "button",
          { type: "button", className: "comic-btn comic-btn--outline", onClick: cancel },
          [props.t("取消")],
        ),
      );
      footer.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "comic-btn comic-btn--cta",
            onClick: async () => {
              const read = (name: string) => {
                const node = document.querySelector(`[data-tpl-editor="${name}"]`);
                return node ? (node as HTMLInputElement).value : "";
              };
              const name = String(read("name") || "").trim();
              const content = String(read("content") || "").trim();
              if (!name || !content) {
                message.value = props.t("名称和正文不能为空。");
                return;
              }
              const tags = String(read("tags") || "")
                .split(/\s+/)
                .filter(Boolean);
              confirm();
              const result = await request("POST", "webui/templates/hub", {
                action: "update",
                remote_id: hit.remote_id || hit.id,
                changes: {
                  id: hit.id,
                  name,
                  ai_description: String(read("ai_description") || "").trim(),
                  content,
                  reference: read("reference") || "none",
                  tags,
                },
              });
              message.value = result
                ? props.t("已保存修改。")
                : props.t("在线操作失败，请稍后重试。");
              await openMine();
            },
          },
          [props.t("保存修改")],
        ),
      );
    },
  });
}

async function dropHub(id: string) {
  const hit = mine.value.find((item) => item.id === id);
  if (!hit) return;
  const ok = await props.modal.confirm({
    title: props.t("删除在线模板"),
    content: props.t("将从在线模板库删除「{v0}」。", { v0: hit.name }),
    confirmText: props.t("删除"),
    danger: true,
  });
  if (!ok) return;
  const result = await request("POST", "webui/templates/hub", {
    action: "delete",
    remote_id: hit.remote_id || hit.id,
  });
  message.value = result ? props.t("已删除。") : props.t("在线操作失败，请稍后重试。");
  await openMine();
}

function reportHub(id: string) {
  const hit = hubTemplates.value.find((item) => item.id === id);
  if (!hit) return;
  const remoteId = hit.remote_id || hit.id;
  props.modal.openCustom({
    title: props.t("举报在线模板"),
    renderBody: (body) => {
      body.appendChild(
        el("p", { className: "tpl-note" }, [props.t("说明举报原因，服务端会人工复核。")]),
      );
      body.appendChild(
        el("textarea", {
          className: "comic-input comic-textarea tpl-package-input",
          rows: 5,
          dataset: { tplReport: "reason" },
        }),
      );
    },
    renderFooter: (footer, confirm, cancel) => {
      footer.appendChild(
        el(
          "button",
          { type: "button", className: "comic-btn comic-btn--outline", onClick: cancel },
          [props.t("取消")],
        ),
      );
      footer.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "comic-btn comic-btn--danger",
            onClick: async () => {
              const node = document.querySelector('[data-tpl-report="reason"]');
              const reason = node ? String((node as HTMLTextAreaElement).value || "").trim() : "";
              if (!reason) {
                message.value = props.t("举报需要填写原因");
                return;
              }
              const result = await request("POST", "webui/templates/hub", {
                action: "report",
                remote_id: remoteId,
                reason,
              });
              message.value = result
                ? props.t("已提交举报。")
                : props.t("在线操作失败，请稍后重试。");
              confirm();
            },
          },
          [props.t("提交举报")],
        ),
      );
    },
  });
}

function viewHub(id: string) {
  // 缓存已在本地，查看正文不需要联网。
  const hit = hubById(id);
  if (!hit) return;
  const meta = [
    hit.author ? `${props.t("标签")}: ${hit.author}` : "",
    hit.source_url ? `source: ${hit.source_url}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  openTextDialog(hit.name, meta ? `${meta}\n\n${hit.content || ""}` : hit.content || "");
}

function useHub(id: string) {
  const hit = hubById(id);
  if (!hit) return;
  props.onUsed({ prompt: hit.content, template: hit });
  message.value = props.t("已把「{v0}」填入工作台。", { v0: hit.name });
}

async function uploadHub(id: string) {
  if (!available.value || !id) return;
  // 上传需要密钥，未配置时先引导填写，避免点了才报 401。
  if (!(hubState.value || {}).has_key) {
    message.value = props.t("请先填写在线模板密钥。");
    void openKeyDialog();
    return;
  }
  let preview: { content?: string } | null = null;
  try {
    preview = (await props.bridge.post("webui/templates/hub", {
      action: "preview",
      id,
    })) as { content?: string } | null;
  } catch {
    message.value = props.t("读取待上传内容失败。");
    return;
  }
  const ok = await props.modal.confirm({
    title: props.t("上传到在线模板库"),
    content: props.t("将公开上传以下内容（不含预览图和用户信息）：\n\n{v0}", {
      v0: preview?.content || "",
    }),
    confirmText: props.t("上传"),
    danger: true,
  });
  if (!ok) return;
  try {
    await props.bridge.post("webui/templates/hub", { action: "upload", id });
    message.value = props.t("已上传，模板会立即公开。");
  } catch {
    message.value = props.t("上传失败，请稍后重试。");
  }
}

// ---------- 密钥弹窗 ----------
function storedEmail() {
  try {
    return String(localStorage.getItem("tpl_hub_email") || "");
  } catch {
    return "";
  }
}

function storeEmail(value: string) {
  try {
    localStorage.setItem("tpl_hub_email", value);
  } catch {
    // 隐私模式忽略
  }
}

function dialogValue(selector: string) {
  const node = document.querySelector(selector);
  return node ? String((node as HTMLInputElement).value || "").trim() : "";
}

function openKeyDialog() {
  // 后端回填当前密钥；输入框保持 password 类型，避免明文展示。
  let info: KeyInfo = { configured: false, masked: "" };
  let dialogClosed = false;
  let keyInput: HTMLInputElement | null = null;
  let emailInput: HTMLInputElement | null = null;
  // 回填值与用户输入分开记：异步 GET 迟到不得覆盖用户已改动的内容。
  let loadedKey = "";
  let keyLoaded = false;
  let dirty = false;

  const syncKeyInput = () => {
    if (!keyInput) return;
    if (!dirty) keyInput.value = info.key || "";
    keyInput.placeholder =
      info.configured && info.masked
        ? props.t("当前 {v0}（清空保存可移除）", { v0: info.masked })
        : "iph_...";
  };

  // renderFooter 提供的 confirm 引用：保存成功后先关弹窗，再刷新背景数据。
  let requestClose: () => void = () => {};
  props.modal.openCustom({
    title: props.t("在线模板密钥"),
    onClose: () => {
      dialogClosed = true;
    },
    renderFooter: (footer, confirm, cancel) => {
      requestClose = confirm;
      footer.appendChild(
        el(
          "button",
          { type: "button", className: "comic-btn comic-btn--outline", onClick: cancel },
          [props.t("关闭")],
        ),
      );
    },
    renderBody: (body) => {
      // 弹窗内反馈：主视图的 message 被弹窗遮住，点"测试连接"看不到结果。
      let feedback: HTMLParagraphElement | null = null;
      const setFeedback = (text: string) => {
        if (feedback) feedback.textContent = text;
      };
      feedback = el("p", { className: "tpl-note", role: "status", "aria-live": "polite" });

      body.appendChild(
        el("div", { className: "tpl-field" }, [
          el("label", { className: "tpl-field-label" }, [props.t("邮箱")]),
          (() => {
            emailInput = el("input", {
              type: "text",
              className: "comic-input",
              dataset: { tplKey: "email" },
              value: storedEmail(),
              placeholder: "you@example.com",
            }) as HTMLInputElement;
            return emailInput;
          })(),
          // 申请/重置放在邮箱框下面，就近操作。
          el(
            "button",
            {
              type: "button",
              className: "comic-btn comic-btn--sm comic-btn--outline",
              onClick: async () => {
                const value = emailInput ? String(emailInput.value || "").trim() : "";
                if (!value) {
                  setFeedback(props.t("请先填写邮箱"));
                  return;
                }
                storeEmail(value);
                setFeedback(props.t("正在提交…"));
                const result = await request("POST", "webui/templates/hub/key", { email: value });
                setFeedback(
                  result
                    ? props.t("已提交，请到邮箱确认后等待密钥。")
                    : props.t("在线操作失败，请稍后重试。"),
                );
              },
            },
            [props.t("申请密钥")],
          ),
        ]),
      );

      body.appendChild(
        el("div", { className: "tpl-field" }, [
          el("label", { className: "tpl-field-label" }, [props.t("在线模板密钥")]),
          el("div", { className: "tpl-key-row" }, [
            (() => {
              keyInput = el("input", {
                type: "password",
                className: "comic-input",
                dataset: { tplKey: "key" },
                autocomplete: "new-password",
                onInput: () => {
                  dirty = true;
                },
              }) as HTMLInputElement;
              return keyInput;
            })(),
            el(
              "button",
              {
                type: "button",
                className: "comic-btn comic-btn--sm comic-btn--outline",
                onClick: (event: Event) => {
                  if (!keyInput) return;
                  keyInput.type = keyInput.type === "password" ? "text" : "password";
                  (event.currentTarget as HTMLElement).textContent =
                    keyInput.type === "password" ? props.t("显示") : props.t("隐藏");
                },
              },
              [props.t("显示")],
            ),
            el(
              "button",
              {
                type: "button",
                className: "comic-btn comic-btn--sm comic-btn--outline",
                onClick: async () => {
                  setFeedback(props.t("正在测试…"));
                  const result = await request("POST", "webui/templates/hub/key", {
                    action: "test",
                  });
                  setFeedback(
                    result ? props.t("密钥可用。") : props.t("在线操作失败，请稍后重试。"),
                  );
                },
              },
              [props.t("测试连接")],
            ),
          ]),
          // 保存放在密钥框下面。
          el(
            "button",
            {
              type: "button",
              className: "comic-btn comic-btn--sm comic-btn--cta",
              onClick: async () => {
                const raw = keyInput ? String(keyInput.value || "").trim() : "";
                // 未回填完且用户没动过、也没输入内容时，才是真正的“未变化”。
                if ((keyLoaded && raw === loadedKey) || (!keyLoaded && !dirty && !raw)) {
                  setFeedback(props.t("密钥未变化。"));
                  return;
                }
                const result = (await request("POST", "webui/templates/hub/key", {
                  action: "save",
                  key: raw,
                })) as { masked?: string } | null;
                if (result) {
                  info = {
                    configured: Boolean(raw),
                    masked: String(result.masked || ""),
                    key: raw,
                  };
                  loadedKey = raw;
                  // 先关弹窗再刷新，避免背后列表加载造成卡顿。
                  requestClose();
                  if (scope.value === "online" && raw) {
                    // 已有密钥：openMine 内部会先刷新状态，不再额外请求。
                    void openMine();
                  } else if (scope.value === "online") {
                    // 密钥被清除：在线列表作废，只刷新状态。
                    mine.value = [];
                    hubNotice.value = "";
                    clearMineMessage();
                    void loadHubState();
                  } else {
                    void loadHubState();
                  }
                } else {
                  setFeedback(props.t("在线操作失败，请稍后重试。"));
                }
              },
            },
            [props.t("保存密钥")],
          ),
        ]),
      );

      body.appendChild(feedback);
    },
  });

  // 异步取回密钥：只在弹窗仍打开时更新输入框。
  void (async () => {
    try {
      const payload = (await props.bridge.get("webui/templates/hub/key")) as {
        configured?: boolean;
        masked?: string;
        key?: string;
      } | null;
      if (dialogClosed) return;
      loadedKey = String(payload?.key || "").trim();
      keyLoaded = true;
      info = {
        configured: !!payload?.configured,
        masked: String(payload?.masked || ""),
        key: loadedKey,
      };
      syncKeyInput();
      // 邮箱顺带回填最近使用值（GET 失败时保留本地缓存值即可）。
    } catch {
      // 取不到按未配置处理
    }
  })();
}

// ---------- 导入导出与编辑器 ----------
async function exportPackage() {
  if (!available.value) return;
  try {
    const payload = await props.bridge.get("webui/templates/export");
    openTextDialog(props.t("导出模板包"), JSON.stringify(payload, null, 2), {
      readOnly: true,
      copyable: true,
    });
  } catch {
    message.value = props.t("导出失败，请稍后重试。");
  }
}

function openImport() {
  openTextDialog(props.t("导入模板包"), "", {
    readOnly: false,
    submit: async (value) => {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(value);
      } catch {
        parsed = null;
      }
      if (!parsed) {
        message.value = props.t("模板包不是合法的 JSON。");
        return;
      }
      await mutate({ action: "import", package: parsed });
    },
  });
}

function openTextDialog(
  title: string,
  value: string,
  {
    readOnly = true,
    submit = null,
    submitText = null,
    copyable = false,
  }: {
    readOnly?: boolean;
    submit?: ((value: string) => void | Promise<void>) | null;
    submitText?: string | null;
    copyable?: boolean;
  } = {},
) {
  props.modal.openCustom({
    title,
    renderBody: (body) => {
      const area = el("textarea", {
        className: "comic-input comic-textarea tpl-package-input",
        rows: 16,
        readOnly: readOnly ? true : undefined,
        dataset: readOnly ? {} : { tplPackage: "1" },
      });
      area.value = value || "";
      body.appendChild(area);
    },
    renderFooter: (footer, confirm, cancel) => {
      if (copyable) {
        footer.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "comic-btn comic-btn--outline",
              onClick: async () => {
                const area = document.querySelector(
                  ".tpl-package-input",
                ) as HTMLTextAreaElement | null;
                await props.clipboard.copy(area ? area.value : "", props.t("已复制到剪贴板"));
              },
            },
            [props.t("复制")],
          ),
        );
      }
      footer.appendChild(
        el(
          "button",
          { type: "button", className: "comic-btn comic-btn--outline", onClick: cancel },
          [props.t("关闭")],
        ),
      );
      if (submit) {
        footer.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "comic-btn comic-btn--cta",
              onClick: async () => {
                const area = document.querySelector(
                  "[data-tpl-package]",
                ) as HTMLTextAreaElement | null;
                await submit(area ? area.value : "");
                confirm();
              },
            },
            [submitText || props.t("导入")],
          ),
        );
      }
    },
  });
}

function openEditor(template?: Partial<StudioTemplate> | null) {
  if (!template) return;
  const draft = {
    id: template.id || "",
    name: template.name || "",
    ai_description: template.ai_description || "",
    content: template.content || "",
    reference: template.reference || "none",
    tags: (template.tags || []).join(" "),
    defaults_resolution: template.defaults?.resolution || "",
    defaults_aspect_ratio: template.defaults?.aspect_ratio || "",
  };
  props.modal.openCustom({
    title: draft.id ? props.t("编辑模板") : props.t("新建模板"),
    renderBody: (body) => {
      body.appendChild(field("name", props.t("名称"), draft.name, { max: 40 }));
      body.appendChild(
        field("ai_description", props.t("一句话说明"), draft.ai_description, { max: 80 }),
      );
      body.appendChild(textareaField("content", props.t("提示词正文"), draft.content));
      body.appendChild(
        selectField("reference", props.t("参考图"), draft.reference, referenceOptions()),
      );
      body.appendChild(field("tags", props.t("标签（空格分隔）"), draft.tags));
      body.appendChild(
        field("defaults_resolution", props.t("默认分辨率（如 2K）"), draft.defaults_resolution),
      );
      body.appendChild(
        field("defaults_aspect_ratio", props.t("默认比例（如 16:9）"), draft.defaults_aspect_ratio),
      );
    },
    renderFooter: (footer, confirm, cancel) => {
      footer.appendChild(
        el(
          "button",
          { type: "button", className: "comic-btn comic-btn--outline", onClick: cancel },
          [props.t("取消")],
        ),
      );
      footer.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "comic-btn comic-btn--cta",
            onClick: async () => {
              const payload = collectEditor(draft);
              if (!payload) {
                message.value = props.t("名称和正文不能为空。");
                return;
              }
              confirm();
              await mutate(payload);
            },
          },
          [props.t("保存")],
        ),
      );
    },
  });
}

function referenceOptions() {
  return REFERENCE_VALUES.map((value) => ({
    value,
    label:
      value === "required"
        ? props.t("需要参考图")
        : value === "optional"
          ? props.t("可选参考图")
          : props.t("不需要参考图"),
  }));
}

function field(name: string, label: string, value: string, { max = 0 } = {}) {
  return el("div", { className: "tpl-field" }, [
    el("label", { className: "tpl-field-label", htmlFor: `studio-templates-${name}` }, [label]),
    el("input", {
      type: "text",
      id: `studio-templates-${name}`,
      className: "comic-input",
      dataset: { tplEditor: name },
      maxLength: max || undefined,
      value: value || "",
    }),
  ]);
}

function textareaField(name: string, label: string, value: string) {
  return el("div", { className: "tpl-field" }, [
    el("label", { className: "tpl-field-label", htmlFor: `studio-templates-${name}` }, [label]),
    el(
      "textarea",
      {
        id: `studio-templates-${name}`,
        className: "comic-input comic-textarea",
        rows: 8,
        dataset: { tplEditor: name },
      },
      [value || ""],
    ),
  ]);
}

function selectField(
  name: string,
  label: string,
  value: string,
  options: { value: string; label: string }[],
) {
  const select = el(
    "select",
    { id: `studio-templates-${name}`, className: "comic-select", dataset: { tplEditor: name } },
    options.map((option) =>
      el("option", { value: option.value, selected: option.value === value }, [option.label]),
    ),
  );
  return el("div", { className: "tpl-field" }, [
    el("label", { className: "tpl-field-label", htmlFor: `studio-templates-${name}` }, [label]),
    select,
  ]);
}

function collectEditor(draft: { id: string }): Record<string, unknown> | null {
  const read = (name: string) => {
    const node = document.querySelector(`[data-tpl-editor="${name}"]`);
    return node ? (node as HTMLInputElement).value : "";
  };
  const name = String(read("name") || "").trim();
  const content = String(read("content") || "").trim();
  if (!name || !content) return null;
  const defaults: Record<string, string> = {};
  const resolution = String(read("defaults_resolution") || "").trim();
  const aspectRatio = String(read("defaults_aspect_ratio") || "").trim();
  if (resolution) defaults.resolution = resolution;
  if (aspectRatio) defaults.aspect_ratio = aspectRatio;
  const tagsList = String(read("tags") || "")
    .split(/\s+/)
    .filter(Boolean);
  const template = {
    name,
    content,
    ai_description: String(read("ai_description") || "").trim(),
    reference: read("reference") || "none",
    tags: tagsList,
    ...(Object.keys(defaults).length ? { defaults } : {}),
  };
  if (draft.id) return { action: "update", id: draft.id, changes: template };
  return { action: "create", template };
}

// ---------- 弹窗内 DOM 构建的小工具 ----------
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, unknown> = {},
  children: (Node | string | null | undefined)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (val === undefined || val === null) continue;
    if (key === "className") node.className = String(val);
    else if (key === "onInput" && typeof val === "function")
      node.addEventListener("input", val as EventListener);
    else if (key === "onClick" && typeof val === "function")
      node.addEventListener("click", val as EventListener);
    else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(val as Record<string, unknown>)) {
        node.dataset[dataKey] = String(dataValue ?? "");
      }
    } else if (key === "value") (node as unknown as HTMLInputElement).value = String(val);
    else if (key === "selected") (node as HTMLOptionElement).selected = Boolean(val);
    else if (key === "readOnly") (node as HTMLTextAreaElement).readOnly = Boolean(val);
    else if (key === "checked") (node as HTMLInputElement).checked = Boolean(val);
    else if (key === "maxLength") (node as HTMLInputElement).maxLength = Number(val);
    else node.setAttribute(key, String(val));
  }
  for (const child of children) {
    node.append(child === null || child === undefined ? "" : child);
  }
  return node;
}

defineExpose({ loadOnce });
</script>

<template>
  <div class="tpl-heading">
    <h2 class="card-title">{{ t("提示词广场") }}</h2>
    <p class="tpl-note">
      {{ t("管理内置与本地模板：新建、编辑、导入导出，并把模板填入工作台。") }}
    </p>
  </div>

  <p class="tpl-status" role="status" aria-live="polite">{{ message }}</p>

  <div class="tpl-tabs" role="tablist">
    <button
      type="button"
      :class="
        scope === 'local'
          ? 'comic-btn comic-btn--sm comic-btn--cta'
          : 'comic-btn comic-btn--sm comic-btn--outline'
      "
      :aria-selected="scope === 'local'"
      @click="switchScope('local')"
    >
      {{ t("本地模板") }}
    </button>
    <button
      type="button"
      :class="
        scope === 'online'
          ? 'comic-btn comic-btn--sm comic-btn--cta'
          : 'comic-btn comic-btn--sm comic-btn--outline'
      "
      :aria-selected="scope === 'online'"
      @click="switchScope('online')"
    >
      {{ t("我的模板") }}
    </button>
  </div>

  <!-- 合并工具栏：本地页一个搜索框同时管本地与远程 -->
  <div v-if="scope === 'local'" class="tpl-toolbar">
    <input
      v-model="query"
      type="text"
      class="comic-input tpl-search"
      :placeholder="t('搜索名称、说明或标签')"
      :aria-label="t('搜索名称、说明或标签')"
    />
    <select v-model="tag" class="comic-select" :aria-label="t('按标签筛选')" @change="searchHub()">
      <option value="">{{ t("全部标签") }}</option>
      <option v-for="name in mergedTagNames" :key="name" :value="name">{{ name }}</option>
    </select>
    <div class="tpl-actions">
      <button
        type="button"
        class="comic-btn comic-btn--cta"
        :disabled="hubLoading"
        @click="searchHub()"
      >
        {{ t("检索") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        :disabled="hubLoading"
        @click="syncHub(true)"
      >
        {{ t("立即同步") }}
      </button>
      <button type="button" class="comic-btn comic-btn--outline" @click="openKeyDialog()">
        {{ t("密钥") }}
      </button>
    </div>
    <div class="tpl-actions">
      <button
        type="button"
        class="comic-btn comic-btn--cta"
        :disabled="!writable"
        @click="openEditor({})"
      >
        {{ t("新建模板") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        :disabled="!writable"
        @click="openImport()"
      >
        {{ t("导入") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        :disabled="!loaded"
        @click="exportPackage()"
      >
        {{ t("导出") }}
      </button>
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        :disabled="loading"
        @click="load()"
      >
        {{ t("刷新") }}
      </button>
    </div>
    <p class="tpl-hub-state">{{ hubSummary }}</p>
  </div>

  <!-- 我的模板工具栏 -->
  <div v-else class="tpl-toolbar">
    <div class="tpl-actions">
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        :disabled="hubLoading"
        @click="openMine()"
      >
        {{ t("刷新") }}
      </button>
      <button type="button" class="comic-btn comic-btn--outline" @click="openKeyDialog()">
        {{ t("密钥") }}
      </button>
    </div>
  </div>

  <div class="tpl-grid">
    <p v-if="hubNotice" class="tpl-status">{{ hubNotice }}</p>

    <template v-if="scope === 'local'">
      <p v-if="!visibleTemplates().length && !hubTemplates.length" class="tpl-empty">
        {{ emptyText }}
      </p>
      <LocalTemplateCard
        v-for="item in visibleTemplates()"
        :key="item.id"
        :template="item"
        :writable="writable"
        :hub-enabled="hubEnabled"
        :t="t"
        @use="use(item)"
        @edit="openEditor(item)"
        @copy="copyTemplate(item)"
        @upload="uploadHub(item.id)"
        @restore="mutate({ action: 'restore', id: item.id })"
        @remove="confirmDelete(item)"
        @toggle-ai="(value) => toggleAi(item, value)"
      />
      <HubTemplateCard
        v-for="item in hubTemplates"
        :key="`hub-${item.id}`"
        :template="item"
        :mine="false"
        :t="t"
        @view="viewHub(item.id)"
        @report="reportHub(item.id)"
        @use="useHub(item.id)"
        @import="importHub(item.id)"
      />
    </template>

    <template v-else>
      <p v-if="!mine.length" class="tpl-empty">
        {{ hubLoading ? t("正在读取我的模板…") : t("还没有上传过在线模板。") }}
      </p>
      <HubTemplateCard
        v-for="item in mine"
        :key="item.id"
        :template="item"
        :mine="true"
        :t="t"
        @view="viewHub(item.id)"
        @edit="editHub(item.id)"
        @drop="dropHub(item.id)"
        @use="useHub(item.id)"
      />
    </template>
  </div>
</template>
