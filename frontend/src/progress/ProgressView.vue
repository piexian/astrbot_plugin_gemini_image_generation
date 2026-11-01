<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { BridgeClientLike, Translator } from '../provider-config/types'

interface JobRecord {
  job_id: string
  parent_job_id?: string | null
  item_name?: string
  status?: string
  created_at?: string
  duration_ms?: number
  prompt?: string
  error?: string
  preview?: string
  images?: unknown[]
  source_urls?: unknown[]
  params?: Record<string, unknown>
  stats?: Record<string, unknown>
  caller?: Record<string, unknown>
  requester?: Record<string, unknown>
  source?: string
  requested_images?: number
  generated_images?: number
  [key: string]: unknown
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>

interface Props {
  store: {
    on: (event: string, handler: (...args: unknown[]) => void) => () => void
    applySnapshot: (records: unknown[]) => void
    getJobsList: () => JobRecord[]
    getRunningJobsCount: () => number
    getRecentJobsCount: () => number
  }
  bridge: BridgeClientLike
  t: Translator
  safeDom: {
    isSafeImageName: (name: unknown) => boolean
    sourceLabel: (source?: string, caller?: AnyRecord) => string
    displayRoute: (record: AnyRecord) => { provider: string; model: string }
    requesterLabels: (requester: AnyRecord | undefined, source?: string) => string[]
    referenceStrip: (record: AnyRecord, onSelect: (name: string) => void) => HTMLElement | null
    previewImage: (source: unknown) => HTMLElement | null
  }
  icons: Record<string, string>
  toast: { success: (m: string) => void; warning: (m: string) => void; error: (m: string) => void }
  errorFeedback: { show: (err: unknown, fallback: string) => void }
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void }
  imageLoader: { attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void }
  lightbox: { open: (items: unknown[], index: number) => void }
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void
  onRefillRequest?: (prompt: string | undefined, params: unknown, record: AnyRecord) => void
  onSetReference?: (imageName: string) => void
}

const props = defineProps<Props>()

// ---------- 状态 ----------
const version = ref(0)
const expanded = ref(new Set<string>())
const highlight = ref<string | null>(null)
let highlightTimer: ReturnType<typeof setTimeout> | null = null
let unsubscribe: (() => void) | null = null

// ---------- 派生（store 为普通对象，靠 version 触发重算） ----------
const runningCount = computed(() => {
  void version.value
  return props.store.getRunningJobsCount()
})
const recentCount = computed(() => {
  void version.value
  return props.store.getRecentJobsCount()
})

interface JobGroup {
  parent: JobRecord
  children: JobRecord[]
}

const groups = computed<JobGroup[]>(() => {
  void version.value
  const allJobs = props.store.getJobsList()
  const parents: JobRecord[] = []
  const childrenMap = new Map<string, JobRecord[]>()
  for (const job of allJobs) {
    if (job.parent_job_id) {
      const list = childrenMap.get(job.parent_job_id) || []
      list.push(job)
      childrenMap.set(job.parent_job_id, list)
    } else {
      parents.push(job)
    }
  }
  const result: JobGroup[] = parents.map(parent => ({
    parent,
    children: childrenMap.get(parent.job_id) || [],
  }))
  // 容灾：父任务不在列表但子任务在的情况
  for (const [parentId, children] of childrenMap.entries()) {
    if (!parents.some(parent => parent.job_id === parentId)) {
      for (const child of children) result.push({ parent: child, children: [] })
    }
  }
  return result
})

const badgeText = computed(() => String(runningCount.value))

// ---------- 状态徽章 ----------
const STATUS_KEYS = ['running', 'succeeded', 'partial_success', 'failed', 'interrupted'] as const
const STATUS_TEXT: Record<string, string> = {
  queued: props.t('排队中'),
  running: props.t('进行中'),
  succeeded: props.t('已完成'),
  partial_success: props.t('部分成功'),
  failed: props.t('失败'),
  interrupted: props.t('已中断'),
}

function statusKey(status?: string): string {
  return (STATUS_KEYS as readonly string[]).includes(status as string)
    ? (status as string)
    : 'interrupted'
}

function statusText(status?: string): string {
  return STATUS_TEXT[status as string] || status || props.t('未知状态')
}

function durationText(record: JobRecord): string {
  if (['queued', 'running'].includes(record.status as string)) {
    const createdAt = new Date(record.created_at || '').getTime()
    const diff = Number.isFinite(createdAt)
      ? Math.max(0, Math.floor((Date.now() - createdAt) / 1000))
      : 0
    const mins = String(Math.floor(diff / 60)).padStart(2, '0')
    const secs = String(diff % 60).padStart(2, '0')
    return `${mins}:${secs}`
  }
  if (record.duration_ms) return `${(record.duration_ms / 1000).toFixed(1)}s`
  return '00:00'
}

// ---------- 卡片纯文案 ----------
function routePills(record: JobRecord): string[] {
  const merged = {
    ...record.params,
    ...(props.safeDom.displayRoute(record as AnyRecord) as Record<string, unknown>),
  } as Record<string, unknown>
  const labels: string[] = []
  if (merged.provider) labels.push(props.t('供应商: {v0}', { v0: merged.provider }))
  if (merged.model) labels.push(props.t('模型: {v0}', { v0: merged.model }))
  if (merged.resolution) labels.push(props.t('分辨率: {v0}', { v0: merged.resolution }))
  if (merged.aspect_ratio) labels.push(props.t('比例: {v0}', { v0: merged.aspect_ratio }))
  labels.push(
    props.t('张数: {v0} / {v1}', {
      v0: record.generated_images || 0,
      v1: record.requested_images || 1,
    })
  )
  return labels
}

function requesterPills(record: JobRecord): string[] {
  const labels = [...props.safeDom.requesterLabels(record.requester, record.source)]
  if (record.source === 'plugin' && record.caller?.plugin_id) {
    labels.unshift(props.t('插件 ID: {v0}', { v0: (record.caller as AnyRecord).plugin_id }))
  }
  const callbackLabels: Record<string, string> = {
    pending: props.t('待通知'),
    running: props.t('通知中'),
    removed: props.t('已取消通知'),
    succeeded: props.t('已通知'),
    failed: props.t('通知失败'),
    interrupted: props.t('通知已中断'),
  }
  const callbackLabel = record.callback_status
    ? callbackLabels[record.callback_status as string]
    : undefined
  if (callbackLabel) labels.push(callbackLabel)
  return labels
}

function safeImages(record: JobRecord): string[] {
  return Array.isArray(record.images)
    ? record.images.filter(
      (name): name is string => typeof name === 'string' && props.safeDom.isSafeImageName(name)
    )
    : []
}

function sourceUrls(record: JobRecord): string[] {
  return Array.isArray(record.source_urls)
    ? record.source_urls.filter(
      (url): url is string => typeof url === 'string' && /^https?:\/\//i.test(url)
    )
    : []
}

function batchStats(group: JobGroup) {
  const totalRequested = group.children.reduce((acc, child) => acc + (child.requested_images || 1), 0)
  const totalGenerated = group.children.reduce((acc, child) => acc + (child.generated_images || 0), 0)
  const percent =
    totalRequested > 0 ? Math.min(100, Math.round((totalGenerated / totalRequested) * 100)) : 0
  return { totalRequested, totalGenerated, percent }
}

// ---------- 动作 ----------
function toggleDrawer(parentId: string) {
  if (expanded.value.has(parentId)) expanded.value.delete(parentId)
  else expanded.value.add(parentId)
  expanded.value = new Set(expanded.value)
}

function copyJobId(jobId: string) {
  void props.clipboard.copy(jobId, props.t('已复制 Job ID: {v0}', { v0: jobId }))
}

function copySourceUrl(url: string) {
  void props.clipboard.copy(url, props.t('源链接已复制'))
}

function openLightbox(record: JobRecord, images: string[], index: number) {
  const route = props.safeDom.displayRoute(record as AnyRecord)
  const previewItems = images.map(imageName => ({
    imageName,
    prompt: record.prompt,
    provider: route.provider,
    model: route.model,
    resolution: record.params?.resolution,
    aspect_ratio: record.params?.aspect_ratio,
    duration_ms: record.duration_ms,
    source: record.source,
    caller: record.caller,
    requester: record.requester,
  }))
  props.lightbox.open(previewItems, index)
}

async function refreshJobs() {
  try {
    const records = await props.bridge.get('webui/jobs')
    if (Array.isArray(records)) {
      props.store.applySnapshot(records)
      props.toast.success(props.t('任务状态已刷新'))
    }
  } catch (err) {
    props.errorFeedback.show(err, props.t('刷新任务状态失败'))
  }
}

// ---------- 动态 DOM 端口（缩略图 / 参考图条 / SSE 预览） ----------
async function refreshDynamicDom() {
  await nextTick()
  for (const img of Array.from(
    document.querySelectorAll<HTMLImageElement>('[data-job-thumb]')
  )) {
    if (img.dataset.attached) continue
    img.dataset.attached = '1'
    const name = img.dataset.jobThumb as string
    props.installImageFallback(img, img.parentElement as HTMLElement, props.t('图片加载失败或已清理'))
    props.imageLoader.attach(img, name, { thumb: true })
    img.parentElement?.addEventListener('click', () => {
      const jobId = img.dataset.jobId
      const record = props.store.getJobsList().find(entry => entry.job_id === jobId)
      if (!record) return
      const images = safeImages(record)
      openLightbox(record, images, images.indexOf(name))
    })
  }
  for (const host of Array.from(document.querySelectorAll<HTMLElement>('[data-ref-strip]'))) {
    if (host.dataset.attached) continue
    host.dataset.attached = '1'
    const jobId = host.dataset.refStrip
    const record = props.store.getJobsList().find(entry => entry.job_id === jobId)
    if (!record) continue
    const node = props.safeDom.referenceStrip(
      record as AnyRecord,
      name => props.onSetReference?.(name)
    )
    if (node) host.appendChild(node)
  }
  for (const host of Array.from(document.querySelectorAll<HTMLElement>('[data-job-preview]'))) {
    if (host.dataset.attached) continue
    const jobId = host.dataset.jobPreview
    const record = props.store.getJobsList().find(entry => entry.job_id === jobId)
    const preview = record ? props.safeDom.previewImage(record.preview) : null
    if (preview) {
      host.dataset.attached = '1'
      host.appendChild(preview)
    }
  }
}

watch([version, groups], () => void refreshDynamicDom(), { flush: 'post' })

// 高亮滚动：与 vanilla 相同的 2.5s 清除节奏
watch(version, async () => {
  if (!highlight.value) return
  await nextTick()
  const el = document.getElementById(`job-${highlight.value}`)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if (highlightTimer) clearTimeout(highlightTimer)
  const highlightedJobId = highlight.value
  highlightTimer = setTimeout(() => {
    highlight.value = null
    highlightTimer = null
    void highlightedJobId
  }, 2500)
})

// 页签徽章仍由宿主导航承载，保持命令式更新
watch(badgeText, text => {
  const badge = document.getElementById('active-jobs-count') as HTMLElement | null
  if (!badge) return
  if (runningCount.value > 0) {
    badge.style.display = 'inline-block'
    badge.textContent = text
  } else {
    badge.style.display = 'none'
  }
}, { immediate: true })

onMounted(() => {
  unsubscribe = props.store.on('jobs_updated', () => {
    version.value++
  })
  version.value++
})

onBeforeUnmount(() => {
  unsubscribe?.()
  unsubscribe = null
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = null
})

defineExpose({ highlightJob: (jobId: string) => { highlight.value = jobId; version.value++ } })
</script>

<template>
  <div>
    <div class="progress-toolbar">
      <div class="toolbar-stats">
        <span class="stat-item">
          <span>{{ t('运行中 ') }}</span><strong>{{ runningCount }}</strong>
        </span>
        <span class="stat-item">
          <span>{{ t('近 24 小时 ') }}</span><strong>{{ recentCount }}</strong>
        </span>
      </div>
      <button
        type="button"
        id="btn-refresh-jobs"
        class="comic-btn comic-btn--sm comic-btn--outline"
        @click="refreshJobs"
      >
        <span class="btn-icon" aria-hidden="true" v-html="icons.refresh"></span>
        <span>{{ t('刷新任务状态') }}</span>
      </button>
    </div>

    <div id="jobs-stream-container" class="jobs-container">
      <div
        v-if="!groups.length"
        class="comic-empty-state"
        id="empty-jobs-tip"
        style="display: flex;"
      >
        <div class="empty-icon" aria-hidden="true" v-html="icons.inbox"></div>
        <h2 class="empty-title">{{ t('下一幅作品，从工作台开始') }}</h2>
        <p class="empty-desc">{{ t('还没有近期任务。发起生成后，可以在这里实时查看进度与结果。') }}</p>
      </div>

      <article
        v-for="group in groups"
        :key="group.parent.job_id"
        :id="`job-${group.parent.job_id}`"
        class="job-card"
        :class="{ 'job-card--highlight': highlight === group.parent.job_id }"
        :data-status="group.parent.status"
        :data-created-at="group.parent.created_at || ''"
      >
        <!-- 单任务卡片 -->
        <template v-if="!group.children.length">
          <div class="job-header">
            <div class="job-header-left">
              <span class="job-source-tag">{{ safeDom.sourceLabel(group.parent.source, group.parent.caller) }}</span>
              <button type="button" class="job-id-text" :aria-label="t('复制 Job ID')" @click="copyJobId(group.parent.job_id)">
                {{ group.parent.job_id }}
                <span class="btn-icon" aria-hidden="true" v-html="icons.copy"></span>
              </button>
            </div>
            <div class="job-header-right">
              <span :class="`status-pill status-pill--${statusKey(group.parent.status)}`">
                <span class="status-dot"></span>{{ statusText(group.parent.status) }}
              </span>
              <span class="job-duration">{{ durationText(group.parent) }}</span>
            </div>
          </div>
          <div class="job-meta-row requester-meta">
            <span v-for="label in requesterPills(group.parent)" :key="label" class="meta-pill">{{ label }}</span>
          </div>
          <div :data-ref-strip="group.parent.job_id"></div>
          <div v-if="group.parent.prompt" class="job-prompt-box">{{ group.parent.prompt }}</div>
          <div class="job-meta-row">
            <span v-for="label in routePills(group.parent)" :key="label" class="meta-pill">{{ label }}</span>
          </div>
          <div v-if="group.parent.status === 'running' && group.parent.preview" class="job-preview">
            <p>{{ t('生成预览 · 正在等待完整图片') }}</p>
            <div :data-job-preview="group.parent.job_id"></div>
          </div>
          <div v-if="sourceUrls(group.parent).length" class="job-source-urls">
            <div class="job-source-urls-title">{{ t('源图片链接') }}</div>
            <div v-for="(url, idx) in sourceUrls(group.parent)" :key="url" class="job-source-url-row">
              <code class="job-source-url" :title="url">{{ url }}</code>
              <button
                type="button"
                class="comic-btn comic-btn--sm comic-btn--outline"
                :aria-label="t('复制源链接 {v0}', { v0: idx + 1 })"
                @click="copySourceUrl(url)"
              >
                {{ t('复制') }}
              </button>
            </div>
          </div>
          <div v-if="safeImages(group.parent).length" class="job-thumbs-grid">
            <button
              v-for="(imgName, idx) in safeImages(group.parent)"
              :key="imgName"
              type="button"
              class="job-thumb-item"
              :aria-label="t('查看生成结果 {v0}', { v0: idx + 1 })"
            >
              <img loading="lazy" :alt="t('生成结果')" :data-job-thumb="imgName" :data-job-id="group.parent.job_id">
            </button>
          </div>
          <div v-if="group.parent.error" class="job-error-box">
            <div class="job-error-title">{{ t('执行失败详情') }}</div>
            <div>{{ group.parent.error }}</div>
          </div>
          <div class="job-actions-row">
            <button
              v-if="safeImages(group.parent).length"
              type="button"
              class="comic-btn comic-btn--sm comic-btn--outline"
              @click="onSetReference?.(safeImages(group.parent)[0]!)"
            >
              <span class="btn-icon" aria-hidden="true" v-html="icons.image"></span>{{ t('设为参考图') }}
            </button>
            <button
              type="button"
              class="comic-btn comic-btn--sm comic-btn--outline"
              @click="onRefillRequest?.(group.parent.prompt, group.parent.params, group.parent)"
            >
              <span class="btn-icon" aria-hidden="true" v-html="icons.sparkle"></span>{{ t('再次生成') }}
            </button>
          </div>
        </template>

        <!-- 批量聚合卡片 -->
        <template v-else>
          <div class="job-header">
            <div class="job-header-left">
              <span class="job-source-tag">{{ t('批量聚合任务') }}</span>
              <button type="button" class="job-id-text" :aria-label="t('复制 Job ID')" @click="copyJobId(group.parent.job_id)">
                {{ group.parent.job_id }}
                <span class="btn-icon" aria-hidden="true" v-html="icons.copy"></span>
              </button>
            </div>
            <div class="job-header-right">
              <span :class="`status-pill status-pill--${statusKey(group.parent.status)}`">
                <span class="status-dot"></span>{{ statusText(group.parent.status) }}
              </span>
              <span class="job-duration">{{ durationText(group.parent) }}</span>
            </div>
          </div>
          <div class="job-meta-row requester-meta">
            <span v-for="label in requesterPills(group.parent)" :key="label" class="meta-pill">{{ label }}</span>
          </div>

          <div
            class="progress-info-row"
            style="display: flex; justify-content: space-between; font-size: 12px; font-weight: bold;"
          >
            <span>{{ t('子任务项: {v0} 个', { v0: group.children.length }) }}</span>
            <span>{{ t('完成进度: {v0} / {v1} 张 ({v2}%)', {
              v0: batchStats(group).totalGenerated,
              v1: batchStats(group).totalRequested,
              v2: batchStats(group).percent,
            }) }}</span>
          </div>
          <div class="comic-progress-bar-wrap">
            <div class="comic-progress-bar-fill" :style="{ width: `${batchStats(group).percent}%` }"></div>
          </div>

          <div class="job-children-drawer">
            <button
              type="button"
              class="job-children-toggle"
              :aria-expanded="expanded.has(group.parent.job_id) ? 'true' : 'false'"
              :aria-controls="`children-${group.parent.job_id}`"
              @click="toggleDrawer(group.parent.job_id)"
            >
              <span>{{ t('查看 {v0} 个子任务详情', { v0: group.children.length }) }}</span>
              <span
                class="btn-icon"
                aria-hidden="true"
                v-html="expanded.has(group.parent.job_id) ? icons.chevronUp : icons.chevronDown"
              ></span>
            </button>
            <div
              v-show="expanded.has(group.parent.job_id)"
              :id="`children-${group.parent.job_id}`"
              class="job-children-list"
              style="display: flex;"
            >
              <div v-for="child in group.children" :key="child.job_id" class="job-child-item">
                <div class="job-child-header">
                  <strong>{{ child.item_name || child.job_id }}</strong>
                  <span :class="`status-pill status-pill--${statusKey(child.status)}`">
                    <span class="status-dot"></span>{{ statusText(child.status) }}
                  </span>
                </div>
                <div class="job-meta-row">
                  <span v-for="label in routePills(child)" :key="label" class="meta-pill">{{ label }}</span>
                </div>
                <div :data-ref-strip="child.job_id"></div>
                <div v-if="child.prompt" class="job-prompt-box">{{ child.prompt }}</div>
                <div v-if="safeImages(child).length" class="job-thumbs-grid">
                  <button
                    v-for="(imgName, idx) in safeImages(child)"
                    :key="imgName"
                    type="button"
                    class="job-thumb-item"
                    :aria-label="t('查看子任务结果 {v0}', { v0: idx + 1 })"
                  >
                    <img loading="lazy" :alt="t('子任务结果')" :data-job-thumb="imgName" :data-job-id="child.job_id">
                  </button>
                </div>
                <div v-if="child.error" class="job-error-box">{{ child.error }}</div>
                <div class="job-child-actions">
                  <button
                    v-if="safeImages(child).length"
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    @click="onSetReference?.(safeImages(child)[0]!)"
                  >
                    {{ t('设为参考图') }}
                  </button>
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    @click="onRefillRequest?.(child.prompt, child.params, child)"
                  >
                    {{ t('再次生成') }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </article>
    </div>
  </div>
</template>
