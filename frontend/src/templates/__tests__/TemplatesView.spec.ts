import { describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TemplatesView from '../TemplatesView.vue'
import type { HubState, StudioTemplate } from '../types'

const t = (key: string, params?: Record<string, unknown>) =>
  key.replace(/\{v(\d+)\}/g, (_, i: string) => String(params?.[`v${i}`] ?? ''))

function template(id: string, overrides: Partial<StudioTemplate> = {}): StudioTemplate {
  return { id, name: `模板${id}`, content: `内容${id}`, tags: [], ...overrides }
}

function makeBridge(routes: Record<string, unknown>) {
  return {
    get: vi.fn((path: string) => Promise.resolve(routes[path])),
    post: vi.fn((path: string) => Promise.resolve(routes[path])),
  }
}

const modal = {
  confirm: vi.fn(async () => true),
  openCustom: vi.fn(),
}

function mountView(bridge: ReturnType<typeof makeBridge>) {
  const onUsed = vi.fn()
  const wrapper = mount(TemplatesView, {
    props: { bridge, modal, t, onUsed, onSaved: async () => {} },
  })
  return { wrapper, onUsed }
}

describe('TemplatesView', () => {
  it('首帧未加载时显示占位提示，加载后渲染本地卡片并拉取远程缓存', async () => {
    const bridge = makeBridge({
      'webui/templates': { templates: [template('a')] },
      'webui/templates/hub': {
        templates: [{ ...template('b'), found_via: 'cache' }],
        notice: '',
        state: { hash: 'abc', count: 1 } as HubState,
      },
      'webui/templates/hub/tags': { tags: [] },
    })
    const { wrapper } = mountView(bridge)
    expect(wrapper.text()).toContain('打开提示词广场后加载。')

    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
    await flushPromises()

    expect(bridge.get).toHaveBeenCalledWith('webui/templates')
    expect(wrapper.text()).toContain('模板a')
    // 合并网格：远程缓存卡片也出现在本地页
    expect(wrapper.text()).toContain('模板b')
    expect(wrapper.text()).toContain('缓存')
    expect(wrapper.text()).toContain('已加载 1 个模板。')
  })

  it('未绑密钥点击我的模板时不发请求，直接弹密钥弹窗', async () => {
    const bridge = makeBridge({
      'webui/templates': { templates: [] },
      'webui/templates/hub': {
        templates: [],
        notice: '',
        state: { hash: 'abc', count: 0, has_key: false } as HubState,
      },
      'webui/templates/hub/tags': { tags: [] },
      'webui/templates/hub/key': { configured: false, masked: '' },
    })
    const { wrapper } = mountView(bridge)
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
    await flushPromises()

    const mineGetCalls = () =>
      bridge.get.mock.calls.filter(([path]) => path === 'webui/templates/hub/mine').length
    const tabButtons = wrapper.findAll('.tpl-tabs button')
    await tabButtons[1]!.trigger("click")
    await flushPromises()

    expect(mineGetCalls()).toBe(0)
    expect(modal.openCustom).toHaveBeenCalledWith(
      expect.objectContaining({ title: '在线模板密钥' })
    )
    expect(wrapper.text()).toContain('请先填写在线模板密钥。')
  })

  it('我的模板页渲染修改删除按钮，本地卡不出现', async () => {
    const bridge = makeBridge({
      'webui/templates': { templates: [template('a')] },
      'webui/templates/hub': { templates: [], notice: '', state: { has_key: true } as HubState },
      'webui/templates/hub/tags': { tags: [] },
      'webui/templates/hub/mine': { templates: [{ ...template('m'), remote_id: 'r1' }], state: null },
    })
    const { wrapper } = mountView(bridge)
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
    await flushPromises()

    await wrapper.findAll('.tpl-tabs button')[1]!.trigger('click')
    await flushPromises()

    expect(bridge.get).toHaveBeenCalledWith('webui/templates/hub/mine', { limit: '50' })
    expect(wrapper.text()).toContain('模板m')
    const mineCard = wrapper.findAll('.tpl-card').find(c => c.text().includes('模板m'))
    expect(mineCard?.text()).toContain('修改')
    expect(mineCard?.text()).toContain('删除')
    expect(mineCard?.text()).not.toContain('举报')
  })

  it('填入工作台把模板正文交给 onUsed', async () => {
    const bridge = makeBridge({
      'webui/templates': { templates: [template('a')] },
      'webui/templates/hub': { templates: [], notice: '', state: null },
      'webui/templates/hub/tags': { tags: [] },
    })
    const { wrapper, onUsed } = mountView(bridge)
    await (wrapper.vm as unknown as { loadOnce: () => Promise<void> }).loadOnce()
    await flushPromises()

    await wrapper.find('.tpl-card .comic-btn--cta').trigger('click')
    expect(onUsed).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: '内容a', template: expect.objectContaining({ id: 'a' }) })
    )
  })
})
