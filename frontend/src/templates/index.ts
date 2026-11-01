import { createApp } from 'vue'
import TemplatesView from './TemplatesView.vue'
import type { BridgeClientLike, ModalLike, StudioTemplate, Translator } from './types'

export interface TemplatesViewController {
  open: () => Promise<void>
  destroy: () => void
}

/** 把提示词广场挂载到给定容器；返回兼容旧调用方式的 open/destroy 控制器。 */
export function createTemplatesView(
  container: HTMLElement,
  {
    bridge,
    modal,
    t,
    onUsed,
    onSaved
  }: {
    bridge: BridgeClientLike
    modal: ModalLike
    t: Translator
    onUsed: (payload: { prompt?: string; template?: StudioTemplate }) => void
    onSaved?: () => Promise<void>
  }
): TemplatesViewController {
  const app = createApp(TemplatesView, {
    bridge,
    modal,
    t,
    onUsed,
    onSaved
  })
  const instance = app.mount(container) as unknown as { loadOnce: () => Promise<void> }
  return {
    open: () => instance.loadOnce(),
    destroy: () => app.unmount()
  }
}
