import { createApp } from "vue";
import ReferencesView from "./ReferencesView.vue";
import type { BridgeClientLike, ModalLike, Translator } from "../provider-config/types";

export interface ReferencesViewController {
  loadOnce: () => Promise<void>;
  reload: () => Promise<void>;
  destroy: () => void;
}

/** 把参考图库挂载到给定容器；返回 loadOnce/reload/destroy 控制器。 */
export function createReferencesView(
  container: HTMLElement,
  deps: {
    bridge: BridgeClientLike;
    modal: ModalLike;
    t: Translator;
    toast: {
      success: (m: string) => void;
      warning: (m: string) => void;
      error: (m: string) => void;
      info: (m: string) => void;
    };
    errorFeedback: { show: (err: unknown, fallback: string) => void };
    clipboard: { copy: (text: string, message?: string) => Promise<void> | void };
    imageLoader: {
      attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void;
    };
    lightbox: { open: (items: unknown[], index: number) => void };
    installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void;
    onSetReference?: (imageName: string) => void;
  },
): ReferencesViewController {
  const app = createApp(ReferencesView, deps);
  const instance = app.mount(container) as unknown as {
    loadOnce: () => Promise<void>;
    reload: () => Promise<void>;
  };
  return {
    loadOnce: () => instance.loadOnce(),
    reload: () => instance.reload(),
    destroy: () => app.unmount(),
  };
}
