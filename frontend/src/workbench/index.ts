import { createApp } from "vue";
import WorkbenchView from "./WorkbenchView.vue";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>;

export interface WorkbenchViewDeps {
  // Vue createApp props 需要索引签名兼容
  [key: string]: unknown;
  bridge: AnyRecord;
  modal: AnyRecord;
  t: (key: string, params?: Record<string, unknown>) => string;
  icons: Record<string, string>;
  toast: AnyRecord;
  errorFeedback: AnyRecord;
  clipboard: AnyRecord;
  imageLoader: AnyRecord;
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void;
  onJobSubmitted?: (newJobId?: string) => void;
}

export interface WorkbenchViewController {
  loadCapabilities: () => Promise<void>;
  showGlobalBanner: (message: string) => void;
  setFormDisabled: (disabled: boolean) => void;
  applyTemplate: (prompt: string, template?: AnyRecord) => void;
  refillParameters: (prompt: string, params?: AnyRecord) => void;
  addGalleryReference: (imageName: string) => boolean;
  restoreReferences: (names: unknown[]) => number;
  destroy: () => void;
}

/** 把创作台挂载到给定容器；返回兼容旧调用方式的工作台控制器。 */
export function createWorkbenchView(
  container: HTMLElement,
  deps: WorkbenchViewDeps,
): WorkbenchViewController {
  const app = createApp(WorkbenchView, deps);
  const instance = app.mount(container) as AnyRecord;
  return {
    loadCapabilities: () => instance.loadCapabilities(),
    showGlobalBanner: (message: string) => instance.showGlobalBanner(message),
    setFormDisabled: (disabled: boolean) => instance.setFormDisabled(disabled),
    applyTemplate: (prompt: string, template?: AnyRecord) =>
      instance.applyTemplate(prompt, template),
    refillParameters: (prompt: string, params?: AnyRecord) =>
      instance.refillParameters(prompt, params),
    addGalleryReference: (imageName: string) => instance.addGalleryReference(imageName),
    restoreReferences: (names: unknown[]) => instance.restoreReferences(names),
    destroy: () => app.unmount(),
  };
}
