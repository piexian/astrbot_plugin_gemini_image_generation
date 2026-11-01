import { createApp } from 'vue'
import GalleryView from './GalleryView.vue'
import type { BridgeClientLike, ModalLike, Translator } from '../provider-config/types'

export interface GalleryViewController {
  fetchGallery: () => Promise<void>
  destroy: () => void
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>

export interface GalleryViewDeps {
  // Vue createApp props 需要索引签名兼容
  [key: string]: unknown
  store: { removeJobs: (ids: string[]) => void }
  bridge: BridgeClientLike
  modal: ModalLike
  t: Translator
  safeDom: {
    isSafeImageName: (name: unknown) => boolean
    sourceLabel: (source?: string, caller?: AnyRecord) => string
    displayRoute: (record: AnyRecord) => { provider: string; model: string }
    requesterLabels: (requester: AnyRecord | undefined, source?: string) => string[]
    referenceStrip: (record: AnyRecord, onSelect: (name: string) => void) => HTMLElement | null
  }
  icons: Record<string, string>
  toast: { success: (m: string) => void; warning: (m: string) => void; error: (m: string) => void }
  errorFeedback: { show: (err: unknown, fallback: string) => void }
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void }
  imageLoader: { attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void }
  lightbox: { open: (items: unknown[], index: number) => void }
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void
  onRefillRequest?: (prompt: string | undefined, params: unknown, record: AnyRecord) => void
  onSetReference?: (imageName: string) => void
}

/** 把历史画廊挂载到给定容器；返回兼容旧调用方式的 fetchGallery/destroy 控制器。 */
export function createGalleryView(
  container: HTMLElement,
  deps: GalleryViewDeps
): GalleryViewController {
  const app = createApp(GalleryView, deps)
  const instance = app.mount(container) as unknown as { fetchGallery: () => Promise<void> }
  return {
    fetchGallery: () => instance.fetchGallery(),
    destroy: () => app.unmount(),
  }
}
