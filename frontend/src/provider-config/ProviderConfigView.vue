<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import type {
  BridgeClientLike,
  CatalogState,
  DraftEntry,
  DraftState,
  EditorState,
  EntryValues,
  FieldSchema,
  ModalLike,
  ProviderSnapshot,
  SnapshotEntry,
  Translator,
} from "./types";

const props = defineProps<{
  bridge: BridgeClientLike;
  modal: ModalLike;
  t: Translator;
  onSaved?: () => Promise<void>;
}>();

const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const own = (object: unknown, key: string) => Object.prototype.hasOwnProperty.call(object, key);
const SECRET_FIELDS = new Set(["api_keys", "api_base", "proxy"]);
const GENERAL = new Set([
  "enabled",
  "priority",
  "model",
  "base_model",
  "model_alias",
  "endpoint_id",
  "endpoint_mode",
  "api_keys",
  "api_base",
  "proxy",
  "service_account_files",
  "project_id",
  "location",
]);
const GENERATION = new Set([
  "resolution",
  "aspect_ratio",
  "size",
  "size_mode",
  "custom_size",
  "default_size",
  "width",
  "height",
  "quality",
  "n",
  "negative_prompt",
  "seed",
  "output_format",
  "style",
  "style_type",
  "style_weight",
]);

// ---------- 状态 ----------
const tab = ref<"entries" | "polling" | "common">("entries");
const snapshot = shallowRef<ProviderSnapshot | null>(null);
const draft = ref<DraftState | null>(null);
let baseline = "";
const loaded = ref(false);
const loading = ref(false);
const saving = ref(false);
const conflict = ref(false);
const loadFailed = ref(false);
const message = ref(props.t("打开供应商配置后加载；修改仅在点击保存后生效。"));
const catalogs: Record<"edit" | "common", CatalogState> = {
  edit: freshCatalog(),
  common: freshCatalog(),
};
let visionRefreshToken = 0;
const visionRefreshing = ref(false);
const visionManual = ref(false);
const visionRefreshMessage = ref("");
const keysHidden = ref(true);
const editor = shallowRef<EditorState | null>(null);
const editorCleanups: (() => void)[] = [];
let epoch = 0;
let destroyed = false;
const rootEl = ref<HTMLElement | null>(null);
const commonFieldset = ref<HTMLElement | null>(null);
const editorCleanupsBind = editorCleanups;

function freshCatalog(): CatalogState {
  return {
    token: 0,
    loading: false,
    confirmation: null,
    models: [],
    search: "",
    message: "",
    host: null,
    results: null,
  };
}

// ---------- 派生 ----------
const available = computed(
  () => !!props.bridge && (!props.bridge.isAvailable || props.bridge.isAvailable()),
);
const writable = computed(
  () =>
    loaded.value &&
    !loading.value &&
    !loadFailed.value &&
    !saving.value &&
    available.value &&
    !snapshot.value?.requires_reload,
);
const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== baseline);
const saveDisabled = computed(
  () => !writable.value || conflict.value || !dirty.value || !!editor.value,
);
const reloadDisabled = computed(() => loading.value || saving.value || !available.value);
const pollingOrder = computed<string[]>(() => {
  if (!draft.value) return [];
  if (draft.value.provider_polling.length) return [...draft.value.provider_polling];
  // This is a configuration preview, NOT a reconstruction of runtime candidates.
  return [
    ...new Set(
      draft.value.entries
        .filter((entry) => supported(entry) && entry.values.enabled !== false)
        .map((entry) => entry.api_type),
    ),
  ];
});

// ---------- 查询辅助 ----------
function label(type: string) {
  return (
    snapshot.value?.templates?.[type]?.label ||
    snapshot.value?.provider_types?.find((item) => item.id === type)?.label ||
    type
  );
}
function metadata(entry: DraftEntry) {
  return entry.id == null
    ? null
    : snapshot.value?.entries?.find((item) => item.id === entry.id) || null;
}
function supported(entry: DraftEntry) {
  return (
    metadata(entry)?.supported !== false && own(snapshot.value?.templates ?? {}, entry.api_type)
  );
}
function fields(entry: DraftEntry): Record<string, FieldSchema> {
  return snapshot.value?.templates?.[entry.api_type]?.fields || {};
}
function secrets(entry: DraftEntry, common = false) {
  if (common) return snapshot.value?.common?.secrets || {};
  return metadata(entry)?.secrets || {};
}
function keysVisible(entry: DraftEntry) {
  return own(entry.values, "api_keys") || entry.id == null || !secrets(entry).api_keys?.count;
}

function keyChipText(entry: DraftEntry): string {
  const meta = metadata(entry);
  const count = Array.isArray(entry.values.api_keys)
    ? (entry.values.api_keys as unknown[]).length
    : meta?.secrets?.api_keys?.count || 0;
  // 无 API Key 但配置了文件/内联凭证的条目（如 vertex 服务账号）显示凭证状态而非「0 个」
  const credentialFiles = Array.isArray(entry.values.service_account_files)
    ? (entry.values.service_account_files as unknown[]).filter((item) => String(item).trim()).length
    : 0;
  if (!supported(entry)) return props.t("保留原值");
  if (count) return props.t("{v0} 个", { v0: count });
  if (credentialFiles) return props.t("JSON 凭证");
  return props.t("0 个");
}

// ---------- 加载 ----------
function adopt(payload: unknown) {
  const snap = payload as ProviderSnapshot;
  if (
    !snap ||
    typeof snap.revision !== "string" ||
    !Array.isArray(snap.entries) ||
    !snap.templates ||
    !snap.common?.values ||
    !snap.common_fields
  ) {
    throw new Error("Invalid provider configuration response");
  }
  invalidateCatalog("edit");
  invalidateCatalog("common");
  ++visionRefreshToken;
  visionRefreshing.value = false;
  visionRefreshMessage.value = "";
  snapshot.value = copy(snap) as ProviderSnapshot;
  const toDraft = (item: SnapshotEntry): DraftEntry => ({
    id: item.id ?? null,
    api_type: item.api_type,
    values: item.supported === false ? {} : (copy(item.values || {}) as EntryValues),
    secret_actions: {},
  });
  draft.value = {
    revision: snap.revision,
    provider_polling: copy(snap.provider_polling || []),
    entries: snap.entries.map(toDraft),
    common: { values: copy(snap.common.values || {}), secret_actions: {} },
  };
  baseline = JSON.stringify(draft.value);
  loaded.value = true;
  loadFailed.value = false;
  conflict.value = false;
}

async function load() {
  if (loading.value || saving.value) return;
  if (!available.value) {
    message.value = props.t("桥接不可用，请在 AstrBot Dashboard 中打开。");
    return;
  }
  const current = ++epoch;
  loading.value = true;
  message.value = props.t("正在加载供应商配置…");
  try {
    const snap = (await props.bridge.get("webui/providers")) as ProviderSnapshot | null;
    if (current !== epoch) return;
    adopt(snap);
    message.value = snapshot.value?.requires_reload
      ? props.t("运行时恢复失败，请重载插件后再使用。")
      : snapshot.value?.busy
        ? props.t("生成任务正在运行；可编辑草稿，任务结束后再保存。")
        : props.t("配置已加载。密钥仅在此配置页明文编辑；保存不会自动重载插件。");
  } catch {
    if (current !== epoch) return;
    loadFailed.value = true;
    message.value = props.t("加载失败，已有草稿保留；请重新加载后再编辑。");
  } finally {
    if (current === epoch) loading.value = false;
  }
}

async function loadOnce() {
  if (loaded.value) return;
  await load();
}

async function reload() {
  if (loading.value || saving.value) return;
  const current = epoch;
  if (
    dirty.value &&
    !(await props.modal.confirm({
      title: props.t("重新加载供应商配置"),
      content: props.t("将丢弃当前未保存草稿（包括新密钥），是否继续？"),
      danger: true,
    }))
  ) {
    return;
  }
  if (current === epoch) await load();
}

// ---------- 命令式 DOM 小工具 ----------
type Attrs = Record<string, unknown>;
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: (Node | string | null | undefined)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (val === undefined || val === null) continue;
    if (key === "className") node.className = String(val);
    else if (key === "onClick" && typeof val === "function")
      node.addEventListener("click", val as EventListener);
    else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(val as Record<string, unknown>)) {
        node.dataset[dataKey] = String(dataValue ?? "");
      }
    } else if (key === "value") (node as unknown as HTMLInputElement).value = String(val);
    else if (key === "checked") (node as HTMLInputElement).checked = Boolean(val);
    else if (key === "hidden") (node as HTMLElement).hidden = Boolean(val);
    else if (key === "disabled") (node as HTMLButtonElement).disabled = Boolean(val);
    else if (key === "readOnly") (node as HTMLInputElement).readOnly = Boolean(val);
    else if (key === "selected") (node as HTMLOptionElement).selected = Boolean(val);
    else node.setAttribute(key, String(val));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child);
  }
  return node;
}

function bindImperative(container: HTMLElement, cleanups: (() => void)[]) {
  const handlers: [string, EventListener][] = [
    ["click", (event) => handleClick(event)],
    ["input", (event) => handleInput(event)],
    ["change", (event) => handleInput(event)],
    [
      "paste",
      ((event: ClipboardEvent) => {
        const target = event.target as HTMLElement | null;
        if (
          target?.dataset.pcField !== "api_keys" ||
          !editor.value ||
          !keysVisible(editor.value.entry!)
        )
          return;
        const text = event.clipboardData?.getData("text") || "";
        if (/\r|\n/.test(text)) {
          event.preventDefault();
          openKeys(text);
        }
      }) as EventListener,
    ],
    ["keydown", (event) => handleKeydown(event as KeyboardEvent)],
    ["pointerdown", (event) => pointerDown(event)],
    ["pointermove", (event) => pointerMove(event)],
    ["pointerup", (event) => pointerEnd(event)],
    ["pointercancel", () => releasePointer()],
    ["lostpointercapture", () => releasePointer()],
  ];
  for (const [type, handler] of handlers) {
    container.addEventListener(type, handler);
    cleanups.push(() => container.removeEventListener(type, handler));
  }
}

// ---------- 公共设置字段网格（命令式端口） ----------
function renderCommonGrid() {
  const host = commonFieldset.value;
  if (!host || !draft.value || tab.value !== "common") return;
  host.replaceChildren();
  renderFields(host, draft.value.common, snapshot.value?.common_fields ?? {}, true);
}

function renderFields(
  container: HTMLElement,
  entry: {
    values: Record<string, unknown>;
    secret_actions: Record<string, { mode: string; value?: unknown }>;
  },
  fieldsMap: Record<string, FieldSchema>,
  common = false,
) {
  const groups: Record<string, HTMLElement> = {};
  const prefix = `provider-config-${common ? "common" : "edit"}`;
  for (const [id, title] of [
    ["general", props.t("通用")],
    ["generation", props.t("生成")],
    ["advanced", props.t("高级设置")],
  ] as [string, string][]) {
    const section = el(id === "advanced" ? "details" : "section", {
      className: "pc-field-group",
      "data-pc-group": id,
    });
    section.appendChild(el(id === "advanced" ? "summary" : "h3", {}, [title]));
    groups[id] = el("div", { className: "pc-field-grid" });
    section.appendChild(groups[id]);
    container.appendChild(section);
  }
  for (const [name, schema] of Object.entries(fieldsMap || {})) {
    const group =
      common || GENERAL.has(name) ? "general" : GENERATION.has(name) ? "generation" : "advanced";
    const fieldLabel = schema.description || name;
    const field = el("div", { className: "pc-field", "data-pc-field-wrap": name });
    const id = `${prefix}-${name}`;
    field.appendChild(el("label", { for: id }, [fieldLabel]));
    // string 型 api_keys（单凭证渠道）走普通文本输入，不进列表 Key 管理器
    const keyList = name === "api_keys" && schema.type !== "string";
    const secret = keyList || !!secrets(entry as DraftEntry, common)[name]?.present;
    if (keyList) renderKeys(field, entry as DraftEntry, schema, id);
    else if (common && name === "vision_provider_id")
      renderVisionProvider(field, entry, schema, id);
    else if (secret) renderSecret(field, entry as DraftEntry, name, schema, common, id);
    else if (schema.type === "file" || name === "service_account_json")
      renderCredentialField(field, entry, schema, name, common, id);
    else field.appendChild(control(entry.values[name] ?? schema.default, schema, name, common, id));
    if (
      (!common && (name === "model" || name === "endpoint_id")) ||
      (common && name === "vision_model")
    ) {
      renderCatalogField(field, entry as DraftEntry, name, common);
    }
    if (schema.hint)
      field.appendChild(el("p", { className: "pc-note", id: `${id}-hint` }, [schema.hint]));
    groups[group]!.appendChild(field);
  }
  for (const group of Object.values(groups)) {
    if (!group.children.length) (group.parentNode as HTMLElement).hidden = true;
  }
  updateConditions(container, entry, fieldsMap);
}

function renderCredentialField(
  field: HTMLElement,
  entry: { values: Record<string, unknown> },
  schema: FieldSchema,
  name: string,
  common: boolean,
  id: string,
) {
  // 凭证框：粘贴 JSON 内容或填写文件路径均可；「上传 .json」把文件内容填入框内。
  const current = Array.isArray(entry.values[name])
    ? ((entry.values[name] as unknown[])?.[0] ?? "")
    : String(entry.values[name] ?? "");
  const textarea = el("textarea", {
    id,
    dataset: { pcField: name, pcScope: common ? "common" : "edit", pcSecret: "false" },
    "aria-label": schema.description || name,
    rows: 6,
    autocomplete: "off",
    spellcheck: "false",
    className: "comic-input",
  }) as HTMLTextAreaElement;
  textarea.value = String(current);
  field.appendChild(textarea);
  field.appendChild(
    buttonDom(props.t("上传 .json"), "upload-credential", {
      dataset: { pcCredTarget: name },
      "aria-label": props.t("上传 {v0} JSON 文件", { v0: schema.description || name }),
    }),
  );
  const picker = el("input", {
    type: "file",
    accept: ".json,application/json",
    hidden: true,
    dataset: { pcFilePicker: name },
  }) as HTMLInputElement;
  picker.addEventListener("change", () => {
    const file = picker.files && picker.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      textarea.value = String(reader.result ?? "").trim();
      textarea.dispatchEvent(new Event("change", { bubbles: true }));
    };
    reader.readAsText(file);
    picker.value = "";
  });
  field.appendChild(picker);
}

function buttonDom(text: string, action: string, attrs: Attrs = {}) {
  // attrs 自带 dataset 时需与 pcAction 合并，不能整体覆盖。
  const { dataset, ...rest } = attrs as { dataset?: Record<string, unknown> };
  return el(
    "button",
    {
      type: "button",
      className: "comic-btn comic-btn--outline",
      "aria-label": text,
      dataset: { pcAction: action, ...dataset },
      ...rest,
    },
    [text],
  );
}

function control(
  value: unknown,
  schema: FieldSchema,
  name: string,
  common: boolean,
  id: string,
  secret = false,
): HTMLElement {
  const attrs: Attrs = {
    id,
    "aria-label": schema.description || name,
    dataset: {
      pcField: name,
      pcScope: common ? "common" : "edit",
      pcSecret: secret ? "true" : "false",
    },
    "aria-describedby": schema.hint
      ? `${secret ? id.replace(/-replacement$/, "") : id}-hint`
      : undefined,
    className: "comic-input",
  };
  let node: HTMLElement;
  if (Array.isArray(schema.options) && schema.type !== "list") {
    node = el(
      "select",
      attrs,
      schema.options.map((option) =>
        el("option", { value: String(option) }, [String(option) || props.t("（留空）")]),
      ),
    );
  } else if (schema.type === "list" || schema.type === "file") {
    node = el("textarea", { ...attrs, rows: 4, autocomplete: "off", spellcheck: "false" });
  } else {
    const numeric = schema.type === "int" || schema.type === "float";
    node = el("input", {
      ...attrs,
      type: schema.type === "bool" ? "checkbox" : numeric ? "number" : secret ? "password" : "text",
      min: schema.slider?.min,
      max: schema.slider?.max,
      step: numeric ? (schema.slider?.step ?? (schema.type === "int" ? 1 : "any")) : undefined,
      autocomplete: "off",
      spellcheck: "false",
    });
  }
  const input = node as HTMLInputElement;
  if (schema.type === "bool") input.checked = !!value;
  else if (schema.type === "list" || schema.type === "file")
    (node as HTMLTextAreaElement).value = Array.isArray(value)
      ? (value as string[]).join("\n")
      : String(value ?? "");
  else input.value = String(value ?? "");
  return node;
}

function renderKeys(container: HTMLElement, entry: DraftEntry, schema: FieldSchema, id: string) {
  const visible = keysVisible(entry);
  const keys = entry.values.api_keys;
  const row = el("div", { className: "pc-key-summary", dataset: { pcKeySummary: "" } });
  if (!visible) {
    const meta = secrets(entry).api_keys || {};
    const previews = meta.previews || [];
    if (previews.length) {
      row.appendChild(el("span", { className: "pc-key-chip" }, [previews[0]]));
      if (previews.length > 1)
        row.appendChild(el("span", { className: "pc-key-count" }, [`+${previews.length - 1}`]));
    } else {
      row.appendChild(
        el("span", { className: "pc-key-chip" }, [props.t("{v0} 个 Key", { v0: meta.count || 0 })]),
      );
    }
    container.appendChild(row);
    if (!own(entry.secret_actions, "api_keys")) entry.secret_actions.api_keys = { mode: "keep" };
    renderSecret(container, entry, "api_keys", schema, false, id);
    return;
  }
  const toggle = buttonDom(keysHidden.value ? props.t("显示") : props.t("隐藏"), "toggle-keys", {
    className: "comic-btn comic-btn--outline pc-key-toggle",
    "aria-label": keysHidden.value ? props.t("显示完整 API Key") : props.t("隐藏 API Key"),
    "aria-pressed": String(!keysHidden.value),
  });
  const list = Array.isArray(keys) ? (keys as string[]) : [];
  if (list.length <= 1) {
    const node = control(
      keysHidden.value && list[0] ? "••••••••" : list[0] || "",
      { ...schema, type: "string" },
      "api_keys",
      false,
      id,
    ) as HTMLInputElement;
    node.disabled = !visible || (keysHidden.value && !!list[0]);
    node.placeholder = props.t("输入 API Key");
    node.readOnly = keysHidden.value && !!list[0];
    row.appendChild(node);
  } else {
    row.appendChild(
      el("span", { className: "pc-key-chip" }, [
        keysHidden.value
          ? "••••••••"
          : list[0]!.length > 20
            ? `${list[0]!.slice(0, 20)}…`
            : list[0]!,
      ]),
    );
    row.appendChild(el("span", { className: "pc-key-count" }, [`+${list.length - 1}`]));
  }
  if (visible) row.appendChild(toggle);
  row.appendChild(
    buttonDom(list.length <= 1 ? props.t("添加更多") : props.t("管理 Key"), "manage-keys", {
      id: list.length > 1 ? id : undefined,
      disabled: !visible,
      "aria-label": props.t("管理 API Key"),
    }),
  );
  container.appendChild(row);
}

function renderSecret(
  container: HTMLElement,
  entry: DraftEntry,
  name: string,
  schema: FieldSchema,
  common: boolean,
  id: string,
) {
  // 已保存 Key 只显示掩码，所有变更都通过 secret_actions 提交。
  const meta = secrets(entry, common)[name];
  const action = entry.secret_actions[name] || { mode: "keep" };
  const fieldLabel = schema.description || name;
  const mode = el(
    "select",
    {
      id,
      className: "comic-select",
      "aria-label": props.t("{v0}操作", { v0: fieldLabel }),
      dataset: { pcSecretMode: name, pcScope: common ? "common" : "edit" },
    },
    [
      el("option", { value: "keep" }, [
        meta?.present
          ? props.t("保持原值{v0}", {
              v0:
                name === "api_keys"
                  ? props.t("（{v0} 个）", { v0: meta.count || 0 })
                  : props.t("（已配置）"),
            })
          : props.t("保持（未配置）"),
      ]),
      el("option", { value: "replace" }, [props.t("替换")]),
      el("option", { value: "clear" }, [props.t("清空")]),
    ],
  );
  if (name === "api_keys" && schema.type !== "string") {
    mode.appendChild(el("option", { value: "append" }, [props.t("追加")]));
  }
  (mode as HTMLSelectElement).value = action.mode;
  container.appendChild(mode);
  if (action.mode === "replace" || action.mode === "append") {
    container.appendChild(
      control(
        action.value ?? (schema.type === "list" ? [] : ""),
        schema,
        name,
        common,
        `${id}-replacement`,
        true,
      ),
    );
    container.appendChild(
      el("p", { className: "pc-note" }, [
        name === "api_keys"
          ? props.t("每行一个 Key；新值仅保存在当前页面内存中。")
          : props.t("新地址仅保存在当前页面内存中。"),
      ]),
    );
  } else if (action.mode === "clear") {
    container.appendChild(el("p", { className: "pc-note" }, [props.t("将在主保存后清空此字段。")]));
  }
}

function renderVisionProvider(
  container: HTMLElement,
  entry: { values: Record<string, unknown> },
  schema: FieldSchema,
  id: string,
) {
  const value = String(entry.values.vision_provider_id ?? schema.default ?? "");
  if (visionManual.value) {
    container.appendChild(
      control(value, { ...schema, options: undefined }, "vision_provider_id", true, id),
    );
  } else {
    const options: HTMLElement[] = [el("option", { value: "" }, [props.t("（留空禁用）")])];
    const providers = snapshot.value?.vision_providers || [];
    if (value && !providers.some((item) => item.id === value)) {
      options.push(
        el("option", { value }, [props.t("{v0}（当前选择不在可用名单，保留）", { v0: value })]),
      );
    }
    for (const provider of providers) {
      options.push(
        el("option", { value: provider.id }, [
          `${provider.label || provider.id}${provider.model ? ` · ${provider.model}` : ""}${
            provider.available === false ? props.t("（实例暂不可用）") : ""
          }`,
        ]),
      );
    }
    const select = el(
      "select",
      {
        id,
        className: "comic-select",
        "aria-label": schema.description || "vision_provider_id",
        dataset: { pcField: "vision_provider_id", pcScope: "common" },
      },
      options,
    ) as HTMLSelectElement;
    select.value = value;
    container.appendChild(select);
  }
  container.appendChild(
    el("div", { className: "pc-actions" }, [
      buttonDom(
        visionRefreshing.value ? props.t("正在刷新…") : props.t("刷新视觉提供商"),
        "refresh-vision",
        { disabled: visionRefreshing.value },
      ),
      buttonDom(
        visionManual.value ? props.t("返回提供商选择") : props.t("手填 ID（回退）"),
        "manual-vision",
      ),
    ]),
  );
  const warning = visionRefreshMessage.value || snapshot.value?.vision_providers_warning;
  if (warning) container.appendChild(el("p", { className: "pc-note", role: "status" }, [warning]));
}

function updateConditions(
  container: HTMLElement,
  entry: { values: Record<string, unknown> },
  fieldsMap: Record<string, FieldSchema>,
) {
  for (const node of Array.from(container.querySelectorAll("[data-pc-field-wrap]"))) {
    const name = (node as HTMLElement).dataset.pcFieldWrap as string;
    const condition = fieldsMap[name]?.condition;
    (node as HTMLElement).hidden =
      !!condition &&
      !Object.entries(condition).every(
        ([cname, expected]) => (entry.values[cname] ?? fieldsMap[cname]?.default) === expected,
      );
  }
}

// ---------- 模型目录 ----------
function invalidateCatalog(scope: "edit" | "common") {
  const state = catalogs[scope];
  ++state.token;
  state.loading = false;
  state.confirmation = null;
  state.models = [];
  state.search = "";
  state.message = "";
  renderCatalog(scope);
}

function renderCatalogField(
  container: HTMLElement,
  entry: DraftEntry,
  name: string,
  common: boolean,
) {
  const scope = common ? "common" : "edit";
  const capability = snapshot.value?.model_catalog?.[entry.api_type];
  if (!common && !capability?.supported) {
    container.appendChild(
      el("p", { className: "pc-note" }, [
        capability?.message || props.t("此供应商暂未接入模型目录，可继续手填模型。"),
      ]),
    );
    return;
  }
  const state = catalogs[scope];
  state.field = name;
  state.host = el("div", { className: "pc-catalog", dataset: { pcCatalog: scope } });
  container.appendChild(state.host);
  renderCatalog(scope);
}

function renderCatalog(scope: "edit" | "common") {
  const state = catalogs[scope];
  if (!state.host) return;
  state.host.replaceChildren();
  const provider = draft.value?.common.values.vision_provider_id;
  state.host.appendChild(
    buttonDom(state.loading ? props.t("正在拉取模型…") : props.t("拉取模型"), "fetch-models", {
      dataset: { pcScope: scope },
      disabled: state.loading || !writable.value || (scope === "common" && !provider),
    }),
  );
  if (state.message) {
    state.host.appendChild(el("p", { className: "pc-note", role: "status" }, [state.message]));
  }
  if (state.confirmation) {
    state.host.appendChild(
      el(
        "div",
        {
          className: "pc-target-confirm",
          role: "group",
          "aria-label": props.t("确认模型查询目标"),
        },
        [
          el("p", {}, [
            props.t(
              "将向 {v0} 发送该条目已保存的 Key。请确认目标可信；修改连接参数会取消本次确认。",
              {
                v0: state.confirmation.target,
              },
            ),
          ]),
          buttonDom(props.t("确认目标并拉取"), "confirm-target", { dataset: { pcScope: scope } }),
          buttonDom(props.t("取消目标确认"), "cancel-target", { dataset: { pcScope: scope } }),
        ],
      ),
    );
  }
  if (state.models?.length) {
    const search = el("input", {
      type: "search",
      "aria-label": props.t("搜索模型"),
      className: "comic-input",
      dataset: { pcSearch: "", pcScope: scope },
      autocomplete: "off",
    }) as HTMLInputElement;
    search.value = state.search || "";
    state.host.appendChild(search);
    state.results = el("div", {
      className: "pc-model-results",
      role: "group",
      "aria-label": props.t("可选模型"),
    });
    state.host.appendChild(state.results);
    renderCatalogResults(scope);
  } else {
    state.results = null;
  }
}

function renderCatalogResults(scope: "edit" | "common") {
  const state = catalogs[scope];
  if (!state.results) return;
  state.results.replaceChildren();
  const query = (state.search || "").trim().toLocaleLowerCase();
  state.models.forEach((model, index) => {
    if (!`${model.id} ${model.label}`.toLocaleLowerCase().includes(query)) return;
    state.results!.appendChild(
      buttonDom(
        model.label && model.label !== model.id ? `${model.label} · ${model.id}` : model.id,
        "choose-model",
        { dataset: { pcScope: scope, pcModel: index } },
      ),
    );
  });
  if (!state.results.children.length) {
    state.results.appendChild(
      el("p", { className: "pc-note" }, [props.t("没有匹配的模型。可继续手填。")]),
    );
  }
}

function catalogPayload(scope: "edit" | "common"): Record<string, unknown> | null {
  if (scope === "common") {
    return { kind: "vision", provider_id: draft.value?.common.values.vision_provider_id };
  }
  const entry = editor.value?.entry;
  if (!entry || !snapshot.value?.model_catalog?.[entry.api_type]?.supported) return null;
  const connectionFields = Object.fromEntries(
    Object.entries(fields(entry)).filter(([name]) => SECRET_FIELDS.has(name)),
  );
  return {
    kind: "entry",
    revision: draft.value?.revision,
    entry: normalized(entry, connectionFields),
    common: normalized(
      draft.value!.common,
      snapshot.value?.common_fields?.proxy ? { proxy: snapshot.value.common_fields.proxy } : {},
      true,
    ),
    confirmed_target: false,
  };
}

async function fetchModels(scope: "edit" | "common", confirmed = false) {
  const state = catalogs[scope];
  if (state.loading || !writable.value) return;
  let payload: Record<string, unknown> | null;
  try {
    if (confirmed) {
      if (!state.confirmation) return;
      payload = state.confirmation.payload as Record<string, unknown>;
      payload.confirmed_target = true;
    } else {
      payload = catalogPayload(scope);
    }
  } catch {
    state.message = props.t("连接参数无效，请检查 Key、地址和代理。");
    renderCatalog(scope);
    return;
  }
  if (!payload || (scope === "common" && !payload.provider_id)) return;
  // A generation token invalidates late replies without retaining a credential fingerprint.
  const token = ++state.token;
  state.confirmation = null;
  state.loading = true;
  state.message = "";
  state.models = [];
  state.search = "";
  renderCatalog(scope);
  try {
    const data = (await props.bridge.post("webui/providers/models", payload)) as {
      confirmation_required?: boolean;
      target?: string;
      models?: { id: string; label: string }[];
      warning?: string;
      truncated?: boolean;
    } | null;
    if (destroyed || state.token !== token) return;
    if (
      data?.confirmation_required === true &&
      scope === "edit" &&
      !confirmed &&
      typeof data.target === "string"
    ) {
      state.confirmation = { payload, target: data.target };
      return;
    }
    if (
      !Array.isArray(data?.models) ||
      data.models.some((model) => typeof model?.id !== "string" || typeof model?.label !== "string")
    ) {
      throw new Error("Invalid models");
    }
    state.models = copy(data.models) as { id: string; label: string }[];
    state.message = [
      data.models.length
        ? props.t("已获取 {v0} 个模型，点击才会填入；目录不保证模型支持当前任务。", {
            v0: data.models.length,
          })
        : props.t("未获取到模型，当前值保持不变，可继续手填。"),
      data.warning || "",
      data.truncated ? props.t("列表已截断，可手填未列出的模型。") : "",
    ]
      .filter(Boolean)
      .join(" ");
  } catch (error) {
    if (destroyed || state.token !== token) return;
    const err = error as {
      data?: Record<string, unknown>;
      status?: number;
      statusCode?: number;
      status_code?: number;
      response?: { status?: number; data?: Record<string, unknown> };
      message?: string;
    };
    const data = (err?.data || err?.response?.data || {}) as Record<string, unknown>;
    if (
      data.reason === "confirm_target" &&
      scope === "edit" &&
      !confirmed &&
      typeof data.target === "string"
    ) {
      state.confirmation = { payload, target: data.target as string };
    } else {
      const status = [err?.status, err?.statusCode, err?.status_code, err?.response?.status]
        .map(Number)
        .find((value) => value >= 100 && value <= 599);
      const text = String(err?.message || "");
      state.message =
        data.reason === "revision" || status === 409 || /供应商配置已更新/.test(text)
          ? props.t("配置版本已变化，请先应用或取消编辑，再重新加载配置；当前模型未更改。")
          : props.t("模型拉取失败，请检查连接、凭据和提供商状态后重试；当前模型保持不变。");
    }
  } finally {
    payload = null;
    if (!destroyed && state.token === token) {
      state.loading = false;
      renderCatalog(scope);
    }
  }
}

function chooseModel(scope: "edit" | "common", index: number) {
  const state = catalogs[scope];
  const model = state.models?.[index];
  const entry = scope === "common" ? draft.value?.common : editor.value?.entry;
  if (!model || !entry) return;
  entry.values[state.field as string] = model.id;
  const container = scope === "common" ? commonFieldset.value : editor.value?.body;
  const node = container?.querySelector(
    `[data-pc-field="${state.field}"]`,
  ) as HTMLInputElement | null;
  if (node) node.value = model.id;
  updateConditions(
    container as HTMLElement,
    entry,
    scope === "common" ? (snapshot.value?.common_fields ?? {}) : fields(entry as DraftEntry),
  );
}

async function refreshVision() {
  if (!writable.value || visionRefreshing.value) return;
  const token = ++visionRefreshToken;
  visionRefreshing.value = true;
  visionRefreshMessage.value = "";
  try {
    const data = (await props.bridge.get("webui/vision-providers")) as {
      vision_providers?: { id: string; label?: string; model?: string; available?: boolean }[];
      vision_providers_available?: boolean;
      vision_providers_warning?: string;
    } | null;
    if (destroyed || token !== visionRefreshToken) return;
    if (!Array.isArray(data?.vision_providers) || data.vision_providers_available === false) {
      throw new Error("Provider list unavailable");
    }
    if (snapshot.value) {
      snapshot.value.vision_providers = copy(data!.vision_providers) as NonNullable<
        ProviderSnapshot["vision_providers"]
      >;
      snapshot.value.vision_providers_available = data!.vision_providers_available as boolean;
      snapshot.value.vision_providers_warning = data!.vision_providers_warning as
        | string
        | undefined;
    }
    visionRefreshMessage.value =
      data!.vision_providers_warning || props.t("视觉提供商名单已刷新，其他草稿保持不变。");
  } catch {
    if (destroyed || token !== visionRefreshToken) return;
    visionRefreshMessage.value = props.t("刷新失败，当前选择、原名单和其他草稿均已保留。");
  } finally {
    if (!destroyed && token === visionRefreshToken) visionRefreshing.value = false;
  }
}

// ---------- 编辑器弹窗 ----------
function openEditor(entry: DraftEntry, index: number | null) {
  if (editor.value || !writable.value) return;
  const state: EditorState = {
    entry: copy(entry) as DraftEntry,
    index,
    body: null,
    footer: null,
    keys: null,
  };
  editor.value = state;
  props.modal.openCustom({
    title: `${index == null ? props.t("新增") : props.t("编辑条目 {v0}", { v0: index + 1 })} · ${label(entry.api_type)}`,
    variant: "parameters",
    onClose: () => {
      invalidateCatalog("edit");
      editorCleanups.splice(0).forEach((cleanup) => cleanup());
      editor.value = null;
      state.entry = null;
      state.keys = null;
    },
    renderBody: (body) => {
      state.body = body;
      bindImperative(body, editorCleanupsBind);
      renderEditorBody();
    },
    renderFooter: (footer, accept, cancel) => {
      state.accept = accept;
      state.cancel = cancel;
      state.footer = footer;
      bindImperative(footer, editorCleanupsBind);
      renderEditorFooter();
    },
  });
}

function renderEditorBody() {
  const state = editor.value;
  if (!state?.entry || !state.body) return;
  state.body.replaceChildren();
  const container = el("div", { className: "provider-config pc-editor" });
  state.feedback = el("p", { role: "status", className: "pc-status" }, [
    props.t("应用到草稿不会立即保存；关闭或取消将丢弃本次弹窗编辑。"),
  ]);
  container.appendChild(state.feedback);
  container.appendChild(
    el("p", { className: "pc-note" }, [
      props.t("未配置字段仅展示模板默认值，修改才写入；旧字段仍由服务器保留。"),
    ]),
  );
  const unknown = metadata(state.entry)?.unknown_fields || [];
  if (unknown.length) {
    container.appendChild(
      el("p", { className: "pc-note" }, [
        props.t("旧字段仅由服务器保留，不显示或提交其值：{v0}", { v0: unknown.join("、") }),
      ]),
    );
  }
  const fieldset = el("fieldset", { className: "pc-fieldset", disabled: !writable.value });
  renderFields(fieldset, state.entry, fields(state.entry));
  container.appendChild(fieldset);
  state.body.appendChild(container);
}

function keyInputDom(name: string, value: string, attrs: Attrs = {}) {
  const node = el(name === "batchValue" ? "textarea" : "input", {
    type: name === "batchValue" ? undefined : "text",
    className: "comic-input",
    autocomplete: "off",
    spellcheck: "false",
    dataset: { pcKeyInput: name },
    ...attrs,
  }) as HTMLInputElement;
  node.value = value;
  return node;
}

function keyActionDom(text: string, action: string, index: number, disabled = false) {
  return buttonDom(text, action, {
    className: "comic-btn comic-btn--outline pc-key-icon",
    dataset: { pcIndex: index },
    title: text,
    disabled,
  });
}

function renderKeyManager(focus = "") {
  const state = editor.value;
  const keys = state?.keys;
  if (!state || !keys || !state.body) return;
  const pane = el("div", {
    className: "provider-config pc-editor pc-keys",
    dataset: { pcKeyManager: "" },
  });
  pane.appendChild(
    el("div", { className: "pc-key-heading" }, [
      el("h3", {}, [keys.batch ? props.t("批量导入 Key") : props.t("API Key 管理")]),
      keys.batch
        ? el("span", { className: "pc-key-count" }, [
            props.t("{v0} 个 Key", { v0: keys.items.length }),
          ])
        : buttonDom(keysHidden.value ? props.t("显示") : props.t("隐藏"), "toggle-keys", {
            "aria-label": keysHidden.value ? props.t("显示完整 API Key") : props.t("隐藏 API Key"),
            "aria-pressed": String(!keysHidden.value),
          }),
    ]),
  );
  pane.appendChild(
    el("p", { className: "pc-note" }, [
      keys.batch
        ? props.t("每行一个，追加到当前列表并去重；不会覆盖已有 Key。")
        : props.t("点击一行编辑，右侧移除；完成管理后返回条目，主保存后才生效。"),
    ]),
  );
  pane.appendChild(
    el(
      "p",
      {
        className: "pc-note",
        role: "status",
        "aria-live": "polite",
        dataset: { pcKeyStatus: "" },
      },
      [keys.message],
    ),
  );
  if (keys.batch) {
    pane.appendChild(
      keyInputDom("batchValue", keys.batchValue, {
        rows: 7,
        "aria-label": props.t("批量导入 API Key"),
        placeholder: props.t("每行粘贴一个 Key"),
      }),
    );
  } else {
    const adding = keyInputDom("newValue", keys.newValue, {
      "aria-label": props.t("新增 API Key"),
      placeholder: props.t("输入新 Key，按 Enter 添加"),
      disabled: keys.editIndex >= 0,
    });
    const add = buttonDom(props.t("添加"), "add-key", {
      disabled: keys.editIndex >= 0 || !keys.newValue.trim(),
    });
    const batch = buttonDom(props.t("批量导入"), "batch-keys", { disabled: keys.editIndex >= 0 });
    pane.appendChild(el("div", { className: "pc-key-add" }, [adding, add, batch]));
    const list = el("ol", { className: "pc-key-list", "aria-label": props.t("API Key 列表") });
    keys.items.forEach((value, index) => {
      const editing = keys.editIndex === index;
      const line = el("li", { className: "pc-key-row", dataset: { pcKeyRow: index } });
      line.appendChild(
        el("span", { className: "pc-key-number", "aria-hidden": "true" }, [
          String(index + 1).padStart(2, "0"),
        ]),
      );
      line.appendChild(
        editing
          ? keyInputDom("editValue", keys.editValue, {
              "aria-label": props.t("编辑 Key {v0}", { v0: index + 1 }),
            })
          : buttonDom(keysHidden.value ? "••••••••" : value, "edit-key", {
              className: "pc-key-value",
              dataset: { pcIndex: index },
              "aria-label": props.t("编辑 Key {v0}", { v0: index + 1 }),
              title: keysHidden.value ? undefined : value,
              disabled: keys.editIndex >= 0,
            }),
      );
      const actions = el("div", { className: "pc-key-row-actions" });
      if (editing) actions.appendChild(keyActionDom(props.t("确认此 Key"), "save-key", index));
      actions.appendChild(
        keyActionDom(
          editing ? props.t("取消此 Key 编辑") : props.t("移除 Key {v0}", { v0: index + 1 }),
          editing ? "cancel-key-edit" : "remove-key",
          index,
          !editing && keys.editIndex >= 0,
        ),
      );
      line.appendChild(actions);
      list.appendChild(line);
    });
    if (!keys.items.length) {
      pane.appendChild(
        el("p", { className: "pc-key-empty" }, [props.t("暂无 Key，从上方添加或批量导入。")]),
      );
    } else {
      pane.appendChild(list);
    }
  }
  state.body.replaceChildren(pane);
  renderEditorFooter();
  const selector =
    focus ||
    (keys.batch
      ? '[data-pc-key-input="batchValue"]'
      : keys.editIndex >= 0
        ? '[data-pc-key-input="editValue"]'
        : '[data-pc-key-input="newValue"]');
  (
    (state.body.querySelector(selector) as HTMLElement | null) ||
    (state.body.querySelector('[data-pc-key-input="newValue"]') as HTMLElement | null)
  )?.focus();
}

function renderEditorFooter() {
  const state = editor.value;
  if (!state?.footer) return;
  const keys = state.keys;
  state.footer.replaceChildren();
  state.footer.appendChild(
    buttonDom(
      keys ? (keys.batch ? props.t("返回密钥列表") : props.t("取消密钥更改")) : props.t("取消编辑"),
      keys ? (keys.batch ? "cancel-key-batch" : "cancel-keys") : "cancel-edit",
    ),
  );
  state.footer.appendChild(
    buttonDom(
      keys ? (keys.batch ? props.t("导入到列表") : props.t("完成密钥管理")) : props.t("应用到草稿"),
      keys ? (keys.batch ? "import-keys" : "apply-keys") : "apply-edit",
      {
        className: "comic-btn comic-btn--cta",
        disabled:
          !!keys &&
          (keys.batch ? !keys.batchValue.trim() : keys.editIndex >= 0 || !!keys.newValue.trim()),
      },
    ),
  );
}

function openKeys(batchValue: string | null = null) {
  const state = editor.value;
  if (!state || !state.entry || state.keys || !keysVisible(state.entry) || !writable.value) return;
  invalidateCatalog("edit");
  catalogs.edit.host = null;
  state.keys = {
    items: copy(state.entry.values.api_keys || []) as string[],
    newValue: "",
    editIndex: -1,
    editValue: "",
    batch: batchValue !== null,
    batchValue: batchValue || "",
    message: "",
  };
  renderKeyManager();
}

function keyMessage(text: string) {
  const keys = editor.value?.keys;
  if (!keys) return;
  keys.message = text;
  const node = editor.value?.body?.querySelector("[data-pc-key-status]");
  if (node) node.textContent = text;
}

function checkedKeyItems(items: string[]) {
  const values = [...new Set(items.map((item) => item.trim()).filter(Boolean))];
  validate(values, fields(editor.value!.entry!).api_keys, "api_keys");
  return values;
}

function mutateKeys(action: string, index = -1) {
  const keys = editor.value?.keys;
  if (!keys) return;
  if (action === "cancel-key-batch") {
    keys.batch = false;
    keys.batchValue = "";
    renderKeyManager();
    return;
  }
  if (action === "cancel-key-edit") {
    keys.editIndex = -1;
    keys.editValue = "";
    renderKeyManager();
    return;
  }
  if (action === "batch-keys" && keys.editIndex < 0) {
    keys.batch = true;
    keys.batchValue = "";
    keys.message = "";
    renderKeyManager();
    return;
  }
  if (action === "edit-key" && keys.editIndex < 0 && keys.items[index] !== undefined) {
    keys.editIndex = index;
    keys.editValue = keys.items[index];
    keys.message = "";
    renderKeyManager();
    return;
  }
  if (action === "remove-key" && keys.editIndex < 0 && keys.items[index] !== undefined) {
    keys.items.splice(index, 1);
    keys.message = props.t("已从管理草稿移除，取消密钥更改可恢复。");
    renderKeyManager(
      `[data-pc-action="remove-key"][data-pc-index="${Math.min(index, keys.items.length - 1)}"]`,
    );
    return;
  }
  try {
    if (action === "add-key" && keys.editIndex < 0 && keys.newValue.trim()) {
      const previous = keys.items.length;
      keys.items = checkedKeyItems([...keys.items, keys.newValue]);
      keys.newValue = "";
      keys.message =
        keys.items.length === previous
          ? props.t("该 Key 已存在，未重复添加。")
          : props.t("已添加到管理草稿。");
    } else if (action === "save-key" && keys.editIndex >= 0) {
      if (!keys.editValue.trim()) {
        keyMessage(props.t("Key 不能为空，需要删除请使用移除按钮。"));
        return;
      }
      const values = [...keys.items];
      values[keys.editIndex] = keys.editValue;
      keys.items = checkedKeyItems(values);
      keys.editIndex = -1;
      keys.editValue = "";
      keys.message = props.t("已更新管理草稿。");
    } else if (action === "import-keys" && keys.batch && keys.batchValue.trim()) {
      const previous = keys.items.length;
      keys.items = checkedKeyItems([...keys.items, ...keys.batchValue.split(/\r\n?|\n/)]);
      keys.message = props.t("已追加 {v0} 个 Key，重复与空白已忽略。", {
        v0: keys.items.length - previous,
      });
      keys.batchValue = "";
      keys.batch = false;
    } else return;
    renderKeyManager();
  } catch (error) {
    keyMessage((error as Error).message);
  }
}

function finishKeys(accepted: boolean) {
  const state = editor.value;
  const keys = state?.keys;
  if (!state || !keys) return;
  if (accepted) {
    if (keys.editIndex >= 0 || keys.newValue.trim() || keys.batch) return;
    try {
      const values = checkedKeyItems(keys.items);
      if (JSON.stringify(values) !== JSON.stringify(state.entry!.values.api_keys || [])) {
        state.entry!.values.api_keys = values;
        delete state.entry!.secret_actions.api_keys;
      }
    } catch (error) {
      keyMessage((error as Error).message);
      return;
    }
  }
  state.keys = null;
  renderEditorBody();
  renderEditorFooter();
  (state.body?.querySelector('[data-pc-action="manage-keys"]') as HTMLElement | null)?.focus();
}

function applyEditor() {
  const state = editor.value;
  if (!state || state.keys || !state.entry) return;
  try {
    const entry = normalized(state.entry, fields(state.entry)) as unknown as DraftEntry;
    if (!draft.value) return;
    if (state.index == null) draft.value.entries.push(entry);
    else draft.value.entries[state.index] = entry;
    state.accept?.();
  } catch (error) {
    if (state.feedback) state.feedback.textContent = (error as Error).message;
  }
}

function validate(value: unknown, schema: FieldSchema | undefined, name: string) {
  const fail = (text: string) => {
    throw new Error(`${schema?.description || name}：${text}`);
  };
  if (schema?.type === "bool" && typeof value !== "boolean") fail(props.t("请选择启用或禁用。"));
  if (schema?.type === "string" && (typeof value !== "string" || value.length > 16384)) {
    fail(props.t("文本过长或类型无效。"));
  }
  if (schema?.type === "int" || schema?.type === "float") {
    if (!Number.isFinite(value as number) || (schema.type === "int" && !Number.isInteger(value))) {
      fail(props.t("请输入有效数字。"));
    }
    if (
      schema.slider &&
      ((schema.slider.min != null && (value as number) < schema.slider.min) ||
        (schema.slider.max != null && (value as number) > schema.slider.max))
    ) {
      fail(props.t("数字超出允许范围。"));
    }
  }
  if (schema?.type === "list") {
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
      fail(props.t("请每行输入一项。"));
    }
    const list = value as string[];
    if (name === "api_keys" && (list.length > 200 || list.some((item) => item.length > 8192))) {
      fail(props.t("最多 200 个 Key，每项最多 8192 字符。"));
    }
    if (list.some((item) => item.length > 16384)) fail(props.t("列表项过长。"));
  }
  if (schema?.type === "file") {
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
      fail(props.t("凭证内容无效。"));
    }
    if ((value as string[]).some((item) => item.length > 16384)) fail(props.t("凭证内容过长。"));
  }
  if (Array.isArray(schema?.options)) {
    const options = schema.options as unknown[];
    const bad = Array.isArray(value)
      ? (value as unknown[]).some((item) => !options.includes(item))
      : !options.includes(value);
    if (bad) fail(props.t("请选择允许的选项。"));
  }
}

function normalized(
  entry: DraftEntry | DraftState["common"],
  fieldsMap: Record<string, FieldSchema>,
  common = false,
): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  const secretActions: Record<string, { mode: string; value?: unknown }> = {};
  for (const [name, schema] of Object.entries(fieldsMap || {})) {
    if (name === "api_keys") {
      const action = entry.secret_actions[name];
      if (action) {
        secretActions[name] = copy(action);
        if (["replace", "append"].includes(action.mode)) validate(action.value, schema, name);
      } else if (own(entry.values, name)) {
        if (schema.type === "string") {
          // 单凭证渠道（如 vertex）：api_keys 为字符串
          const key = String(entry.values[name] ?? "").trim();
          validate(key, schema, name);
          values[name] = key;
        } else {
          const keys = [
            ...new Set(
              (entry.values[name] as string[]).map((value) => value.trim()).filter(Boolean),
            ),
          ];
          validate(keys, schema, name);
          values[name] = keys;
        }
      }
      continue;
    }
    const secret =
      SECRET_FIELDS.has(name) &&
      (secrets(entry as DraftEntry, common)[name]?.present || own(entry.secret_actions, name));
    if (secret) {
      const action = entry.secret_actions[name] || { mode: "keep" };
      secretActions[name] = copy(action);
      if (action.mode === "replace") validate(action.value, schema, name);
    } else if (own(entry.values, name)) {
      let value = entry.values[name];
      if (schema.type === "int" || schema.type === "float") {
        if (typeof value === "string" && !value.trim()) {
          throw new Error(props.t("{v0}：请输入数字。", { v0: schema.description || name }));
        }
        value = Number(value);
      }
      const original = common
        ? snapshot.value?.common?.values
        : metadata(entry as DraftEntry)?.values;
      if (
        !original ||
        !own(original, name) ||
        JSON.stringify(value) !== JSON.stringify(original[name])
      ) {
        validate(value, schema, name);
      }
      values[name] = value;
    }
  }
  return common
    ? { values, secret_actions: secretActions }
    : {
        id: (entry as DraftEntry).id,
        api_type: (entry as DraftEntry).api_type,
        values,
        secret_actions: secretActions,
      };
}

async function save() {
  if (!writable.value || conflict.value || editor.value || !dirty.value || !draft.value) return;
  let payload: Record<string, unknown>;
  try {
    payload = {
      revision: draft.value.revision,
      provider_polling: [...draft.value.provider_polling],
      entries: draft.value.entries.map((entry) =>
        supported(entry)
          ? normalized(entry, fields(entry))
          : { id: entry.id, api_type: entry.api_type, values: {}, secret_actions: {} },
      ),
      common: normalized(draft.value.common, snapshot.value?.common_fields ?? {}, true),
    };
  } catch (error) {
    message.value = (error as Error).message;
    return;
  }
  const current = ++epoch;
  saving.value = true;
  message.value = props.t("正在保存，请稍候…");
  let saved = false;
  try {
    const snap = (await props.bridge.post("webui/providers", payload)) as ProviderSnapshot | null;
    if (destroyed || current !== epoch) return;
    adopt(snap);
    message.value = snapshot.value?.requires_reload
      ? props.t("配置需要重载插件恢复；当前不可继续编辑或生成。")
      : props.t("供应商配置已保存，运行时已更新；未自动重载插件。");
    saved = true;
  } catch (error) {
    if (destroyed || current !== epoch) return;
    const err = error as {
      data?: Record<string, unknown>;
      reason?: string;
      message?: string;
      requires_reload?: boolean;
      status?: number;
      statusCode?: number;
      status_code?: number;
      code?: number | string;
      response?: { status?: number };
    };
    const data = (err?.data || {}) as Record<string, unknown>;
    const reason = (data.reason as string) || err?.reason;
    const text = String(err?.message || "");
    const busy = reason === "busy" || text.includes(props.t("生成任务正在运行"));
    const status = [
      err?.status,
      err?.statusCode,
      err?.status_code,
      err?.code,
      err?.response?.status,
    ]
      .map(Number)
      .find((value) => value >= 100 && value <= 599);
    conflict.value =
      !busy &&
      (reason === "revision" ||
        status === 409 ||
        /\b409\b|conflict|revision|配置.*(?:变更|冲突|过期|更新)/i.test(text));
    if (
      data.requires_reload ||
      err?.requires_reload ||
      (status === 503 && text.includes(props.t("重载")))
    ) {
      if (snapshot.value) snapshot.value.requires_reload = true;
    }
    message.value = snapshot.value?.requires_reload
      ? props.t("保存未完成，草稿保留。运行时恢复失败，请重载插件。")
      : busy
        ? props.t("生成任务正在运行，草稿完整保留；任务结束后可再次保存。")
        : conflict.value
          ? props.t("配置版本冲突（409），草稿完整保留。请先重新加载，不能直接覆盖服务器配置。")
          : (status === 400 || status === 503) && text
            ? props.t("{v0}；草稿已保留。", { v0: text })
            : props.t("保存失败，草稿完整保留；请稍后重试。");
  } finally {
    if (!destroyed && current === epoch) saving.value = false;
  }
  if (saved && !destroyed) {
    try {
      await props.onSaved?.();
    } catch {
      if (!destroyed && current === epoch) {
        message.value = props.t("供应商配置已保存，但工作台能力刷新失败；请稍后刷新。");
      }
    }
  }
}

// ---------- 轮询排序 ----------
function movePolling(from: number, to: number) {
  if (!writable.value || !draft.value) return;
  const order = pollingOrder.value;
  if (from < 0 || to < 0 || from >= order.length || to >= order.length || from === to) return;
  const moved = order.splice(from, 1)[0];
  if (moved === undefined) return;
  order.splice(to, 0, moved);
  draft.value.provider_polling = order;
  message.value = props.t("已更新手动轮询顺序，主保存后生效；未参与的供应商将被跳过。");
  void nextTick(() => {
    (
      rootEl.value?.querySelector(
        `[data-pc-action="handle"][data-pc-index="${to}"]`,
      ) as HTMLElement | null
    )?.focus();
  });
}

let pointer: {
  id: number;
  handle: HTMLElement;
  from: number;
  to: number;
  rows: HTMLElement[];
} | null = null;

function pointerDown(event: Event) {
  const target = event.target as HTMLElement;
  const handle = target.closest('[data-pc-action="handle"]') as HTMLElement | null;
  const mouse = event as PointerEvent;
  if (
    !handle ||
    !writable.value ||
    pointer ||
    mouse.isPrimary === false ||
    (mouse.button != null && mouse.button !== 0)
  )
    return;
  event.preventDefault();
  const from = Number(handle.dataset.pcIndex);
  pointer = {
    id: mouse.pointerId,
    handle,
    from,
    to: from,
    rows: Array.from(rootEl.value?.querySelectorAll("[data-pc-poll-index]") ?? []) as HTMLElement[],
  };
  handle.setPointerCapture?.(mouse.pointerId);
  handle.classList.add("pc-dragging");
}

function pointerMove(event: Event) {
  if (!pointer || pointer.id !== (event as PointerEvent).pointerId) return;
  event.preventDefault();
  const mouse = event as PointerEvent;
  const other = pointer.rows.filter((_, index) => index !== pointer!.from);
  pointer.to = other.filter((row) => {
    const rect = row.getBoundingClientRect();
    return mouse.clientY > rect.top + rect.height / 2;
  }).length;
  pointer.rows.forEach((row, index) =>
    row.classList.toggle("pc-drop-target", index === pointer!.to),
  );
}

function pointerEnd(event: Event) {
  if (!pointer || pointer.id !== (event as PointerEvent).pointerId) return;
  const current = pointer;
  releasePointer();
  movePolling(current.from, current.to);
}

function releasePointer() {
  if (!pointer) return;
  const current = pointer;
  pointer = null;
  current.handle.classList.remove("pc-dragging");
  current.rows.forEach((row) => row.classList.remove("pc-drop-target"));
  if (current.handle.hasPointerCapture?.(current.id))
    current.handle.releasePointerCapture(current.id);
}

// ---------- 事件路由 ----------
function handleInput(event: Event) {
  if (!writable.value) return;
  const node = event.target as HTMLElement;
  if (node.dataset.pcEnabled != null) {
    const row = draft.value?.entries[Number(node.dataset.pcEnabled)];
    const checked = (node as HTMLInputElement).checked;
    if (row && supported(row) && row.values.enabled !== checked) {
      row.values.enabled = checked;
    }
    return;
  }
  const keyInput = node.dataset.pcKeyInput;
  const keys = editor.value?.keys;
  if (keys) {
    if (
      ["newValue", "editValue", "batchValue"].includes(keyInput as string) &&
      keys[keyInput as "newValue"] !== (node as HTMLInputElement).value
    ) {
      keys[keyInput as "newValue"] = (node as HTMLInputElement).value;
      const add = editor.value?.body?.querySelector(
        '[data-pc-action="add-key"]',
      ) as HTMLButtonElement | null;
      if (add) add.disabled = keys.editIndex >= 0 || !keys.newValue.trim();
      renderEditorFooter();
    }
    return;
  }
  const common = node.dataset.pcScope === "common";
  const scope = common ? "common" : "edit";
  if (node.dataset.pcSearch != null) {
    if (catalogs[scope].search === (node as HTMLInputElement).value) return;
    catalogs[scope].search = (node as HTMLInputElement).value;
    renderCatalogResults(scope);
    return;
  }
  const entry = common ? draft.value?.common : editor.value?.entry;
  if (!entry) return;
  const fieldsMap = common ? (snapshot.value?.common_fields ?? {}) : fields(entry as DraftEntry);
  const modeField = node.dataset.pcSecretMode;
  if (modeField && event.type === "change") {
    if (!SECRET_FIELDS.has(modeField) || !own(fieldsMap, modeField)) return;
    const mode = (node as HTMLSelectElement).value;
    if (!["keep", "replace", "append", "clear"].includes(mode)) return;
    const previous = entry.secret_actions[modeField];
    entry.secret_actions[modeField] = ["replace", "append"].includes(mode)
      ? {
          mode,
          value:
            previous?.mode === "replace"
              ? previous.value
              : (fieldsMap[modeField] as FieldSchema).type === "list"
                ? []
                : "",
        }
      : { mode };
    delete entry.values[modeField];
    invalidateCatalog(scope);
    if (common && modeField === "proxy") invalidateCatalog("edit");
    if (common) renderCommonGrid();
    else renderEditorBody();
    return;
  }
  const name = node.dataset.pcField;
  if (!name || !own(fieldsMap, name)) return;
  const schema = fieldsMap[name];
  if (!schema) return;
  const inputValue = (node as HTMLInputElement).value;
  const value =
    schema.type === "bool"
      ? (node as HTMLInputElement).checked
      : schema.type === "list"
        ? inputValue
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean)
        : schema.type === "file"
          ? inputValue.trim()
            ? [inputValue.trim()]
            : []
          : inputValue;
  const current =
    node.dataset.pcSecret === "true" ? entry.secret_actions[name]?.value : entry.values[name];
  const keyList = name === "api_keys" && schema.type !== "string";
  const next = keyList ? [...new Set(value as string[])] : value;
  // Text inputs also emit change on blur; do not replace a button mid-click.
  if (JSON.stringify(current) === JSON.stringify(next)) return;
  if (node.dataset.pcSecret === "true") {
    entry.secret_actions[name] = { mode: entry.secret_actions[name]?.mode || "replace", value };
  } else if (keyList) {
    // 省略显示的占位值不是真实 Key，忽略对其的程序化编辑。
    const entryDraft = entry as DraftEntry;
    if (
      !keysVisible(entryDraft) ||
      (keysHidden.value &&
        ((entryDraft.values.api_keys as string[]) || []).length > 0 &&
        value === "••••••••")
    )
      return;
    entryDraft.values.api_keys = [...new Set(value as string[])];
    delete entryDraft.secret_actions.api_keys;
  } else {
    entry.values[name] = value;
  }
  if (["api_keys", "api_base", "proxy", "vision_provider_id"].includes(name)) {
    invalidateCatalog(scope);
    if (common && name === "proxy") invalidateCatalog("edit");
  }
  updateConditions(
    (common ? commonFieldset.value : editor.value?.body) as HTMLElement,
    entry,
    fieldsMap,
  );
}

function addEntry() {
  const type = rootEl.value?.querySelector("[data-pc-new-type]") as HTMLSelectElement | null;
  const typeId = type?.value;
  if (
    !typeId ||
    !own(snapshot.value?.templates ?? {}, typeId) ||
    !draft.value ||
    draft.value.entries.length >= 100
  )
    return;
  const template = snapshot.value?.templates?.[typeId];
  if (!template) return;
  const values: Record<string, unknown> = {};
  for (const [name, schema] of Object.entries(template.fields ?? {})) {
    if (name !== "api_keys" && own(schema, "default")) values[name] = copy(schema.default);
  }
  if (own(template.fields ?? {}, "enabled")) values.enabled = false;
  openEditor({ id: null, api_type: typeId, values, secret_actions: {} } as DraftEntry, null);
}

async function handleClick(event: Event) {
  const target = event.target as HTMLElement;
  const button = target.closest("[data-pc-action]") as HTMLButtonElement | null;
  if (!button || button.disabled || destroyed) return;
  const action = button.dataset.pcAction;
  if (!action) return;
  event.preventDefault();
  if (action === "tab") {
    tab.value = button.dataset.pcTab as "entries" | "polling" | "common";
    void nextTick(() => {
      (rootEl.value?.querySelector(`[data-pc-tab="${tab.value}"]`) as HTMLElement | null)?.focus();
    });
    return;
  }
  if (action === "reload") {
    void reload();
    return;
  }
  if (action === "cancel-edit") {
    editor.value?.cancel?.();
    return;
  }
  if (action === "cancel-keys") {
    finishKeys(false);
    return;
  }
  if (action === "cancel-key-edit" || action === "cancel-key-batch") {
    mutateKeys(action);
    return;
  }
  if (action === "toggle-keys") {
    keysHidden.value = !keysHidden.value;
    if (editor.value?.keys) {
      renderKeyManager();
      return;
    }
    renderEditorBody();
    return;
  }
  if (!writable.value) return;
  const scope = (button.dataset.pcScope as "edit" | "common") || "edit";
  if (action === "manage-keys") {
    openKeys();
    return;
  }
  if (action === "apply-keys") {
    finishKeys(true);
    return;
  }
  if (editor.value?.keys) {
    mutateKeys(action, Number(button.dataset.pcIndex));
    return;
  }
  if (action === "refresh-vision") {
    void refreshVision();
    return;
  }
  if (action === "manual-vision") {
    visionManual.value = !visionManual.value;
    return;
  }
  if (action === "fetch-models") {
    void fetchModels(scope);
    return;
  }
  if (action === "confirm-target") {
    void fetchModels(scope, true);
    return;
  }
  if (action === "cancel-target") {
    invalidateCatalog(scope);
    return;
  }
  if (action === "choose-model") {
    chooseModel(scope, Number(button.dataset.pcModel));
    return;
  }
  const index = Number(button.dataset.pcIndex);
  if (action === "save") {
    void save();
    return;
  }
  if (action === "upload-credential") {
    const fieldName = button.dataset.pcCredTarget as string;
    const pickerHost = (editor.value?.body ||
      commonFieldset.value ||
      rootEl.value) as HTMLElement | null;
    (
      pickerHost?.querySelector(`[data-pc-file-picker="${fieldName}"]`) as HTMLElement | null
    )?.click();
    return;
  }
  if (action === "add") {
    addEntry();
    return;
  }
  if (action === "edit") {
    const entry = draft.value?.entries[index];
    if (entry && supported(entry)) openEditor(entry, index);
    return;
  }
  if (action === "apply-edit") {
    applyEditor();
    return;
  }
  if (action === "delete") {
    const entry = draft.value?.entries[index];
    if (!entry) return;
    const current = epoch;
    if (
      !(await props.modal.confirm({
        title: props.t("删除条目 {v0}", { v0: index + 1 }),
        content: props.t("主保存后将永久删除本条配置及其密钥；其他同类型条目不受影响。是否删除？"),
        danger: true,
      }))
    )
      return;
    if (!writable.value || current !== epoch || draft.value?.entries[index] !== entry) return;
    draft.value?.entries.splice(index, 1);
    return;
  }
  if (action === "auto") {
    if (!draft.value) return;
    draft.value.provider_polling = [];
    message.value = props.t("已恢复自动轮询，主保存后生效。");
    return;
  }
  if (action === "up" || action === "down") {
    movePolling(index, index + (action === "up" ? -1 : 1));
    return;
  }
  if (action === "add-poll") {
    const type = button.dataset.pcType as string;
    if (!snapshot.value?.provider_types?.some((item) => item.id === type) || !draft.value) return;
    const order = pollingOrder.value;
    if (!order.includes(type)) order.push(type);
    draft.value.provider_polling = order;
    message.value = props.t("已切换为手动轮询；未参与类型将被跳过，主保存后生效。");
    return;
  }
  if (action === "remove-poll" && draft.value?.provider_polling.length) {
    draft.value.provider_polling.splice(index, 1);
    message.value = draft.value.provider_polling.length
      ? props.t("已移出轮询，配置条目仍保留。")
      : props.t("轮询列表为空，已恢复自动跟随有效配置。");
  }
}

function handleKeydown(event: KeyboardEvent) {
  const keys = editor.value?.keys;
  if (keys) {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (keys.batch) mutateKeys("cancel-key-batch");
      else if (keys.editIndex >= 0) mutateKeys("cancel-key-edit");
      else finishKeys(false);
      return;
    }
    const name = (event.target as HTMLElement).dataset?.pcKeyInput;
    if (
      event.key === "Enter" &&
      writable.value &&
      ["newValue", "editValue"].includes(name as string)
    ) {
      event.preventDefault();
      event.stopPropagation();
      mutateKeys(name === "newValue" ? "add-key" : "save-key");
    }
    return;
  }
  const tabNode = (event.target as HTMLElement).closest?.("[data-pc-tab]");
  if (tabNode && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const tabs = ["entries", "polling", "common"] as const;
    const current = tabs.indexOf(tab.value);
    const index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? 2
          : (current + (event.key === "ArrowRight" ? 1 : -1) + 3) % 3;
    tab.value = tabs[index] ?? "entries";
    void nextTick(() => {
      (rootEl.value?.querySelector(`[data-pc-tab="${tab.value}"]`) as HTMLElement | null)?.focus();
    });
  }
  const handle = (event.target as HTMLElement).closest?.('[data-pc-action="handle"]');
  if (handle && writable.value && ["ArrowUp", "ArrowDown"].includes(event.key)) {
    event.preventDefault();
    const index = Number((handle as HTMLElement).dataset.pcIndex);
    movePolling(index, index + (event.key === "ArrowUp" ? -1 : 1));
  }
  if (event.key === "Escape" && pointer) {
    event.preventDefault();
    releasePointer();
  }
}

// ---------- 命令式区域的响应式触发 ----------
watch(
  [tab, loading, saving, visionManual, visionRefreshing, loadFailed, loaded],
  () => {
    renderCommonGrid();
  },
  // DOM 更新后再渲染：字段网格宿主（commonFieldset）随当前页签挂载。
  { flush: "post" },
);
watch(
  () => draft.value && JSON.stringify(draft.value.common.secret_actions),
  () => {
    if (tab.value === "common") renderCommonGrid();
  },
);

onBeforeUnmount(() => {
  destroyed = true;
  ++epoch;
  ++visionRefreshToken;
  invalidateCatalog("edit");
  invalidateCatalog("common");
  releasePointer();
  editor.value?.cancel?.();
  editorCleanups.splice(0).forEach((cleanup) => cleanup());
  editor.value = null;
});

// 测试与宿主编排需要读取内部状态；refs 在暴露后自动解包。
defineExpose({ loadOnce, draft, snapshot, catalogs, conflict, dirty, message });
</script>

<template>
  <div
    ref="rootEl"
    class="provider-config"
    @click="handleClick"
    @input="handleInput"
    @change="handleInput"
    @keydown="handleKeydown"
    @pointerdown="pointerDown"
    @pointermove="pointerMove"
    @pointerup="pointerEnd"
    @pointercancel="releasePointer"
    @lostpointercapture="releasePointer"
  >
    <div class="pc-heading">
      <div>
        <h2>{{ t("供应商配置") }}</h2>
        <p class="pc-note">{{ t("配置与创作参数独立；保存只更新供应商设置，不修改限流。") }}</p>
      </div>
      <div class="pc-actions">
        <button
          type="button"
          class="comic-btn comic-btn--outline"
          data-pc-action="reload"
          :aria-label="t('重新加载')"
          :disabled="reloadDisabled"
        >
          {{ t("重新加载") }}
        </button>
        <button
          type="button"
          class="comic-btn comic-btn--cta"
          data-pc-action="save"
          :aria-label="saving ? t('正在保存…') : t('保存供应商配置')"
          :disabled="saveDisabled"
        >
          {{ saving ? t("正在保存…") : t("保存供应商配置") }}
        </button>
      </div>
    </div>

    <p class="pc-status" role="status" aria-live="polite">{{ message }}</p>
    <span class="pc-note" data-pc-dirty>{{
      dirty ? t("有未保存的修改") : t("无未保存的修改")
    }}</span>

    <div class="pc-tabs" role="tablist" :aria-label="t('供应商配置分类')">
      <button
        v-for="[id, text] in [
          ['entries', t('配置条目')],
          ['polling', t('轮询排序')],
          ['common', t('公共设置')],
        ] as const"
        :key="id"
        type="button"
        class="comic-btn comic-btn--outline"
        role="tab"
        :data-pc-action="'tab'"
        :data-pc-tab="id"
        :aria-selected="tab === id"
        :tabindex="tab === id ? 0 : -1"
      >
        {{ text }}
      </button>
    </div>

    <section v-if="tab === 'entries'" role="tabpanel" class="pc-panel" aria-label="entries">
      <template v-if="draft">
        <div class="pc-actions">
          <select class="comic-select" :aria-label="t('新增供应商类型')" data-pc-new-type>
            <option
              v-for="type in (snapshot?.provider_types || []).filter((type) =>
                own(snapshot?.templates ?? {}, type.id),
              )"
              :key="type.id"
              :value="type.id"
            >
              {{ type.label }}
            </option>
          </select>
          <button
            type="button"
            class="comic-btn comic-btn--outline"
            data-pc-action="add"
            :aria-label="t('新增配置条目')"
            :disabled="draft.entries.length >= 100"
          >
            {{ t("新增配置条目") }}
          </button>
        </div>
        <p class="pc-note">
          {{
            t(
              "以下为原始配置，包括禁用和不完整条目，并非当前有效候选。同类型按优先级降序尝试；同优先级按此表顺序。",
            )
          }}
        </p>
        <div class="pc-table-scroll" tabindex="0" :aria-label="t('配置表，可横向滚动')">
          <table class="pc-table" :aria-label="t('供应商原始配置条目')">
            <thead>
              <tr>
                <th
                  v-for="head in [
                    t('条目 / 供应商'),
                    t('模型'),
                    t('状态'),
                    t('优先级'),
                    t('密钥'),
                    t('操作'),
                  ]"
                  :key="head"
                  scope="col"
                >
                  {{ head }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(entry, index) in draft.entries"
                :key="`${entry.id ?? 'new'}-${index}`"
                :data-pc-entry="index"
              >
                <td>{{ index + 1 }} · {{ label(entry.api_type) }}</td>
                <td>
                  {{
                    supported(entry)
                      ? entry.values.model || entry.values.endpoint_id || t("未填写")
                      : t("旧条目（只可保留或删除）")
                  }}
                </td>
                <td>
                  <label class="pc-enabled">
                    <input
                      v-model="entry.values.enabled"
                      type="checkbox"
                      role="switch"
                      :aria-label="t('启用条目 {v0}', { v0: index + 1 })"
                      :data-pc-enabled="index"
                      :disabled="!supported(entry) || !writable"
                    />
                    {{
                      !supported(entry)
                        ? t("不支持")
                        : entry.values.enabled === false
                          ? t("已禁用")
                          : t("已启用")
                    }}
                  </label>
                </td>
                <td>{{ supported(entry) ? (entry.values.priority ?? 0) : "—" }}</td>
                <td>{{ supported(entry) ? keyChipText(entry) : t("保留原值") }}</td>
                <td>
                  <div class="pc-actions">
                    <button
                      type="button"
                      class="comic-btn comic-btn--outline"
                      data-pc-action="edit"
                      :aria-label="t('编辑条目 {v0}', { v0: index + 1 })"
                      :data-pc-index="index"
                      :disabled="!supported(entry)"
                    >
                      {{ t("编辑") }}
                    </button>
                    <button
                      type="button"
                      class="comic-btn comic-btn--outline"
                      data-pc-action="delete"
                      :aria-label="t('删除条目 {v0}', { v0: index + 1 })"
                      :data-pc-index="index"
                    >
                      {{ t("删除") }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!draft.entries.length" class="pc-empty">
          {{ t("尚无配置条目。选择供应商后添加独立配置。") }}
        </p>
      </template>
    </section>

    <section v-else-if="tab === 'polling' && draft" role="tabpanel" class="pc-panel">
      <h3>{{ !draft.provider_polling.length ? t("自动跟随有效配置") : t("手动轮询顺序") }}</h3>
      <p class="pc-note">
        {{
          !draft.provider_polling.length
            ? t(
                "当前自动跟随后端有效配置。下方仅预览已启用的配置类型，不代表有效候选；拖动、上移或下移后切换为手动轮询。",
              )
            : t(
                "按下方顺序尝试供应商；未参与的供应商不参与聊天自动生成，但仍可在工作台选择，其配置不会删除。",
              )
        }}
      </p>
      <button
        type="button"
        class="comic-btn comic-btn--outline"
        data-pc-action="auto"
        :aria-label="t('恢复自动轮询')"
        :disabled="!draft.provider_polling.length"
      >
        {{ t("恢复自动轮询") }}
      </button>
      <ol
        class="pc-poll-list"
        :aria-label="
          !draft.provider_polling.length ? t('自动轮询配置类型预览') : t('参与轮询的供应商')
        "
      >
        <li
          v-for="(type, index) in pollingOrder"
          :key="type"
          class="pc-poll-row"
          :data-pc-poll-index="index"
        >
          <button
            type="button"
            class="comic-btn comic-btn--outline pc-drag-handle"
            data-pc-action="handle"
            :aria-label="t('拖动排序 {v0}', { v0: label(type) })"
            :data-pc-index="index"
          >
            {{ t("拖动") }}
          </button>
          <span class="pc-poll-name">{{ index + 1 }}. {{ label(type) }}</span>
          <div class="pc-actions">
            <button
              type="button"
              class="comic-btn comic-btn--outline"
              data-pc-action="up"
              :aria-label="t('上移 {v0}', { v0: label(type) })"
              :data-pc-index="index"
              :disabled="index === 0"
            >
              {{ t("上移") }}
            </button>
            <button
              type="button"
              class="comic-btn comic-btn--outline"
              data-pc-action="down"
              :aria-label="t('下移 {v0}', { v0: label(type) })"
              :data-pc-index="index"
              :disabled="index === pollingOrder.length - 1"
            >
              {{ t("下移") }}
            </button>
            <button
              type="button"
              class="comic-btn comic-btn--outline"
              data-pc-action="remove-poll"
              :aria-label="t('移出轮询 {v0}', { v0: label(type) })"
              :data-pc-index="index"
              :disabled="!draft.provider_polling.length"
            >
              {{ t("移出轮询") }}
            </button>
          </div>
        </li>
      </ol>
      <p class="pc-note">
        {{
          t(
            "仅排序手柄接管触摸拖动；也可使用上移/下移按钮，或聚焦手柄按方向键。移出最后一项将恢复自动。",
          )
        }}
      </p>
      <h3>{{ t("未参与 / 可添加") }}</h3>
      <div class="pc-actions">
        <button
          v-for="type in (snapshot?.provider_types || []).filter(
            (type) => !pollingOrder.includes(type.id),
          )"
          :key="type.id"
          type="button"
          class="comic-btn comic-btn--outline"
          data-pc-action="add-poll"
          :data-pc-type="type.id"
          :aria-label="t('加入轮询 {v0}', { v0: type.label })"
        >
          {{ t("加入轮询 {v0}", { v0: type.label }) }}
        </button>
        <span
          v-if="!(snapshot?.provider_types || []).some((type) => !pollingOrder.includes(type.id))"
          class="pc-note"
        >
          {{ t("没有其他供应商类型。") }}
        </span>
      </div>
    </section>

    <section v-else-if="tab === 'common' && draft" role="tabpanel" class="pc-panel">
      <fieldset ref="commonFieldset" class="pc-fieldset" :disabled="!writable"></fieldset>
    </section>
  </div>
</template>
