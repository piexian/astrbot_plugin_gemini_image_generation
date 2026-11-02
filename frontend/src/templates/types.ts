export interface StudioTemplate {
  id: string
  name: string
  ai_description?: string
  content?: string
  reference?: string
  tags?: string[]
  builtin?: boolean
  ai_enabled?: boolean
  defaults?: { resolution?: string; aspect_ratio?: string }
}

export interface HubTemplate extends StudioTemplate {
  remote_id?: string
  author?: string
  source_url?: string
  found_via?: string
}

export interface HubState {
  mode?: string
  enabled?: boolean
  has_key?: boolean
  hash?: string
  count?: number
}

export interface HubSearchPayload {
  templates?: HubTemplate[]
  notice?: string
  state?: HubState
}

export interface KeyInfo {
  configured: boolean
  masked: string
}

export type Translator = (key: string, params?: Record<string, unknown>) => string

export interface BridgeClientLike {
  get: (path: string, params?: Record<string, string>) => Promise<unknown>
  post: (path: string, body?: unknown) => Promise<unknown>
  delete?: (path: string) => Promise<unknown>
  isAvailable?: () => boolean
}

export interface ModalLike {
  confirm: (options: {
    title?: string
    content?: string
    confirmText?: string
    danger?: boolean
  }) => Promise<boolean>
  openCustom: (options: {
    title?: string
    variant?: string
    onClose?: () => void
    renderBody?: (body: HTMLElement) => void
    renderFooter?: (footer: HTMLElement, confirm: () => void, cancel: () => void) => void
  }) => void
}
