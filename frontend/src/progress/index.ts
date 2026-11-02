import { createApp } from "vue";
import ProgressView from "./ProgressView.vue";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>;

export interface ProgressViewDeps {
  // Vue createApp props 需要索引签名兼容
  [key: string]: unknown;
  store: AnyRecord;
  bridge: AnyRecord;
  t: (key: string, params?: Record<string, unknown>) => string;
  safeDom: AnyRecord;
  icons: Record<string, string>;
  toast: AnyRecord;
  errorFeedback: AnyRecord;
  clipboard: AnyRecord;
  imageLoader: AnyRecord;
  lightbox: AnyRecord;
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void;
  onRefillRequest?: (prompt: string | undefined, params: unknown, record: AnyRecord) => void;
  onSetReference?: (imageName: string) => void;
}

export interface ProgressViewController {
  highlightJob: (jobId: string) => void;
  destroy: () => void;
}

/** 把任务进度挂载到给定容器；返回兼容旧调用方式的 highlightJob/destroy 控制器。 */
export function createProgressView(
  container: HTMLElement,
  deps: ProgressViewDeps,
): ProgressViewController {
  const app = createApp(ProgressView, deps);
  const instance = app.mount(container) as unknown as { highlightJob: (jobId: string) => void };
  return {
    highlightJob: (jobId) => instance.highlightJob(jobId),
    destroy: () => app.unmount(),
  };
}
