<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import type { BridgeClientLike, ModalLike, Translator } from "../provider-config/types";

interface RefState {
  id: string;
  label: string;
  missing: boolean;
}

interface FileState {
  path: string;
  label: string;
  missing: boolean;
  gen: string;
}

interface AddressState {
  address: string;
  kind: string;
  missing: boolean;
  gen: string;
}
interface Binding {
  name: string;
  wordsText: string;
  ref_ids: string[];
  ref_files: string[];
  ref_addresses: string[];
  files: FileState[];
  addresses: AddressState[];
  addressInput: string;
  prompt_append: string;
  enabled: boolean;
}

interface PreviewHit {
  name: string;
  words: string[];
  prompt_append: string;
}

interface PreviewRef {
  id: string;
  label: string;
  missing: boolean;
  kind?: string;
}

interface PreviewResult {
  mode: string;
  hits: PreviewHit[];
  refs: PreviewRef[];
  dropped: { binding: string; id: string }[];
  prompt_append: string;
  slots: number;
}

interface Props {
  bridge: {
    get: (path: string, params?: Record<string, string>) => Promise<unknown>;
    post: (path: string, body?: unknown) => Promise<unknown>;
    upload: (path: string, file: File) => Promise<unknown>;
    isAvailable?: () => boolean;
  };
  t: (key: string, params?: Record<string, unknown>) => string;
  modal: ModalLike;
  toast: {
    success: (m: string) => void;
    warning: (m: string) => void;
    error: (m: string) => void;
    info: (m: string) => void;
  };
  errorFeedback: { show: (err: unknown, fallback: string) => void };
}

const props = defineProps<Props>();

const t = (key: string, params?: Record<string, unknown>) => props.t(key, params);

// ---------- 状态 ----------
const revision = ref("");
const loading = ref(false);
const saving = ref(false);
const bindings = ref<Binding[]>([]);
const destroyed = ref(false);
const previewText = ref("");
const manualCount = ref(0);
const previewResult = ref<PreviewResult | null>(null);
const previewing = ref(false);

const settings = reactive({
  mode: "command",
  wrapper_left: "",
  wrapper_right: "",
  max_refs: 3,
});

const unavailable = () => !props.bridge.isAvailable?.();

// ---------- 数据 ----------
function toForm(entry: Record<string, unknown>): Binding {
  const words = Array.isArray(entry.words) ? (entry.words as string[]) : [];
  return {
    name: String(entry.name || ""),
    wordsText: words.join("，"),
    ref_ids: Array.isArray(entry.ref_ids) ? (entry.ref_ids as string[]) : [],
    ref_files: Array.isArray(entry.ref_files) ? (entry.ref_files as string[]) : [],
    ref_addresses: Array.isArray(entry.ref_addresses)
      ? (entry.ref_addresses as string[])
      : [],
    files: Array.isArray(entry.files) ? (entry.files as FileState[]) : [],
    addresses: Array.isArray(entry.addresses)
      ? (entry.addresses as AddressState[])
      : [],
    addressInput: "",
    prompt_append: String(entry.prompt_append || ""),
    enabled: entry.enabled !== false,
  };
}

async function reload() {
  loading.value = true;
  try {
    const payload = (await props.bridge.get("webui/keywords")) as {
      revision?: string;
      settings?: Record<string, unknown>;
      bindings?: Record<string, unknown>[];
    } | null;
    if (!payload || destroyed.value) return;
    revision.value = String(payload.revision || "");
    const next = payload.settings || {};
    settings.mode = String(next.mode || "command");
    settings.wrapper_left = String(next.wrapper_left || "");
    settings.wrapper_right = String(next.wrapper_right || "");
    settings.max_refs = Number(next.max_refs ?? 3);
    bindings.value = (payload.bindings || []).map((entry) => toForm(entry));
    previewResult.value = null;
  } catch (err) {
    console.error("[KeywordsView] 加载关键词配置失败:", err);
    props.errorFeedback.show(err, props.t("加载关键词配置失败"));
  } finally {
    loading.value = false;
  }
}

function addBinding() {
  bindings.value.unshift({
    name: "",
    wordsText: "",
    ref_ids: [],
    ref_files: [],
    ref_addresses: [],
    files: [],
    addresses: [],
    addressInput: "",
    prompt_append: "",
    enabled: true,
  });
}

function removeBinding(index: number) {
  bindings.value.splice(index, 1);
}

async function pickReferences(binding: Binding) {
  let entries: { id: string; label: string }[] = [];
  try {
    const payload = (await props.bridge.get("webui/references")) as {
      references?: { id: string; label: string }[];
    } | null;
    entries = Array.isArray(payload?.references) ? payload!.references : [];
  } catch (err) {
    console.error("[KeywordsView] 读取参考图库失败:", err);
    props.errorFeedback.show(err, props.t("读取参考图库失败"));
    return;
  }
  if (!entries.length) {
    props.toast.info(props.t("参考图库为空，请先上传参考图"));
    return;
  }
  const selected = new Set(binding.ref_ids);
  props.modal.openCustom({
    title: props.t("选择参考图（可多选）"),
    renderBody: (container: HTMLElement) => {
      container.style.maxHeight = "360px";
      container.style.overflowY = "auto";
      for (const entry of entries) {
        const row = document.createElement("label");
        row.style.display = "flex";
        row.style.alignItems = "center";
        row.style.gap = "8px";
        row.style.padding = "4px 0";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = selected.has(entry.id);
        box.addEventListener("change", () => {
          if (box.checked) selected.add(entry.id);
          else selected.delete(entry.id);
        });
        row.appendChild(box);
        row.appendChild(document.createTextNode(entry.label || entry.id));
        container.appendChild(row);
      }
    },
    renderFooter: (footer: HTMLElement, confirm: () => void, cancel: () => void) => {
      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "comic-btn comic-btn--cta";
      okBtn.textContent = props.t("确认");
      okBtn.addEventListener("click", () => {
        binding.ref_ids = [...selected];
        confirm();
      });
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "comic-btn";
      cancelBtn.textContent = props.t("取消");
      cancelBtn.addEventListener("click", cancel);
      footer.append(cancelBtn, okBtn);
    },
  });
}

function refLabel(binding: Binding): string {
  return binding.ref_ids.length ? binding.ref_ids.map((id) => id).join("、") : props.t("未选择");
}

async function uploadFile(binding: Binding, event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  try {
    const result = (await props.bridge.upload("webui/keywords/files", file)) as {
      path?: string;
    } | null;
    const path = String(result?.path || "");
    if (!path) return;
    if (!binding.ref_files.includes(path)) {
      binding.ref_files.push(path);
      binding.files.push({
        path,
        label: path.split("/").pop() || path,
        missing: false,
        gen: "",
      });
    }
    props.toast.success(props.t("上传成功，保存后生效"));
  } catch (err) {
    console.error("[KeywordsView] 上传绑定图片失败:", err);
    props.errorFeedback.show(err, props.t("上传绑定图片失败"));
  }
}

function removeFile(binding: Binding, path: string) {
  binding.ref_files = binding.ref_files.filter((item) => item !== path);
  binding.files = binding.files.filter((item) => item.path !== path);
}

function addAddress(binding: Binding) {
  const address = binding.addressInput.trim();
  if (!address) return;
  const isUrl = /^https?:\/\//i.test(address);
  const isPath =
    address.startsWith("/") ||
    /^[A-Za-z]:[\\/]/.test(address) ||
    address.toLowerCase().startsWith("file:");
  if (!isUrl && !isPath) {
    props.toast.warning(props.t("地址必须是 http(s) URL 或本地绝对路径"));
    return;
  }
  binding.addressInput = "";
  if (binding.ref_addresses.includes(address)) return;
  binding.ref_addresses.push(address);
  binding.addresses.push({
    address,
    kind: isUrl ? "url" : "path",
    missing: !isUrl,
    gen: "",
  });
}

function removeAddress(binding: Binding, address: string) {
  binding.ref_addresses = binding.ref_addresses.filter((item) => item !== address);
  binding.addresses = binding.addresses.filter((item) => item.address !== address);
}

async function copyGen(gen: string) {
  try {
    await navigator.clipboard.writeText(gen);
    props.toast.success(props.t("已复制 {v0}", { v0: gen }));
  } catch {
    props.toast.info(gen);
  }
}

async function save() {
  const cleaned = bindings.value.map((binding, index) => ({
    name: binding.name.trim() || props.t("绑定 {v0}", { v0: String(index + 1) }),
    words: binding.wordsText
      .split(/[,，]/)
      .map((word) => word.trim())
      .filter(Boolean),
    ref_ids: binding.ref_ids,
    ref_files: binding.ref_files,
    ref_addresses: binding.ref_addresses,
    prompt_append: binding.prompt_append.trim(),
    enabled: binding.enabled,
  }));
  if (cleaned.some((binding) => !binding.words.length)) {
    props.toast.warning(props.t("每条绑定至少需要一个触发关键词"));
    return;
  }
  saving.value = true;
  try {
    const payload = (await props.bridge.post("webui/keywords", {
      revision: revision.value,
      settings: { ...settings },
      bindings: cleaned,
    })) as {
      revision?: string;
      settings?: Record<string, unknown>;
      bindings?: Record<string, unknown>[];
    } | null;
    if (payload) {
      revision.value = String(payload.revision || "");
      const next = payload.settings || {};
      settings.mode = String(next.mode || "command");
      settings.wrapper_left = String(next.wrapper_left || "");
      settings.wrapper_right = String(next.wrapper_right || "");
      settings.max_refs = Number(next.max_refs ?? 3);
      bindings.value = (payload.bindings || []).map((entry) => toForm(entry));
    }
    props.toast.success(props.t("关键词配置已保存并即时生效"));
  } catch (err) {
    console.error("[KeywordsView] 保存关键词配置失败:", err);
    props.errorFeedback.show(err, props.t("保存关键词配置失败"));
    void reload();
  } finally {
    saving.value = false;
  }
}

async function runPreview() {
  const text = previewText.value.trim();
  if (!text) {
    props.toast.warning(props.t("请输入要预演的文本"));
    return;
  }
  previewing.value = true;
  try {
    previewResult.value = (await props.bridge.post("webui/keywords/preview", {
      text,
      manual_count: manualCount.value,
    })) as PreviewResult | null;
  } catch (err) {
    console.error("[KeywordsView] 关键词预演失败:", err);
    props.errorFeedback.show(err, props.t("关键词预演失败"));
  } finally {
    previewing.value = false;
  }
}

const modeHint = computed(() =>
  settings.mode === "auto"
    ? props.t("自动模式：生成请求文本按关键词匹配注入")
    : props.t("指令模式：仅 /快速 关键词 子指令触发"),
);

onMounted(() => {
  void reload();
});

defineExpose({ reload });
</script>

<template>
  <div class="keywords-view">
    <div class="comic-card keywords-header">
      <div class="keywords-title">
        <h2>{{ t("关键词触发") }}</h2>
        <p class="muted">{{ t("命中关键词时自动注入参考图库条目与提示词片段。") }}</p>
      </div>
      <div class="keywords-actions">
        <button type="button" class="comic-btn" :disabled="loading || unavailable()" @click="reload">
          {{ t("刷新") }}
        </button>
      </div>
    </div>

    <div class="comic-card keywords-settings">
      <h3>{{ t("触发设置") }}</h3>
      <div class="keywords-settings-grid">
        <label>
          <span class="muted">{{ t("触发模式") }}</span>
          <select v-model="settings.mode" class="form-input">
            <option value="command">{{ t("指令模式（/快速 关键词）") }}</option>
            <option value="auto">{{ t("自动模式") }}</option>
          </select>
        </label>
        <label>
          <span class="muted">{{ t("auto 包裹符（左）") }}</span>
          <input v-model="settings.wrapper_left" class="form-input" maxlength="2" />
        </label>
        <label>
          <span class="muted">{{ t("auto 包裹符（右）") }}</span>
          <input v-model="settings.wrapper_right" class="form-input" maxlength="2" />
        </label>
        <label>
          <span class="muted">{{ t("单次自动注入上限") }}</span>
          <input
            v-model.number="settings.max_refs"
            type="number"
            min="0"
            max="10"
            class="form-input"
          />
        </label>
      </div>
      <p class="muted">{{ modeHint }}</p>
    </div>

    <div class="comic-card keywords-bindings">
      <div class="keywords-bindings-head">
        <h3>{{ t("关键词绑定") }}</h3>
        <button type="button" class="comic-btn comic-btn--cta" @click="addBinding">
          {{ t("添加绑定") }}
        </button>
      </div>
      <div v-if="!bindings.length" class="references-empty">
        <p>{{ t("还没有绑定；添加一条后，命中关键词即可注入其参考图与提示词。") }}</p>
      </div>
      <div v-for="(binding, index) in bindings" :key="index" class="keywords-binding">
        <div class="keywords-binding-row">
          <input
            v-model="binding.name"
            class="form-input"
            maxlength="64"
            :placeholder="t('绑定名称')"
          />
          <input
            v-model="binding.wordsText"
            class="form-input"
            :placeholder="t('触发关键词，逗号分隔')"
          />
          <label class="keywords-enabled">
            <input v-model="binding.enabled" type="checkbox" />
            {{ t("启用") }}
          </label>
        </div>
        <div class="keywords-binding-row">
          <button type="button" class="comic-btn" @click="pickReferences(binding)">
            {{ t("选择参考图") }}
          </button>
          <span class="muted keywords-refids" :title="refLabel(binding)">
            {{ t("已选 {v0} 张", { v0: String(binding.ref_ids.length) }) }}
          </span>
          <input
            v-model="binding.prompt_append"
            class="form-input keywords-append"
            maxlength="500"
            :placeholder="t('追加提示词片段（可选）')"
          />
          <button type="button" class="comic-btn comic-btn--danger" @click="removeBinding(index)">
            {{ t("删除") }}
          </button>
        </div>
        <div class="keywords-binding-row">
          <label class="comic-btn keywords-upload">
            {{ t("上传图片") }}
            <input
              type="file"
              accept=".png,.jpg,.jpeg,.webp,.gif,.bmp"
              hidden
              @change="uploadFile(binding, $event)"
            />
          </label>
          <input
            v-model="binding.addressInput"
            class="form-input keywords-address"
            :placeholder="t('图片 URL 或本地绝对路径')"
            @keyup.enter="addAddress(binding)"
          />
          <button type="button" class="comic-btn" @click="addAddress(binding)">
            {{ t("添加地址") }}
          </button>
        </div>
        <div
          v-if="binding.files.length || binding.addresses.length"
          class="keywords-media-list"
        >
          <span v-for="file in binding.files" :key="file.path" class="keywords-media-chip">
            <span class="keywords-media-label" :title="file.path">{{ file.label }}</span>
            <span v-if="file.missing" class="keywords-missing">{{ t("缺失") }}</span>
            <code
              v-if="file.gen"
              class="keywords-gen"
              :title="t('点击复制 gen 引用句柄')"
              @click="copyGen(file.gen)"
              >{{ file.gen }}</code
            >
            <button
              type="button"
              class="keywords-chip-x"
              :aria-label="t('移除')"
              @click="removeFile(binding, file.path)"
            >
              ×
            </button>
          </span>
          <span
            v-for="addr in binding.addresses"
            :key="addr.address"
            class="keywords-media-chip"
          >
            <span class="keywords-media-label" :title="addr.address">{{ addr.address }}</span>
            <span class="keywords-kind">{{ addr.kind === "url" ? t("URL") : t("本地") }}</span>
            <span v-if="addr.missing" class="keywords-missing">{{ t("缺失") }}</span>
            <code
              v-if="addr.gen"
              class="keywords-gen"
              :title="t('点击复制 gen 引用句柄')"
              @click="copyGen(addr.gen)"
              >{{ addr.gen }}</code
            >
            <button
              type="button"
              class="keywords-chip-x"
              :aria-label="t('移除')"
              @click="removeAddress(binding, addr.address)"
            >
              ×
            </button>
          </span>
        </div>
      </div>
      <button
        type="button"
        class="comic-btn comic-btn--cta keywords-save"
        :disabled="saving || unavailable()"
        @click="save"
      >
        {{ saving ? t("保存中...") : t("保存并应用") }}
      </button>
    </div>

    <div class="comic-card keywords-preview">
      <h3>{{ t("触发预演") }}</h3>
      <div class="keywords-preview-form">
        <input
          v-model="previewText"
          class="form-input"
          :placeholder="t('输入一句话，看看会命中什么')"
          @keyup.enter="runPreview"
        />
        <label class="muted keywords-manual">
          {{ t("手动附图") }}
          <input v-model.number="manualCount" type="number" min="0" max="15" class="form-input" />
        </label>
        <button
          type="button"
          class="comic-btn comic-btn--cta"
          :disabled="previewing || unavailable()"
          @click="runPreview"
        >
          {{ previewing ? t("预演中...") : t("预演") }}
        </button>
      </div>
      <div v-if="previewResult" class="keywords-preview-result">
        <p v-if="!previewResult.hits.length" class="muted">
          {{ t("没有命中任何关键词绑定") }}
        </p>
        <template v-else>
          <p>
            {{ t("命中 {v0} 条绑定", { v0: String(previewResult.hits.length) }) }}
          </p>
          <ul class="keywords-hits">
            <li v-for="hit in previewResult.hits" :key="hit.name">
              {{ hit.name }}
              <span class="muted">[{{ hit.words.join("、") }}]</span>
            </li>
          </ul>
          <p>
            {{ t("注入 {v0} 张参考图", { v0: String(previewResult.refs.length) }) }}
            <template v-if="previewResult.dropped.length">
              ·
              {{
                t("丢弃 {v0} 张（超出上限）", {
                  v0: String(previewResult.dropped.length),
                })
              }}
            </template>
          </p>
          <ul class="keywords-refs">
            <li v-for="ref of previewResult.refs" :key="ref.id">
              {{ ref.label }}
              <span class="keywords-kind">{{
                ref.kind === "file" ? t("上传") : ref.kind === "address" ? t("地址") : t("图库")
              }}</span>
              <span v-if="ref.missing" class="keywords-missing">
                {{ t("缺失") }}
              </span>
            </li>
          </ul>
          <p v-if="previewResult.prompt_append" class="keywords-append-preview">
            {{ t("追加提示词：{v0}", { v0: previewResult.prompt_append }) }}
          </p>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.keywords-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.keywords-title h2 {
  margin: 0 0 4px;
}

.keywords-title p {
  margin: 0;
}

.keywords-settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}

.keywords-settings-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.keywords-bindings-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.keywords-binding {
  border-top: 1px solid var(--line, rgba(0, 0, 0, 0.1));
  padding: 10px 0;
}

.keywords-binding-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.keywords-binding-row .form-input {
  flex: 1 1 180px;
  min-width: 0;
}

.keywords-append {
  flex: 2 1 260px;
}

.keywords-refids {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.keywords-save {
  margin-top: 8px;
}

.keywords-preview-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.keywords-preview-form .form-input:first-child {
  flex: 2 1 260px;
}

.keywords-manual {
  display: flex;
  align-items: center;
  gap: 6px;
}

.keywords-manual .form-input {
  width: 72px;
}

.keywords-preview-result {
  margin-top: 10px;
}

.keywords-hits,
.keywords-refs {
  margin: 4px 0 8px;
  padding-left: 20px;
}

.keywords-missing {
  color: var(--danger, #c0392b);
  font-size: 12px;
}

.keywords-append-preview {
  white-space: pre-wrap;
}
.keywords-upload {
  cursor: pointer;
}

.keywords-address {
  flex: 2 1 260px;
}

.keywords-media-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 6px;
}

.keywords-media-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 8px;
  border: 1px solid var(--line, rgba(0, 0, 0, 0.15));
  border-radius: 999px;
  font-size: 12px;
}

.keywords-media-label {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.keywords-kind {
  color: var(--muted, #888);
}

.keywords-gen {
  cursor: pointer;
  font-size: 11px;
  color: var(--pop, #b8a85f);
}

.keywords-chip-x {
  border: none;
  background: none;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  color: var(--muted, #888);
  padding: 0 2px;
}

.keywords-chip-x:hover {
  color: var(--danger, #c0392b);
}
</style>
