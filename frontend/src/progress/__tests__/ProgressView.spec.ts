import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ProgressView from '../ProgressView.vue'

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`))

const listeners = new Map<string, Set<(...args: unknown[]) => void>>()
const activeJobs = new Map<string, { job_id: string; [key: string]: unknown }>()

function seed(jobs: Record<string, unknown>[]) {
  activeJobs.clear()
  for (const job of jobs) activeJobs.set(job.job_id as string, job as { job_id: string })
}

const store = {
  on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
    const set = listeners.get(event) ?? new Set()
    set.add(handler)
    listeners.set(event, set)
    return () => set.delete(handler)
  }),
  applySnapshot: vi.fn(records => {
    activeJobs.clear()
    for (const rec of records as Record<string, unknown>[]) {
      activeJobs.set((rec as { job_id: string }).job_id, rec as { job_id: string })
    }
    emitJobs()
  }),
  getJobsList: () =>
    [...activeJobs.values()].sort((a, b) => {
      const aRunning = ['queued', 'running'].includes(a.status as string)
      const bRunning = ['queued', 'running'].includes(b.status as string)
      if (aRunning && !bRunning) return -1
      if (!aRunning && bRunning) return 1
      return 0
    }),
  getRunningJobsCount: () =>
    [...activeJobs.values()].filter(job => ['queued', 'running'].includes(job.status as string)).length,
  getRecentJobsCount: () => activeJobs.size,
}

function emitJobs() {
  for (const handler of listeners.get('jobs_updated') ?? []) handler()
}

const toast = { success: vi.fn(), warning: vi.fn(), error: vi.fn() }
const errorFeedback = { show: vi.fn() }
const clipboard = { copy: vi.fn() }
const imageLoader = { attach: vi.fn() }
const lightbox = { open: vi.fn() }
const installImageFallback = vi.fn()
const onRefillRequest = vi.fn()
const onSetReference = vi.fn()
const safeDom = {
  isSafeImageName: (name: unknown) => typeof name === 'string' && /\.(png|jpg)$/.test(name),
  sourceLabel: (source?: string) => source || '未知',
  displayRoute: () => ({ provider: 'prov', model: 'model-x' }),
  requesterLabels: () => [],
  referenceStrip: () => null,
  previewImage: () => null,
}
const modal = { confirm: vi.fn(async () => true), openCustom: vi.fn() }
const bridge = {
  isAvailable: () => true,
  get: vi.fn(async (route: string) => {
    if (route === 'webui/jobs') return [...activeJobs.values()]
    return {}
  }),
  post: vi.fn(async () => ({})),
}

const badge = { style: { display: '' }, textContent: '' }
beforeEach(() => {
  document.body.innerHTML = ''
  listeners.clear()
  seed([])
  toast.success.mockClear()
  errorFeedback.show.mockClear()
  clipboard.copy.mockClear()
  imageLoader.attach.mockClear()
  lightbox.open.mockClear()
  installImageFallback.mockClear()
  onRefillRequest.mockClear()
  onSetReference.mockClear()
  bridge.get.mockClear()
  // 页签徽章由宿主导航承载
  const el = document.createElement('span')
  el.id = 'active-jobs-count'
  Object.defineProperty(el, 'textContent', {
    get: () => badge.textContent,
    set: (value: string) => { badge.textContent = value },
  })
  el.style.display = 'none'
  document.body.appendChild(el)
})

const q = (selector: string) => document.querySelector(selector) as HTMLElement | null

function mountView() {
  return mount(ProgressView, {
    props: {
      store, bridge, t, safeDom,
      icons: { refresh: '', inbox: '', copy: '', image: '', sparkle: '', chevronDown: '', chevronUp: '' },
      toast, errorFeedback, clipboard, imageLoader, lightbox, installImageFallback,
      onRefillRequest, onSetReference,
    },
    attachTo: document.body,
  })
}

describe('ProgressView', () => {
  it('批量任务聚合为父卡片并计算进度条', async () => {
    seed([
      { job_id: 'parent', status: 'running', created_at: new Date().toISOString(), source: 'webui' },
      { job_id: 'c1', parent_job_id: 'parent', status: 'succeeded', requested_images: 2, generated_images: 2, item_name: '第一条' },
      { job_id: 'c2', parent_job_id: 'parent', status: 'running', requested_images: 2, generated_images: 1, item_name: '第二条' },
    ])
    const wrapper = mountView()
    await flushPromises()

    const cards = document.querySelectorAll('.job-card')
    expect(cards.length).toBe(1)
    expect(document.body.textContent).toContain('批量聚合任务')
    expect(document.body.textContent).toContain('子任务项: 2 个')
    expect(document.body.textContent).toContain('完成进度: 3 / 4 张 (75%)')
    const fill = document.querySelector('.comic-progress-bar-fill') as HTMLElement
    expect(fill.style.width).toBe('75%')
    // parent 与 c2 都在 running
    expect(badge.textContent).toBe('2')
    wrapper.unmount()
  })

  it('单任务卡渲染动作按钮并回填回调', async () => {
    seed([
      {
        job_id: 'single', status: 'succeeded', source: 'webui',
        prompt: '一只猫', params: { resolution: '2K', aspect_ratio: '16:9' },
        images: ['out-1.png', 'out-2.png'], duration_ms: 3400,
        requested_images: 2, generated_images: 2,
      },
    ])
    const wrapper = mountView()
    await flushPromises()

    expect(document.body.textContent).toContain('一只猫')
    expect(document.body.textContent).toContain('分辨率: 2K')
    expect(document.body.textContent).toContain('张数: 2 / 2')
    expect(imageLoader.attach).toHaveBeenCalledTimes(2)

    const card = document.querySelector('.job-card') as HTMLElement
    // 与 vanilla 一致：进度卡按钮是纯文案，无 aria-label
    const reference = (Array.from(card.querySelectorAll('button')).find(button =>
      button.textContent?.includes('设为参考图')
    ) ?? null) as HTMLButtonElement | null
    expect(reference).toBeTruthy()
    reference!.click()
    expect(onSetReference).toHaveBeenCalledWith('out-1.png')

    const reuse = (Array.from(card.querySelectorAll('button')).find(button =>
      button.textContent?.includes('再次生成')
    ) ?? null) as HTMLButtonElement | null
    expect(reuse).toBeTruthy()
    reuse!.click()
    expect(onRefillRequest).toHaveBeenCalledTimes(1)
    expect((onRefillRequest as any).mock.calls[0][2].job_id).toBe('single')

    const copyId = card.querySelector('[aria-label="复制 Job ID"]') as HTMLButtonElement
    copyId.click()
    expect(clipboard.copy).toHaveBeenCalledWith('single', '已复制 Job ID: {v0}'.replace('{v0}', 'single'))
    wrapper.unmount()
  })

  it('抽屉默认折叠，展开后可见子任务；刷新走 applySnapshot', async () => {
    seed([
      { job_id: 'p', status: 'running', source: 'webui' },
      { job_id: 'c1', parent_job_id: 'p', status: 'failed', error: '上游 500', item_name: '子项' },
    ])
    const wrapper = mountView()
    await flushPromises()

    const toggle = document.querySelector('.job-children-toggle') as HTMLButtonElement
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    // v-show 折叠 = display:none，hidden 属性不参与
    expect(q('.job-children-list')!.style.display).toBe('none')
    toggle.click()
    await flushPromises()
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(document.body.textContent).toContain('子项')
    expect(document.body.textContent).toContain('上游 500')

    bridge.get.mockClear()
    ;(document.querySelector('#btn-refresh-jobs') as HTMLButtonElement).click()
    await flushPromises()
    expect(bridge.get).toHaveBeenCalledWith('webui/jobs')
    expect(store.applySnapshot).toHaveBeenCalled()
    expect(toast.success).toHaveBeenCalledWith('任务状态已刷新')
    wrapper.unmount()
  })
})
