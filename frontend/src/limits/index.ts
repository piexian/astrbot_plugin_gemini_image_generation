import { createApp } from 'vue'
import LimitsView from './LimitsView.vue'
import type { BridgeClientLike, ModalLike, Translator } from '../provider-config/types'

export interface LimitsViewController {
  open: () => Promise<void>
  destroy: () => void
  readonly dirty: boolean
}

/** 把限流视图挂载到给定容器；返回兼容旧调用方式的 open/destroy/dirty 控制器。 */
export function createLimitsView(
  container: HTMLElement,
  {
    bridge,
    modal,
    t
  }: {
    bridge: BridgeClientLike
    modal: ModalLike
    t: Translator
  }
): LimitsViewController {
  const app = createApp(LimitsView, { bridge, modal, t })
  const instance = app.mount(container) as unknown as {
    loadOnce: () => Promise<void>
    dirty: boolean
  }
  return {
    open: () => instance.loadOnce(),
    destroy: () => app.unmount(),
    get dirty() {
      return instance.dirty
    },
  }
}
