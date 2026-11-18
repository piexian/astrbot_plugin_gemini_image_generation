<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import type { BridgeClientLike, ModalLike, Translator } from "../provider-config/types";

interface ReferenceEntry {
  id: string;
  label: string;
  description: string;
  keywords: string[];
  file: string;
  source: string;
  created_at: string;
}

interface Props {
  bridge: {
    get: (path: string, params?: Record<string, string>) => Promise<unknown>;
    post: (path: string, body?: unknown) => Promise<unknown>;
    upload: (path: string, file: File) => Promise<unknown>;
    download: (
      path: string,
      params?: Record<string, string>,
      filename?: string,
    ) => Promise<unknown>;
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
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void };
  imageLoader: {
    attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void;
  };
  lightbox: { open: (items: unknown[], index: number) => void };
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void;
  onSetReference?: (imageName: string) => void;
}

const props = defineProps<Props>();

const t = (key: string, params?: Record<string, unknown>) => props.t(key, params);

// ---------- 状态 ----------
const entries = ref<ReferenceEntry[]>([]);
const loaded = ref(false);
const loading = ref(false);
const keyword = ref("");
const destroyed = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const isUploading = ref(false);

const form = reactive({
  editingId: "" as string,
  label: "",
  description: "",
  keywords: "",
});

const filtered = ref<ReferenceEntry[]>([]);

// ---------- 派生 ----------
const unavailable = () => !props.bridge.isAvailable?.();

function refreshFiltered() {
  const query = keyword.value.trim().toLowerCase();
  filtered.value = query
    ? entries.value.filter((entry) => {
        const haystack = [entry.label, entry.description, ...entry.keywords]
          .join("\n")
          .toLowerCase();
        return haystack.includes(query);
      })
    : [...entries.value];
}

watch(keyword, refreshFiltered);
watch(entries, refreshFiltered);

// ---------- 数据 ----------
async function loadOnce() {
  if (loaded.value || loading.value) return;
  loading.value = true;
  try {
    const payload = (await props.bridge.get("webui/references")) as {
      references?: ReferenceEntry[];
    } | null;
    entries.value = Array.isArray(payload?.references) ? payload!.references : [];
    loaded.value = true;
  } catch (err) {
    console.error("[ReferencesView] 加载参考图库失败:", err);
    props.errorFeedback.show(err, props.t("加载参考图库失败"));
  } finally {
    loading.value = false;
  }
}

async function reload() {
  loaded.value = false;
  await loadOnce();
}

// ---------- 缩略图 ----------
function refreshThumbs() {
  for (const img of Array.from(document.querySelectorAll<HTMLImageElement>("[data-ref-thumb]"))) {
    if (img.dataset.attached) continue;
    img.dataset.attached = "1";
    props.installImageFallback(img, img.parentElement as HTMLElement, props.t("图片加载失败"));
    props.imageLoader.attach(img, img.dataset.refThumb as string, { thumb: true });
  }
}

watch(filtered, () => void Promise.resolve().then(refreshThumbs), { flush: "post" });

// ---------- 上传 ----------
function pickFiles() {
  fileInput.value?.click();
}

async function onFilesChosen(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!files.length) return;
  isUploading.value = true;
  let saved = 0;
  try {
    for (const file of files) {
      if (
        file.type &&
        !["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"].includes(file.type)
      ) {
        props.toast.error(props.t("文件 {v0} 不是支持的图片格式", { v0: file.name }));
        continue;
      }
      try {
        // 先进上传暂存，再立刻存为永久参考（复用大小校验）
        const uploaded = (await props.bridge.upload("webui/upload", file)) as {
          names?: unknown[];
        } | null;
        const names = Array.isArray(uploaded?.names) ? (uploaded!.names as string[]) : [];
        if (!names.length) throw new Error(props.t("上传接口未返回有效图片文件名"));
        const entry = (await props.bridge.post("webui/references", {
          upload_name: names[0],
          label: file.name.replace(/\.[^.]+$/, "").slice(0, 64) || props.t("未命名参考"),
        })) as ReferenceEntry | null;
        if (entry?.id) {
          saved += 1;
          entries.value.unshift(entry);
        }
      } catch (err) {
        console.error("[ReferencesView] 保存参考图失败:", err);
        props.errorFeedback.show(err, props.t("保存参考图失败：{v0}", { v0: file.name }));
      }
    }
    if (saved > 0) {
      props.toast.success(props.t("已保存 {v0} 张参考图", { v0: String(saved) }));
      await reload();
    }
  } finally {
    isUploading.value = false;
  }
}

// ---------- 编辑 ----------
function startEdit(entry: ReferenceEntry) {
  form.editingId = entry.id;
  form.label = entry.label;
  form.description = entry.description;
  form.keywords = entry.keywords.join("，");
}

function cancelEdit() {
  form.editingId = "";
  form.label = "";
  form.description = "";
  form.keywords = "";
}

async function saveEdit() {
  if (!form.editingId) return;
  const label = form.label.trim();
  if (!label) {
    props.toast.warning(props.t("参考图名称不能为空"));
    return;
  }
  try {
    // 后端 update 走 PATCH 语义：POST /references 带 id 与待改字段
    const updated = (await props.bridge.post("webui/references", {
      id: form.editingId,
      label,
      description: form.description,
      keywords: form.keywords
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    })) as ReferenceEntry | null;
    if (updated?.id) {
      const index = entries.value.findIndex((item) => item.id === updated.id);
      if (index >= 0) entries.value[index] = updated;
      props.toast.success(props.t("参考图信息已更新"));
      cancelEdit();
    }
  } catch (err) {
    console.error("[ReferencesView] 更新参考图失败:", err);
    props.errorFeedback.show(err, props.t("更新参考图失败"));
  }
}

// ---------- 删除 ----------
async function removeEntry(entry: ReferenceEntry) {
  const ok = await props.modal.confirm({
    title: props.t("删除参考图"),
    content: props.t("确定删除「{v0}」吗？此操作不可恢复。", { v0: entry.label }),
    confirmText: props.t("删除"),
    danger: true,
  });
  if (!ok) return;
  try {
    await props.bridge.post("webui/references/delete", { id: entry.id });
    entries.value = entries.value.filter((item) => item.id !== entry.id);
    props.toast.success(props.t("参考图已删除"));
  } catch (err) {
    console.error("[ReferencesView] 删除参考图失败:", err);
    props.errorFeedback.show(err, props.t("删除参考图失败"));
  }
}

// ---------- 操作 ----------
function setAsReference(entry: ReferenceEntry) {
  props.onSetReference?.(entry.file);
}

function openLightbox(entry: ReferenceEntry) {
  props.lightbox.open([{ imageName: entry.file, prompt: entry.label }], 0);
}

async function download(entry: ReferenceEntry) {
  try {
    await props.bridge.download(
      `webui/references/image/${encodeURIComponent(entry.file)}`,
      { download: "1" },
      entry.file,
    );
  } catch (err) {
    console.error("[ReferencesView] 下载参考图失败:", err);
    props.errorFeedback.show(err, props.t("下载参考图失败"));
  }
}

function copyRefToken(entry: ReferenceEntry) {
  void props.clipboard.copy(`ref:${entry.id}`, props.t("已复制引用标识"));
}

onMounted(() => {
  void loadOnce();
});

onBeforeUnmount(() => {
  destroyed.value = true;
});

defineExpose({ loadOnce, reload });
</script>

<template>
  <div class="references-view">
    <div class="comic-card references-header">
      <div class="references-title">
        <h2>{{ t("参考图库") }}</h2>
        <p class="muted">
          {{ t("永久保存的参考图：不受画廊容量清理影响，可在生成时直接引用。") }}
        </p>
      </div>
      <div class="references-actions">
        <input
          ref="fileInput"
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
          multiple
          hidden
          @change="onFilesChosen"
        />
        <button
          type="button"
          class="btn btn-primary"
          :disabled="isUploading || unavailable()"
          @click="pickFiles"
        >
          {{ isUploading ? t("保存中...") : t("上传参考图") }}
        </button>
        <button type="button" class="btn" :disabled="loading || unavailable()" @click="reload">
          {{ t("刷新") }}
        </button>
      </div>
    </div>

    <div class="comic-card references-filter">
      <input
        v-model="keyword"
        type="search"
        class="form-input"
        :placeholder="t('按名称、描述或关键词搜索')"
        :aria-label="t('搜索参考图')"
      />
      <span class="muted references-count">
        {{ t("共 {v0} 张", { v0: String(filtered.length) }) }}
      </span>
    </div>

    <div v-if="loading && !loaded" class="references-empty">
      <p>{{ t("加载中...") }}</p>
    </div>
    <div v-else-if="!filtered.length" class="references-empty comic-card">
      <p>{{ t("还没有参考图。上传一张常用的人物、画风参考图，之后即可随时引用。") }}</p>
    </div>

    <div v-else class="references-grid">
      <div v-for="entry in filtered" :key="entry.id" class="comic-card references-card">
        <div class="references-thumb" @click="openLightbox(entry)">
          <img :alt="entry.label" :data-ref-thumb="entry.file" />
        </div>
        <div class="references-info">
          <template v-if="form.editingId === entry.id">
            <input
              v-model="form.label"
              type="text"
              class="form-input"
              maxlength="64"
              :placeholder="t('名称')"
              :aria-label="t('参考图名称')"
            />
            <input
              v-model="form.description"
              type="text"
              class="form-input"
              maxlength="500"
              :placeholder="t('描述（可选）')"
              :aria-label="t('参考图描述')"
            />
            <input
              v-model="form.keywords"
              type="text"
              class="form-input"
              :placeholder="t('关键词，逗号分隔（仅用于检索）')"
              :aria-label="t('参考图关键词')"
            />
            <div class="references-edit-actions">
              <button type="button" class="btn btn-primary" @click="saveEdit">
                {{ t("保存") }}
              </button>
              <button type="button" class="btn" @click="cancelEdit">
                {{ t("取消") }}
              </button>
            </div>
          </template>
          <template v-else>
            <div class="references-label" :title="entry.label">{{ entry.label }}</div>
            <div v-if="entry.description" class="muted references-desc">
              {{ entry.description }}
            </div>
            <div v-if="entry.keywords.length" class="references-keywords">
              <span v-for="word in entry.keywords" :key="word" class="references-keyword">
                {{ word }}
              </span>
            </div>
            <div class="references-meta muted">
              {{ entry.source === "gallery" ? t("来自画廊") : t("来自上传") }}
              · {{ entry.created_at }}
            </div>
            <div class="references-card-actions">
              <button
                type="button"
                class="btn"
                :title="t('加入工作台参考图托盘')"
                @click="setAsReference(entry)"
              >
                {{ t("用作参考") }}
              </button>
              <button
                type="button"
                class="btn"
                :title="t('复制 LLM 工具引用标识')"
                @click="copyRefToken(entry)"
              >
                {{ t("复制引用") }}
              </button>
              <button type="button" class="btn" @click="startEdit(entry)">
                {{ t("编辑") }}
              </button>
              <button type="button" class="btn" :title="t('下载原图')" @click="download(entry)">
                {{ t("下载") }}
              </button>
              <button
                type="button"
                class="btn btn-danger"
                :title="t('从参考图库删除')"
                @click="removeEntry(entry)"
              >
                {{ t("删除") }}
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.references-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.references-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.references-title h2 {
  margin: 0 0 4px;
}

.references-title p {
  margin: 0;
}

.references-actions {
  display: flex;
  gap: 8px;
}

.references-filter {
  display: flex;
  align-items: center;
  gap: 12px;
}

.references-filter .form-input {
  flex: 1;
}

.references-count {
  white-space: nowrap;
}

.references-empty {
  text-align: center;
  padding: 32px 16px;
}

.references-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}

.references-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.references-thumb {
  aspect-ratio: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface);
  cursor: pointer;
  overflow: hidden;
}

.references-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.references-info {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.references-label {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.references-desc {
  font-size: 0.85em;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.references-keywords {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.references-keyword {
  font-size: 0.75em;
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
}

.references-meta {
  font-size: 0.75em;
}

.references-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}

.references-card-actions .btn {
  padding: 3px 10px;
  font-size: 0.85em;
}

.references-edit-actions {
  display: flex;
  gap: 8px;
}
</style>
