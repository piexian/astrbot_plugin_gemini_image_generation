import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import WorkbenchView from '../WorkbenchView.vue'

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`))

const cap = (candidateId: string, provider = 'prov-a') => ({
  provider,
  provider_display: '供应商A',
  model: 'model-x',
  candidate_id: candidateId,
  resolutions: ['2K', '4K'],
  aspect_ratios: ['16:9'],
  generation_fields: {
    quality: { type: 'string', label: '质量', value: 'high', enum: ['high', 'low'] },
  },
  max_reference_images: 3,
  parameters: { image_count: { maximum: 4 } },
})

let capabilitiesPayload: Record<string, unknown> = {}
const posts: { route: string; body?: Record<string, unknown> }[] = []
const bridge = {
  get: vi.fn(async (route: string, _params?: unknown) => {
    if (route === 'webui/capabilities') return capabilitiesPayload
    if (route === 'webui/history') return { items: [] }
    return {}
  }),
  post: vi.fn(async (route: string, body?: unknown) => {
    posts.push({ route, body: body as Record<string, unknown> | undefined })
    return { job_id: 'job-1' }
  }),
  upload: vi.fn(async () => ({ names: [] })),
}
const toast = { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }
const errorFeedback = { show: vi.fn(), isAuth: () => false }
const clipboard = { copy: vi.fn() }
const imageLoader = { attach: vi.fn() }
const installImageFallback = vi.fn()
const modalBody = document.createElement('div')
const modalFooter = document.createElement('div')
const modal = {
  confirm: vi.fn(async () => true),
  openCustom: vi.fn((options: Record<string, unknown>) => {
    modal.options = options
    ;(options.renderBody as (b: HTMLElement) => void)?.(modalBody)
    ;(options.renderFooter as (f: HTMLElement, c: () => void, x: () => void) => void)?.(
      modalFooter,
      () => {},
      () => {}
    )
  }),
  close: vi.fn(),
  options: null as Record<string, unknown> | null,
}
const onJobSubmitted = vi.fn()

beforeEach(() => {
  document.body.innerHTML = ''
  posts.length = 0
  capabilitiesPayload = {
    models: [cap('cand-1'), cap('cand-2', 'prov-b')],
    limits: { batch_total_budget: 30, batch_max_tasks: 5, upload_max_mb: 8 },
  }
  localStorage.clear()
  toast.success.mockClear()
  toast.warning.mockClear()
  toast.error.mockClear()
  toast.info.mockClear()
  errorFeedback.show.mockClear()
  modalBody.replaceChildren()
  modalFooter.replaceChildren()
  modal.openCustom.mockClear()
  bridge.get.mockClear()
  bridge.post.mockClear()
  bridge.upload.mockClear()
  onJobSubmitted.mockClear()
})

const q = (selector: string) => document.querySelector(selector) as HTMLElement | null

function mountView() {
  return mount(WorkbenchView, {
    props: {
      bridge, modal, t,
      icons: { trash: '', x: '', check: '' },
      toast, errorFeedback, clipboard, imageLoader, installImageFallback,
      onJobSubmitted,
    },
    attachTo: document.body,
  })
}

describe('WorkbenchView', () => {
  it('加载 capabilities 后按供应商分组渲染模型并应用默认参数', async () => {
    const wrapper = mountView()
    const vm = wrapper.vm as unknown as { loadCapabilities: () => Promise<void> }
    await vm.loadCapabilities()
    await flushPromises()

    const select = document.getElementById('select-model') as HTMLSelectElement
    expect(select.options.length).toBe(2)
    expect(select.querySelector('optgroup')?.getAttribute('label')).toBe('供应商A')
    // 字段默认值进编辑器：张数上限来自 capability
    const count = document.getElementById('input-image-count') as HTMLInputElement
    expect(Number(count.max)).toBe(4)
    // 更多参数入口可见（generation_fields 含 quality）
    expect(document.getElementById('advanced-params-disclosure')!.hidden).toBe(false)
    // 与 vanilla 一致：初始加载不持久化选择（persistSelection: false），仅在用户操作后写入
    const stored = JSON.parse(localStorage.getItem('gemini-image-generation.studio.preferences.v1') || '{}')
    expect(stored.selected ?? null).toBeNull()
    wrapper.unmount()
  })

  it('缺少 limits 时展示横幅并禁用表单', async () => {
    capabilitiesPayload = { models: [cap('cand-1')] }
    const wrapper = mountView()
    await (wrapper.vm as unknown as { loadCapabilities: () => Promise<void> }).loadCapabilities()
    await flushPromises()

    const banner = document.querySelector('.comic-banner') as HTMLElement
    expect(banner).toBeTruthy()
    // 与 vanilla 一致：limits 缺失在控制台抛具体错误，横幅展示通用失败文案
    expect(banner.textContent).toContain('加载模型能力失败，请检查插件状态或网络连接')
    const submit = document.getElementById('btn-submit-generate') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    wrapper.unmount()
  })

  it('单任务提交 payload 携带模型与生成参数', async () => {
    const wrapper = mountView()
    await (wrapper.vm as unknown as { loadCapabilities: () => Promise<void> }).loadCapabilities()
    await flushPromises()

    const promptEl = document.getElementById('input-prompt') as HTMLTextAreaElement
    promptEl.value = '一只猫'
    promptEl.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    ;(document.getElementById('btn-submit-generate') as HTMLButtonElement).click()
    await flushPromises()

    expect(posts.length).toBe(1)
    const body = posts[0]!.body as Record<string, unknown>
    expect(body.provider).toBe('prov-a')
    expect(body.candidate_id).toBe('cand-1')
    expect(body.prompt).toBe('一只猫')
    expect(body.image_count).toBe(1)
    expect(onJobSubmitted).toHaveBeenCalledWith('job-1')
    wrapper.unmount()
  })

  it('批量模式自动补一个子任务，缺提示词阻断提交', async () => {
    const wrapper = mountView()
    await (wrapper.vm as unknown as { loadCapabilities: () => Promise<void> }).loadCapabilities()
    await flushPromises()

    ;(document.getElementById('mode-btn-batch') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.querySelectorAll('.batch-item-row').length).toBe(1)

    ;(document.getElementById('btn-submit-generate') as HTMLButtonElement).click()
    await flushPromises()
    expect(posts.length).toBe(0)
    expect(toast.warning).toHaveBeenCalledWith('子任务 "{v0}" 缺少提示词'.replace('{v0}', '子任务 1'))
    wrapper.unmount()
  })
})
