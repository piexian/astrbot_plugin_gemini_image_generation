import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import LimitsView from '../LimitsView.vue'

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`))

const rate = () => ({ enabled: false, period_seconds: 60, max_requests: 10 })
const rule = (extra: Record<string, unknown> = {}) => ({
  ...rate(),
  rule_name: '测试规则',
  umos: [],
  ...extra,
})
const response = (rules: Record<string, unknown>[] = []): Record<string, any> => ({
  revision: 'r1',
  limits: {
    group_limit_mode: 'none',
    group_limit_list: [],
    global_rate_limit: rate(),
    default_rate_limit: rate(),
    rate_limit_rules: rules,
    group_whitelist: ['must-not-post'],
    providers: ['must-not-post'],
  },
  migration: { pending: false, message: '', cooldown_until: 0 },
})

let server = response() as Record<string, any>
const calls: { method: string; route: string; body?: Record<string, unknown>; params?: unknown }[] = []
let confirms = 0
let confirmationResult = false

const bridge = {
  isAvailable: () => true,
  get: vi.fn(async (route: string, params?: unknown) => {
    calls.push({ method: 'get', route, params: params as Record<string, string> | undefined })
    if (route === 'webui/limits') return server
    return { sessions: [], total: 0, page: 1, page_size: 20, available: true, warning: '' }
  }),
  post: vi.fn(async (route: string, body?: unknown) => {
    calls.push({ method: 'post', route, body: body as Record<string, unknown> | undefined })
    return { ...server, revision: 'r2', limits: (body as Record<string, any> | undefined)?.limits }
  }),
}
const modal = {
  confirm: vi.fn(async () => {
    confirms++
    return confirmationResult
  }),
  openCustom: vi.fn(),
}

const q = (selector: string) => document.querySelector(selector) as HTMLElement | null
const action = (name: string, index = '') =>
  q(`[data-limits-action="${name}"][data-index="${index}"]`)
const field = (scope: string, name: string) =>
  q(`[data-scope="${scope}"][data-field="${name}"]`)
const setControl = (node: HTMLElement | null, value: string, type: 'input' | 'change' = 'input') => {
  expect(node).toBeTruthy()
  ;(node as HTMLInputElement).value = value
  node!.dispatchEvent(new Event(type, { bubbles: true }))
}
const posts = () => calls.filter(call => call.method === 'post')
const plain = (value: unknown) => JSON.parse(JSON.stringify(value))

function mountView() {
  return mount(LimitsView, {
    props: { bridge, modal, t },
    attachTo: document.body,
  })
}

async function open(wrapper: VueWrapper) {
  await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
  await flushPromises()
}

beforeEach(() => {
  document.body.innerHTML = ''
  calls.length = 0
  confirms = 0
  confirmationResult = false
  server = response()
  bridge.get.mockClear()
  bridge.post.mockClear()
  modal.confirm.mockClear()
})

describe('LimitsView（对应原 vm 子进程测试）', () => {
  it('停用旧群号规则后保存仍保留 group_ids', async () => {
    server = response([rule({ enabled: true, group_ids: ['legacy'] })])
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as { draft: unknown }

    // 勾选框点击 = 把服务端启用的规则切换为停用
    const enabled = field('0', 'enabled') as HTMLInputElement
    expect(enabled.checked).toBe(true)
    enabled.click()
    await flushPromises()

    ;(action('save') as HTMLElement).click()
    await flushPromises()

    expect(posts().length).toBe(1)
    const body = posts()[0]!.body as Record<string, any>
    expect(plain(body.limits.rate_limit_rules[0].group_ids)).toEqual(['legacy'])
    expect(q('#limits-editor')!.textContent).toMatch(/旧群号待迁移/)
    void vm
    wrapper.unmount()
  })

  it('三种模式共用同一份群号列表并与限流一起保存', async () => {
    server = response()
    server.limits.group_limit_mode = 'blacklist'
    server.limits.group_limit_list = ['123', 'hash-group']
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as {
      draft: { group_limit_mode: string; group_limit_list: string[] }
    }
    const mode = q('[data-access-field="group_limit_mode"]') as HTMLSelectElement
    const list = q('[data-access-field="group_limit_list"]') as HTMLTextAreaElement
    expect(mode.value).toBe('blacklist')
    expect(list.value).toBe('123\nhash-group')
    await flushPromises()
    expect(q('#limits-editor')!.textContent).toMatch(/黑名单群号/)

    for (const value of ['whitelist', 'none', 'blacklist']) {
      setControl(mode, value, 'change')
      await flushPromises()
      expect(plain(vm.draft.group_limit_list)).toEqual(['123', 'hash-group'])
      expect(list.value).toBe('123\nhash-group')
    }

    setControl(mode, 'whitelist', 'change')
    setControl(list, ' 456 \n456\n\nopaque-group')
    await flushPromises()
    expect(plain(vm.draft.group_limit_list)).toEqual(['456', 'opaque-group'])
    expect(q('#limits-editor')!.textContent).toMatch(/白名单群号/)
    expect(q('#limits-editor')!.textContent).toMatch(/仅允许名单中的群/)

    ;(action('save') as HTMLElement).click()
    await flushPromises()
    expect(posts().length).toBe(1)
    const body = posts()[0]!.body as Record<string, any>
    expect(body.limits.group_limit_mode).toBe('whitelist')
    expect(plain(body.limits.group_limit_list)).toEqual(['456', 'opaque-group'])
    expect(body.limits.global_rate_limit.period_seconds).toBe(60)
    expect(vm.draft.group_limit_mode).toBe('whitelist')
    wrapper.unmount()
  })

  it('空名单语义显式展示，旧后端字段缺失时拒绝加载', async () => {
    server = response()
    server.limits.group_limit_mode = 'whitelist'
    const wrapper = mountView()
    await open(wrapper)
    expect(q('#limits-editor')!.textContent).toMatch(/白名单为空.*所有群均可使用/)
    wrapper.unmount()

    delete (server.limits as Record<string, unknown>).group_limit_mode
    delete (server.limits as Record<string, unknown>).group_limit_list
    const old = mountView()
    await open(old)
    const vm = old.vm as unknown as { loaded: boolean; saveDisabled: boolean; draft: null }
    expect(vm.loaded).toBe(false)
    expect(vm.saveDisabled).toBe(true)
    expect(vm.draft).toBeNull()
    old.unmount()
  })

  it('群限制校验失败与 409 都保留草稿', async () => {
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as {
      draft: { group_limit_mode: string; group_limit_list: string[] }
    }
    const mode = q('[data-access-field="group_limit_mode"]') as HTMLSelectElement
    const list = q('[data-access-field="group_limit_list"]') as HTMLTextAreaElement

    setControl(mode, 'blacklist', 'change')
    setControl(list, Array.from({ length: 1001 }, (_, i) => String(i)).join('\n'))
    await flushPromises()
    ;(action('save') as HTMLElement).click()
    await flushPromises()
    expect(posts().length).toBe(0)
    expect(q('#limits-status')!.textContent).toMatch(/最多 1000/)

    setControl(list, '123')
    await flushPromises()
    bridge.post.mockRejectedValueOnce({ status: 409 })
    ;(action('save') as HTMLElement).click()
    await flushPromises()
    expect((action('save') as HTMLButtonElement).disabled).toBe(true)
    expect(vm.draft.group_limit_mode).toBe('blacklist')
    expect(plain(vm.draft.group_limit_list)).toEqual(['123'])
    wrapper.unmount()
  })
})
