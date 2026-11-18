import { createApp } from "vue";
import KeywordsView from "./KeywordsView.vue";
import type { BridgeClientLike, ModalLike, Translator } from "../provider-config/types";

export interface KeywordsViewController {
  reload: () => Promise<void>;
  destroy: () => void;
}

/** 把关键词页签挂载到给定容器；页签每次激活都整页重读（reload 语义）。 */
export function createKeywordsView(
  container: HTMLElement,
  deps: {
    bridge: BridgeClientLike;
    t: Translator;
    modal: ModalLike;
    toast: {
      success: (m: string) => void;
      warning: (m: string) => void;
      error: (m: string) => void;
      info: (m: string) => void;
    };
    errorFeedback: { show: (err: unknown, fallback: string) => void };
  },
): KeywordsViewController {
  const app = createApp(KeywordsView, deps);
  const instance = app.mount(container) as unknown as { reload: () => Promise<void> };
  return {
    reload: () => instance.reload(),
    destroy: () => app.unmount(),
  };
}
