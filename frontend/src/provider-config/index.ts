import { createApp } from "vue";
import ProviderConfigView from "./ProviderConfigView.vue";
import type { BridgeClientLike, ModalLike, Translator } from "./types";

export interface ProviderConfigController {
  open: () => Promise<void>;
  destroy: () => void;
}

/** 把供应商配置挂载到给定容器；返回兼容旧调用方式的 open/destroy 控制器。 */
export function createProviderConfigView(
  container: HTMLElement,
  {
    bridge,
    modal,
    t,
    onSaved,
  }: {
    bridge: BridgeClientLike;
    modal: ModalLike;
    t: Translator;
    onSaved?: () => Promise<void>;
  },
): ProviderConfigController {
  const app = createApp(ProviderConfigView, { bridge, modal, t, onSaved });
  const instance = app.mount(container) as unknown as { loadOnce: () => Promise<void> };
  return {
    open: () => instance.loadOnce(),
    destroy: () => app.unmount(),
  };
}
