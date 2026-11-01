<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import type { BridgeClientLike, ModalLike, Translator } from '../provider-config/types'

interface GalleryItem {
  job_id: string
  prompt?: string
  images?: unknown[]
  params?: Record<string, unknown>
  stats?: Record<string, unknown>
  caller?: Record<string, unknown>
  requester?: Record<string, unknown>
  source?: string
  callback_status?: string
  created_at?: string
  reference_names?: unknown[]
  duration_ms?: number
}

interface Props {
  store: { removeJobs: (ids: string[]) => void }
  bridge: BridgeClientLike
  modal: ModalLike
  t: Translator
  safeDom: {
    isSafeImageName: (name: unknown) => boolean
    sourceLabel: (source?: string, caller?: Record<string, unknown>) => string
    displayRoute: (record: Record<string, unknown>) => { provider: string; model: string }
    requesterLabels: (requester: Record<string, unknown> | undefined, source?: string) => string[]
    referenceStrip: (record: Record<string, unknown>, onSelect: (name: string) => void) => HTMLElement | null
  }
  icons: Record<string, string>
  toast: { success: (m: string) => void; warning: (m: string) => void; error: (m: string) => void }
  errorFeedback: { show: (err: unknown, fallback: string) => void }
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void }
  imageLoader: { attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void }
  lightbox: { open: (items: unknown[], index: number) => void }
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void
  onRefillRequest?: (prompt: string | undefined, params: unknown, record: GalleryItem) => void
  onSetReference?: (imageName: string) => void
}

const props = defineProps<Props>()

// ---------- 状态 ----------
const form = reactive({ keyword: '', source: '', plugin: '', group: '', user: '' })
const query = reactive({
  page: 1, size: 20, keyword: '', source: '', plugin_id: '', group_id: '', user_id: '',
})
const data = reactive({ items: [] as GalleryItem[], page: 1, size: 20, total: 0 })
const selected = ref(new Set<string>())
const loadingId = ref(0)
const maxSelection = 20

// ---------- 派生 ----------
const selectedCount = computed(() => selected.value.size)
const maxPage = computed(() => Math.max(1, Math.ceil(data.total / data.size)))
const paginationText = computed(() =>
  props.t('第 {v0} 页 / 共 {v1} 页 (总数 {v2})', { v0: data.page, v1: maxPage.value, v2: data.total })
)
const prevDisabled = computed(() => data.page <= 1)
const nextDisabled = computed(() => data.page >= maxPage.value)
const hasFilters = computed(() =>
  Object.entries(query).some(([key, value]) => !['page', 'size'].includes(key) && Boolean(value))
)
const emptyTitle = computed(() =>
  hasFilters.value ? props.t('未找到匹配的生成任务') : props.t('这里会收下你的每一次灵感')
)
const emptyDescription = computed(() =>
  hasFilters.value
    ? props.t('请调整筛选条件或点击重置后重试。')
    : props.t('还没有历史作品。在工作台生成第一幅图像，就可以在这里查看与管理。')
)
const allSelected = computed(
  () => data.items.length > 0 && data.items.every(item => selected.value.has(item.job_id))
)
const indeterminate = computed(
  () => !allSelected.value && data.items.some(item => selected.value.has(item.job_id))
)

// ---------- 数据拉取 ----------
async function fetchGallery() {
  const requestId = ++loadingId.value
  try {
    const payload = (await props.bridge.get('webui/history', {
      ...query,
    } as unknown as Record<string, string>)) as {
      items?: GalleryItem[]
      page?: number
      size?: number
      total?: number
    } | null
    if (requestId !== loadingId.value) return
    const items = Array.isArray(payload?.items) ? payload!.items : []
    const page = Number.isInteger(payload?.page) ? payload!.page! : 1
    const size = Number.isInteger(payload?.size) && payload!.size! > 0 ? payload!.size! : 20
    const total = Number.isInteger(payload?.total) && payload!.total! >= 0 ? payload!.total! : 0

    // 越界空页回退到最后一页，避免删除后停留在不存在的页。
    if (items.length === 0 && page > 1 && total > 0) {
      const lastPage = Math.max(1, Math.ceil(total / size))
      if (lastPage !== query.page) {
        query.page = lastPage
        await fetchGallery()
        return
      }
    }

    data.items = items
    data.page = page
    data.size = size
    data.total = total
    query.page = page
    void refreshDynamicDom()
  } catch (err) {
    if (requestId !== loadingId.value) return
    console.error('[GalleryView] 拉取画廊数据失败:', err)
    props.errorFeedback.show(err, props.t('拉取历史画廊数据失败'))
  }
}

// ---------- 动态 DOM 端口（缩略图 / 参考图条） ----------
function safeImages(item: GalleryItem): string[] {
  return Array.isArray(item.images)
    ? item.images.filter(
      (name): name is string => typeof name === 'string' && props.safeDom.isSafeImageName(name)
    )
    : []
}

async function refreshDynamicDom() {
  await nextTick()
  // 缩略图：attach 懒加载与兜底，点击打开灯箱
  for (const img of Array.from(
    document.querySelectorAll<HTMLImageElement>('[data-gallery-thumb]')
  )) {
    const name = img.dataset.galleryThumb as string
    if (img.dataset.attached) continue
    img.dataset.attached = '1'
    props.installImageFallback(img, img.parentElement as HTMLElement, props.t('图片加载失败或已清理'))
    props.imageLoader.attach(img, name, { thumb: true })
    img.parentElement?.addEventListener('click', () => {
      const item = data.items.find(entry => entry.job_id === img.dataset.jobId)
      if (!item) return
      const previewItems = safeImages(item).map(imageName => ({
        imageName,
        prompt: item.prompt,
        ...props.safeDom.displayRoute(item as unknown as Record<string, unknown>),
        resolution: item.params?.resolution,
        aspect_ratio: item.params?.aspect_ratio,
        duration_ms: item.duration_ms,
        source: item.source,
        caller: item.caller,
        requester: item.requester,
      }))
      props.lightbox.open(previewItems, 0)
    })
  }
  // 参考图条：SafeDOM 命令式构建（内部复用 ImageLoader/Toast 基建）
  for (const host of Array.from(
    document.querySelectorAll<HTMLElement>('[data-ref-strip]')
  )) {
    if (host.dataset.attached) continue
    host.dataset.attached = '1'
    const item = data.items.find(entry => entry.job_id === host.dataset.refStrip)
    if (!item) continue
    const node = props.safeDom.referenceStrip(
      item as unknown as Record<string, unknown>,
      name => props.onSetReference?.(name)
    )
    if (node) host.appendChild(node)
  }
}

watch(() => data.items, () => void refreshDynamicDom())

// ---------- 选择 ----------
function touchSelection() {
  selected.value = new Set(selected.value)
}

function toggleAll(event: Event) {
  const isChecked = (event.target as HTMLInputElement).checked
  const items = data.items
  if (isChecked) {
    let reachedLimit = false
    for (const item of items) {
      if (selected.value.has(item.job_id)) continue
      if (selected.value.size >= maxSelection) {
        reachedLimit = true
        break
      }
      selected.value.add(item.job_id)
    }
    if (reachedLimit) {
      props.toast.warning(props.t('单次最多选择 {v0} 个历史任务', { v0: maxSelection }))
    }
  } else {
    items.forEach(item => selected.value.delete(item.job_id))
  }
  touchSelection()
}

function toggleCard(item: GalleryItem, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  if (checked) {
    if (selected.value.size >= maxSelection && !selected.value.has(item.job_id)) {
      ;(event.target as HTMLInputElement).checked = false
      props.toast.warning(props.t('单次最多选择 {v0} 个历史任务', { v0: maxSelection }))
      return
    }
    selected.value.add(item.job_id)
  } else {
    selected.value.delete(item.job_id)
  }
  touchSelection()
}

// ---------- 筛选 ----------
function search() {
  query.keyword = form.keyword.trim()
  query.source = form.source
  query.plugin_id = form.plugin.trim()
  query.group_id = form.group.trim()
  query.user_id = form.user.trim()
  query.page = 1
  void fetchGallery()
}

function resetFilters() {
  form.keyword = ''
  form.source = ''
  form.plugin = ''
  form.group = ''
  form.user = ''
  query.keyword = ''
  query.source = ''
  query.plugin_id = ''
  query.group_id = ''
  query.user_id = ''
  query.page = 1
  void fetchGallery()
}

function pagePrev() {
  if (query.page > 1) {
    query.page--
    void fetchGallery()
  }
}

function pageNext() {
  if (query.page < maxPage.value) {
    query.page++
    void fetchGallery()
  }
}

// ---------- 卡片动作 ----------
async function download(item: GalleryItem, name: string) {
  try {
    await (props.bridge as unknown as {
      download: (path: string, params: Record<string, string>, filename: string) => Promise<void>
    }).download(`webui/image/${encodeURIComponent(name)}`, { download: '1' }, name)
    props.toast.success(props.t('已发起图片下载'))
  } catch (err) {
    props.errorFeedback.show(err, props.t('下载失败'))
  }
}

async function copyPrompt(item: GalleryItem) {
  await props.clipboard.copy(item.prompt || '', props.t('提示词已复制到剪贴板'))
}

function reuse(item: GalleryItem) {
  props.onRefillRequest?.(item.prompt, item.params, item)
}

async function removeOne(item: GalleryItem) {
  const ok = await props.modal.confirm({
    title: props.t('确认删除该任务记录？'),
    content: props.t('删除后将彻底清理服务器磁盘中的原始图片（Job: {v0}），不可恢复。', {
      v0: item.job_id,
    }),
    confirmText: props.t('确认删除'),
    danger: true,
  })
  if (ok) await deleteJobs([item.job_id])
}

async function handleBatchDelete() {
  const count = selected.value.size
  if (count === 0) return
  const ok = await props.modal.confirm({
    title: props.t('批量删除确认'),
    content: props.t('您已选中 {v0} 个历史任务，删除将彻底清理服务器磁盘中的原始图片文件，且不可恢复。确认继续？', {
      v0: count,
    }),
    confirmText: props.t('确认删除'),
    danger: true,
  })
  if (ok) await deleteJobs([...selected.value])
}

async function deleteJobs(jobIds: string[]) {
  const normalizedJobIds = [
    ...new Set((Array.isArray(jobIds) ? jobIds : []).filter(id => typeof id === 'string' && id.trim())),
  ]
  if (normalizedJobIds.length === 0) return
  if (normalizedJobIds.length > maxSelection) {
    props.toast.error(props.t('单次最多删除 {v0} 个历史任务', { v0: maxSelection }))
    return
  }
  try {
    const res = (await props.bridge.post('webui/history/delete', {
      job_ids: normalizedJobIds,
    })) as { deleted?: string[]; failed?: { job_id?: string; error?: string }[] } | null
    const deleted = Array.isArray(res?.deleted) ? res!.deleted! : []
    const failed = Array.isArray(res?.failed) ? res!.failed! : []

    if (deleted.length > 0) {
      props.toast.success(props.t('成功删除 {v0} 个历史任务', { v0: deleted.length }))
    }
    if (failed.length > 0) {
      const details = failed.map(entry =>
        props.t('{v0}({v1})', {
          v0: entry?.job_id || props.t('未知任务'),
          v1: entry?.error || props.t('未知错误'),
        })
      )
      props.toast.warning(props.t('部分删除失败: {v0}', { v0: details.join(', ') }))
    }

    for (const id of deleted) selected.value.delete(id)
    touchSelection()
    props.store.removeJobs(deleted)
    await fetchGallery()
  } catch (err) {
    console.error('[GalleryView] 删除任务失败:', err)
    props.errorFeedback.show(err, props.t('删除失败'))
  }
}

// ---------- 卡片元信息（纯文案，复刻 SafeDOM 逻辑） ----------
function requesterPills(item: GalleryItem): string[] {
  const labels = [...props.safeDom.requesterLabels(item.requester, item.source)]
  if (item.source === 'plugin' && item.caller?.plugin_id) {
    labels.unshift(props.t('插件 ID: {v0}', { v0: (item.caller as Record<string, unknown>).plugin_id }))
  }
  const callbackLabels: Record<string, string> = {
    pending: props.t('待通知'),
    running: props.t('通知中'),
    removed: props.t('已取消通知'),
    succeeded: props.t('已通知'),
    failed: props.t('通知失败'),
    interrupted: props.t('通知已中断'),
  }
  const callbackLabel = item.callback_status ? callbackLabels[item.callback_status] : undefined
  if (callbackLabel) labels.push(callbackLabel)
  return labels
}

function routePills(item: GalleryItem): string[] {
  const route = props.safeDom.displayRoute(item as unknown as Record<string, unknown>)
  const labels: string[] = []
  if (route.provider) labels.push(props.t('供应商: {v0}', { v0: route.provider }))
  if (route.model) labels.push(props.t('模型: {v0}', { v0: route.model }))
  return labels
}

defineExpose({ fetchGallery })
</script>

<template>
  <div>
    <div class="gallery-filters comic-card">
      <div class="filter-row">
        <div class="filter-item filter-item--grow">
          <label for="gallery-search-input" class="form-label">{{ t('搜索作品') }}</label>
          <input
            v-model="form.keyword"
            type="text"
            id="gallery-search-input"
            @keydown.enter="search"
            class="comic-input"
            maxlength="100"
            :placeholder="t('提示词、任务名称、用户或错误信息')"
            :aria-label="t('搜索历史任务关键词')"
          >
        </div>
        <div class="filter-item">
          <label for="gallery-source-select" class="form-label">{{ t('任务来源') }}</label>
          <select
            v-model="form.source"
            id="gallery-source-select"
            class="comic-select"
            :aria-label="t('按任务来源筛选')"
          >
            <option value="">{{ t('全部来源') }}</option>
            <option value="webui">{{ t('工作台 (webui)') }}</option>
            <option value="command">{{ t('指令触发 (command)') }}</option>
            <option value="llm_tool">{{ t('LLM 工具 (llm_tool)') }}</option>
            <option value="llm_batch">{{ t('LLM 批量 (llm_batch)') }}</option>
            <option value="plugin">{{ t('插件调用 (plugin)') }}</option>
            <option value="legacy">{{ t('历史图片 (legacy)') }}</option>
          </select>
        </div>
        <div class="filter-item">
          <label for="gallery-plugin-input" class="form-label">{{ t('插件标识') }}</label>
          <input
            v-model="form.plugin"
            type="text"
            id="gallery-plugin-input"
            class="comic-input"
            maxlength="128"
            :placeholder="t('全部插件')"
            :aria-label="t('按插件标识筛选')"
          >
        </div>
        <div class="filter-item">
          <label for="gallery-group-input" class="form-label">{{ t('群组 ID') }}</label>
          <input
            v-model="form.group"
            type="text"
            id="gallery-group-input"
            @keydown.enter="search"
            class="comic-input"
            maxlength="128"
            :placeholder="t('全部群组')"
            :aria-label="t('按群组 ID 筛选')"
          >
        </div>
        <div class="filter-item">
          <label for="gallery-user-input" class="form-label">{{ t('用户 ID') }}</label>
          <input
            v-model="form.user"
            type="text"
            id="gallery-user-input"
            @keydown.enter="search"
            class="comic-input"
            maxlength="128"
            :placeholder="t('全部用户')"
            :aria-label="t('按用户 ID 筛选')"
          >
        </div>
        <button type="button" id="btn-search-gallery" class="comic-btn" @click="search">
          <span class="btn-icon" aria-hidden="true" v-html="icons.search"></span>
          <span>{{ t('检索') }}</span>
        </button>
        <button type="button" id="btn-reset-gallery" class="comic-btn comic-btn--outline" @click="resetFilters">
          <span>{{ t('重置') }}</span>
        </button>
      </div>

      <div class="gallery-batch-bar" id="gallery-batch-bar">
        <div class="batch-select-info">
          <input
            type="checkbox"
            id="gallery-select-all"
            class="comic-checkbox"
            :checked="allSelected"
            :indeterminate="indeterminate"
            @change="toggleAll"
          >
          <label for="gallery-select-all">
            <span>{{ t('全选本页 (') }}</span>{{ selectedCount }} <span>{{ t('项)') }}</span>
          </label>
        </div>
        <div class="batch-actions">
          <button
            type="button"
            id="btn-batch-delete"
            class="comic-btn comic-btn--danger comic-btn--sm"
            :disabled="selectedCount === 0"
            @click="handleBatchDelete"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.trash"></span>
            <span>{{ t('批量删除') }}</span>
          </button>
        </div>
      </div>
    </div>

    <div id="gallery-grid" class="gallery-grid">
      <article v-for="item in data.items" :key="item.job_id" class="gallery-card" :data-job-id="item.job_id">
        <label class="gallery-card-select-wrap">
          <input
            type="checkbox"
            class="comic-checkbox gallery-card-checkbox"
            :aria-label="t('选择历史任务')"
            :checked="selected.has(item.job_id)"
            :data-job-id="item.job_id"
            @change="toggleCard(item, $event)"
          >
        </label>

        <button
          v-if="safeImages(item)[0]"
          type="button"
          class="gallery-card-img-wrap"
          :aria-label="t('查看大图')"
        >
          <img
            loading="lazy"
            :alt="t('画廊图片')"
            :data-gallery-thumb="safeImages(item)[0]!"
            :data-job-id="item.job_id"
          >
        </button>
        <div v-else class="gallery-card-img-wrap">
          <div class="comic-img-placeholder">
            <span>{{ t('暂无生成图片') }}</span>
          </div>
        </div>

        <div class="gallery-card-info">
          <div class="gallery-card-prompt">{{ item.prompt || t('（历史图片，无提示词记录）') }}</div>
          <div class="gallery-card-meta">
            <span>[{{ safeDom.sourceLabel(item.source, item.caller) }}]</span>
            <span>{{ item.created_at ? new Date(item.created_at).toLocaleDateString('zh-CN') : '' }}</span>
          </div>
          <div class="job-meta-row requester-meta">
            <span v-for="label in requesterPills(item)" :key="label" class="meta-pill">{{ label }}</span>
          </div>
          <div class="job-meta-row">
            <span v-for="label in routePills(item)" :key="label" class="meta-pill">{{ label }}</span>
          </div>
          <div :data-ref-strip="item.job_id"></div>
        </div>

        <div class="gallery-card-actions">
          <button
            v-if="safeImages(item)[0]"
            type="button"
            class="comic-btn comic-btn--outline"
            :title="t('下载原图')"
            :aria-label="t('下载原图')"
            @click.stop="download(item, safeImages(item)[0]!)"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.download"></span>
          </button>
          <button
            type="button"
            class="comic-btn comic-btn--outline"
            :title="t('复制提示词')"
            :aria-label="t('复制提示词')"
            @click.stop="copyPrompt(item)"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.copy"></span>
          </button>
          <button
            type="button"
            class="comic-btn comic-btn--outline"
            :title="item.prompt ? t('回填参数再次生成') : t('历史图片无提示词记录，无法回填')"
            :aria-label="t('再次生成')"
            :disabled="!item.prompt"
            @click.stop="reuse(item)"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.sparkle"></span>
          </button>
          <button
            v-if="safeImages(item)[0]"
            type="button"
            class="comic-btn comic-btn--outline"
            :title="t('设为工作台参考图')"
            :aria-label="t('设为工作台参考图')"
            @click.stop="onSetReference?.(safeImages(item)[0]!)"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.image"></span>
          </button>
          <button
            type="button"
            class="comic-btn comic-btn--danger"
            :title="t('删除记录与图片')"
            :aria-label="t('删除记录与图片')"
            @click.stop="removeOne(item)"
          >
            <span class="btn-icon" aria-hidden="true" v-html="icons.trash"></span>
          </button>
        </div>
      </article>
    </div>

    <div v-if="!data.items.length" class="comic-empty-state" id="empty-gallery-tip" style="display: flex;">
      <div class="empty-icon" aria-hidden="true" v-html="icons.inbox"></div>
      <h2 class="empty-title">{{ emptyTitle }}</h2>
      <p class="empty-desc">{{ emptyDescription }}</p>
    </div>

    <div class="gallery-pagination" id="gallery-pagination">
      <button
        type="button"
        id="btn-page-prev"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="prevDisabled"
        @click="pagePrev"
      >
        {{ t('上一页') }}
      </button>
      <span class="pagination-info" id="pagination-info" aria-live="polite">{{ paginationText }}</span>
      <button
        type="button"
        id="btn-page-next"
        class="comic-btn comic-btn--sm comic-btn--outline"
        :disabled="nextDisabled"
        @click="pageNext"
      >
        {{ t('下一页') }}
      </button>
    </div>
  </div>
</template>
