import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ProviderConfigView from '../ProviderConfigView.vue'
import type { ProviderSnapshot } from '../types'

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(params?.[k] ?? `{${k}}`))

// 与 Python vm 测试同一来源：真实插件 schema 的供应商模板。
const SCHEMA = JSON.parse(
  readFileSync(resolve(process.cwd(), '..', '_conf_schema.json'), 'utf-8')
) as {
  provider_settings: {
    items: {
      provider_overrides: { templates: Record<string, { description: string; items: unknown }> }
    }
  }
}
const TEMPLATES = Object.fromEntries(
  Object.entries(SCHEMA.provider_settings.items.provider_overrides.templates).map(
    ([name, template]) => [name, { label: template.description, fields: template.items }]
  )
)
const COMMON_FIELDS = Object.fromEntries(
  ['proxy', 'vision_provider_id', 'vision_model'].map(name => [
    name,
    (SCHEMA.provider_settings.items as Record<string, unknown>)[name],
  ])
)

function known(id = 'opaque-0', type = 'google', extra: Record<string, unknown> = {}) {
  return {
    id,
    api_type: type,
    supported: true,
    values: {
      enabled: true,
      model: 'model-free-form',
      priority: 0,
      api_keys: ['fake-old-one', 'fake-old-two'],
      api_base: 'https://example.invalid',
      ...extra,
    },
    secrets: { api_keys: { present: true, count: 2 } },
    unknown_fields: ['legacy_unknown'],
  }
}

function snapshot(): ProviderSnapshot {
  return {
    revision: 'r1',
    key_values_visible: true,
    model_catalog: { google: { supported: true }, doubao: { supported: false, message: '暂未接入' } },
    provider_polling: [],
    entries: [known(), known('opaque-1', 'doubao', { enabled: false })],
    common: { values: { proxy: '', vision_provider_id: '', vision_model: '' }, secrets: {} },
    templates: TEMPLATES as ProviderSnapshot['templates'],
    common_fields: COMMON_FIELDS as ProviderSnapshot['common_fields'],
    provider_types: Object.keys(TEMPLATES).map(id => ({ id, label: id })),
    vision_providers: [{ id: 'vision-1', label: 'Vision One' }],
    busy: false,
    requires_reload: false,
  }
}

let server = snapshot()
const calls: { method: string; route: string; payload?: Record<string, unknown> }[] = []
let confirmationResult = false
let confirms = 0
const saved = vi.fn()

let getImpl: (route: string) => Promise<unknown> = async () => server
let postImpl: (route: string, payload?: unknown) => Promise<unknown> =
  async (_route, rawPayload) => {
    const payload = rawPayload as Record<string, any> | undefined
    return {
      ...server,
      revision: 'r2',
      provider_polling: payload?.provider_polling,
      entries: ((payload?.entries ?? []) as Record<string, unknown>[]).map((entry, i) => ({
        ...entry,
        id: entry.id ?? `new-${i}`,
        supported: !!TEMPLATES[entry.api_type as string],
        secrets: {
          api_keys: {
            present: true,
            count:
              (entry.secret_actions as Record<string, { mode?: string; value?: string[] }> | undefined)
                ?.api_keys?.mode === 'replace'
                ? (entry.secret_actions as Record<string, { value?: string[] }>).api_keys!.value!.length
                : 2,
          },
        },
        unknown_fields: [],
      })),
      common: { values: payload?.common && (payload.common as Record<string, unknown>).values, secrets: {} },
    }
  }

const bridge = {
  isAvailable: () => true,
  get: vi.fn(async (route: string) => {
    calls.push({ method: 'get', route })
    return getImpl(route)
  }),
  post: vi.fn(async (route: string, payload?: unknown) => {
    calls.push({ method: 'post', route, payload: payload as Record<string, unknown> | undefined })
    return postImpl(route, payload)
  }),
}

const modalBody = document.createElement('div')
const modalFooter = document.createElement('div')
const modal = {
  confirm: vi.fn(async () => {
    confirms++
    return confirmationResult
  }),
  openCustom: vi.fn((options: {
    variant?: string
    renderBody?: (body: HTMLElement) => void
    renderFooter?: (footer: HTMLElement, confirm: () => void, cancel: () => void) => void
    onClose?: () => void
  }) => {
    expect(options.variant).toBe('parameters')
    if (modal.options) modal.close(false)
    modal.options = options
    options.renderBody?.(modalBody)
    options.renderFooter?.(modalFooter, () => modal.close(true), () => modal.close(false))
  }),
  close(accepted = false) {
    const options = modal.options
    modal.options = null
    modalBody.replaceChildren()
    modalFooter.replaceChildren()
    options?.onClose?.()
    void accepted
  },
  options: null as null | {
    onClose?: () => void
    renderBody?: (body: HTMLElement) => void
    renderFooter?: (footer: HTMLElement, confirm: () => void, cancel: () => void) => void
  },
}

function mountView() {
  const wrapper = mount(ProviderConfigView, {
    props: { bridge, modal, t, onSaved: saved },
    attachTo: document.body,
  })
  return wrapper
}

const q = (selector: string, container: ParentNode = document) =>
  container.querySelector(selector) as HTMLElement | null
const action = (name: string, index?: number, container: ParentNode = document) =>
  q(`[data-pc-action="${name}"]${index == null ? '' : `[data-pc-index="${index}"]`}`, container)
const field = (name: string, container: ParentNode = document) =>
  q(`[data-pc-field="${name}"]`, container) as HTMLInputElement | null
const mode = (name: string, container: ParentNode = document) =>
  q(`[data-pc-secret-mode="${name}"]`, container) as HTMLSelectElement | null
const keyInput = (name: string) => q(`[data-pc-key-input="${name}"]`) as HTMLInputElement | null
const fire = (node: HTMLElement | null, type: string) => {
  expect(node).toBeTruthy()
  node!.dispatchEvent(new Event(type, { bubbles: true }))
}
const setControl = (node: HTMLElement | null, value: string, type = 'input') => {
  expect(node).toBeTruthy()
  ;(node as HTMLInputElement).value = value
  fire(node, type)
}
const tab = (name: string) => (q(`[data-pc-tab="${name}"]`) as HTMLElement | null)?.click()
const posts = () => calls.filter(call => call.method === 'post')
const keyAction = (name: string, index?: number) => {
  const button = action(name, index, modalBody) || action(name, index, modalFooter)
  expect(button, `missing key action ${name}`).toBeTruthy()
  return button as HTMLElement
}
const setKeys = (values: string[]) => {
  keyAction('manage-keys').click()
  while (action('remove-key', 0, modalBody)) keyAction('remove-key', 0).click()
  for (const value of values) {
    setControl(keyInput('newValue'), value)
    keyAction('add-key').click()
  }
  expect((keyAction('apply-keys') as HTMLButtonElement).disabled).toBe(false)
  keyAction('apply-keys').click()
}

beforeEach(() => {
  document.body.innerHTML = ''
  const host = document.createElement('div')
  host.id = 'panel-providers'
  document.body.appendChild(host)
  modalBody.id = 'modal-body'
  modalFooter.id = 'modal-footer'
  document.body.appendChild(modalBody)
  document.body.appendChild(modalFooter)
  calls.length = 0
  confirms = 0
  confirmationResult = false
  saved.mockClear()
  server = snapshot()
  bridge.get.mockClear()
  bridge.post.mockClear()
  getImpl = async () => server
  postImpl = async (_route, rawPayload) => {
    const payload = rawPayload as Record<string, any> | undefined
    return {
      ...server,
      revision: 'r2',
      provider_polling: payload?.provider_polling,
      entries: ((payload?.entries ?? []) as Record<string, unknown>[]).map((entry, i) => ({
        ...entry,
        id: entry.id ?? `new-${i}`,
        supported: !!TEMPLATES[entry.api_type as string],
        secrets: { api_keys: { present: true, count: 2 } },
        unknown_fields: [],
      })),
      common: {
        values: (payload?.common as Record<string, unknown> | undefined)?.values,
        secrets: {},
      },
    }
  }
  modal.options = null
  modal.confirm.mockClear()
  modal.openCustom.mockClear()
})

async function open(wrapper: VueWrapper) {
  await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
  await flushPromises()
}

describe('ProviderConfigView（对应原 vm 子进程测试）', () => {
  it('版本冲突要求显式确认后重新加载', async () => {
    const saveError = Object.assign(new Error('conflict'), { status: 409, data: { reason: 'revision' } })
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as {
      conflict: boolean
      dirty: boolean
      draft: { revision: string; entries?: { values: { api_keys?: string[] }; secret_actions: Record<string, unknown> }[] }
    }
    action('edit', 0)!.click()
    setKeys(['conflict-key'])
    keyAction('apply-edit').click()
    await flushPromises()
    postImpl = async () => { throw saveError }
    action('save')!.click()
    await flushPromises()
    expect(vm.conflict).toBe(true)
    expect(vm.draft.revision).toBe('r1')
    expect((action('save') as HTMLButtonElement).disabled).toBe(true)
    action('save')!.click()
    await flushPromises()
    expect(posts().length).toBe(1)
    expect(vm.draft.entries![0]!.values.api_keys).toEqual(['conflict-key'])
    expect(vm.draft.entries![0]!.secret_actions.api_keys).toBeUndefined()
    action('reload')!.click()
    await flushPromises()
    expect(confirms).toBe(1)
    expect(vm.dirty).toBe(true)
    confirmationResult = true
    server = { ...server, revision: 'r9' }
    action('reload')!.click()
    await flushPromises()
    expect(confirms).toBe(2)
    expect(vm.draft.revision).toBe('r9')
    expect(vm.dirty).toBe(false)
    expect(vm.conflict).toBe(false)
    expect(saved).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('掩码 Key 走 secret_actions 且不暴露原值', async () => {
    for (const actionMode of ['keep', 'replace', 'append', 'clear']) {
      calls.length = 0
      server = snapshot()
      server.key_values_visible = false
      const entry = server.entries![0]!
      delete (entry.values as Record<string, unknown>).api_keys
      entry.secrets!.api_keys = { present: true, count: 2, previews: ['••••one', '••••two'] }
      const wrapper = mountView()
      await open(wrapper)
      action('edit', 0)!.click()
      expect(modalBody.textContent.includes('fake-old-one')).toBe(false)
      expect(modalBody.textContent.includes('••••one')).toBe(true)
      if (actionMode !== 'keep') {
        setControl(mode('api_keys'), actionMode, 'change')
        await flushPromises()
      }
      if (['replace', 'append'].includes(actionMode)) {
        setControl(field('api_keys'), `new-${actionMode}-key`)
      }
      keyAction('apply-edit').click()
      await flushPromises()
      action('save')!.click()
      await flushPromises()
      const payload = (posts()[posts().length - 1]!.payload as Record<string, any>).entries[0] as Record<string, any>
      expect(payload.values.api_keys).toBeUndefined()
      expect(payload.secret_actions.api_keys.mode).toBe(actionMode)
      if (['replace', 'append'].includes(actionMode)) {
        expect(payload.secret_actions.api_keys.value).toEqual([`new-${actionMode}-key`])
      } else {
        expect(payload.secret_actions.api_keys.value).toBeUndefined()
      }
      wrapper.unmount()
    }
  })

  it('允许手填视觉 ID 并解释安全校验失败', async () => {
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as {
      dirty: boolean
      conflict: boolean
      message: string
      snapshot: ProviderSnapshot
    }
    tab('common')
    await flushPromises()
    expect(field('vision_provider_id')!.tagName).toBe('SELECT')
    action('manual-vision')!.click()
    await flushPromises()
    expect(field('vision_provider_id')!.tagName).toBe('INPUT')
    setControl(field('vision_provider_id'), 'manual-unlisted-vision')
    await flushPromises()
    postImpl = async () => { throw Object.assign(new Error('启用的供应商配置缺少有效模型或 API Key'), { status: 'error', statusCode: 400 }) }
    action('save')!.click()
    await flushPromises()
    expect(vm.message).toMatch(/缺少有效模型或 API Key/)
    expect(vm.dirty).toBe(true)
    expect(vm.conflict).toBe(false)
    const firstPayload = posts()[0]!.payload! as Record<string, any>
    expect((firstPayload.common.values as Record<string, unknown>).vision_provider_id).toBe('manual-unlisted-vision')
    postImpl = async () => { throw Object.assign(new Error('供应商运行时不可用，请重载插件'), { status: 'error', statusCode: 503, data: {} }) }
    action('save')!.click()
    await flushPromises()
    expect(vm.snapshot.requires_reload).toBe(true)
    expect((action('save') as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })

  it('不强制改写未变化的旧枚举', async () => {
    server.entries![0]!.api_type = 'doubao'
    server.entries![0]!.values = { enabled: false, endpoint_mode: 'plan' }
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as { dirty: boolean }
    tab('common')
    await flushPromises()
    setControl(field('vision_model'), 'new-vision-model')
    await flushPromises()
    action('save')!.click()
    await flushPromises()
    expect(posts().length).toBe(1)
    const payload = posts()[0]!.payload as Record<string, any>
    expect(payload.entries[0].values.endpoint_mode).toBe('plan')
    expect(vm.dirty).toBe(false)
    wrapper.unmount()
  })

  it('失焦修改不替换模型按钮或搜索结果', async () => {
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as { catalogs: { edit: { token: number } } }
    action('edit', 0)!.click()
    setControl(field('api_base', modalBody), 'https://edited.invalid/v1')
    const fetchButton = action('fetch-models', undefined, modalBody)
    const token = vm.catalogs.edit.token
    setControl(field('api_base', modalBody), 'https://edited.invalid/v1', 'change')
    expect(action('fetch-models', undefined, modalBody)).toBe(fetchButton)
    expect(vm.catalogs.edit.token).toBe(token)
    postImpl = async () => ({ models: [{ id: 'model-picked', label: 'Model Picked' }] })
    fetchButton!.click()
    await flushPromises()
    const search = q('[data-pc-search]', modalBody) as HTMLInputElement
    setControl(search, 'Picked')
    const choice = action('choose-model', undefined, modalBody)
    setControl(search, 'Picked', 'change')
    expect(action('choose-model', undefined, modalBody)).toBe(choice)
    choice!.click()
    expect(field('model', modalBody)!.value).toBe('model-picked')
    wrapper.unmount()
  })

  it('成功信封保留目标确认且桥接错误只带文案', async () => {
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as {
      catalogs: { edit: { confirmation: { target: string } | null } }
    }
    action('edit', 0)!.click()
    postImpl = async () => ({ confirmation_required: true, target: 'https://target.invalid' })
    action('fetch-models', undefined, modalBody)!.click()
    await flushPromises()
    expect(vm.catalogs.edit.confirmation!.target).toBe('https://target.invalid')
    expect(action('confirm-target', undefined, modalBody)).toBeTruthy()
    postImpl = async () => ({ models: [{ id: 'chosen', label: 'Chosen' }] })
    action('confirm-target', undefined, modalBody)!.click()
    await flushPromises()
    expect((posts()[1]!.payload! as Record<string, unknown>).confirmed_target).toBe(true)
    action('choose-model', undefined, modalBody)!.click()
    expect(field('model', modalBody)!.value).toBe('chosen')
    postImpl = async () => { throw new Error('供应商配置已更新，请重新加载后再保存') }
    action('fetch-models', undefined, modalBody)!.click()
    await flushPromises()
    expect(modalBody.textContent).toMatch(/配置版本已变化/)
    expect(field('model', modalBody)!.value).toBe('chosen')
    wrapper.unmount()
  })

  it('视觉刷新失败保留原名单与草稿', async () => {
    const wrapper = mountView()
    await open(wrapper)
    const vm = wrapper.vm as unknown as { snapshot: ProviderSnapshot }
    tab('common')
    await flushPromises()
    setControl(field('vision_provider_id'), 'vision-1', 'change')
    setControl(field('vision_model'), 'kept-model')
    const previous = JSON.stringify(vm.snapshot.vision_providers)
    getImpl = async () => ({
      vision_providers: [],
      vision_providers_available: false,
      vision_providers_warning: 'Unavailable',
    })
    action('refresh-vision')!.click()
    await flushPromises()
    expect(JSON.stringify(vm.snapshot.vision_providers)).toBe(previous)
    expect(field('vision_provider_id')!.value).toBe('vision-1')
    expect(field('vision_model')!.value).toBe('kept-model')
    expect(document.body.textContent).toMatch(/刷新失败/)
    wrapper.unmount()
  })
})
