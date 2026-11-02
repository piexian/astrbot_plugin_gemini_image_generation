export type Translator = (key: string, params?: Record<string, unknown>) => string

export interface BridgeClientLike {
  get: (path: string, params?: Record<string, string>) => Promise<unknown>
  post: (path: string, body?: unknown) => Promise<unknown>
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
    renderBody?: (body: HTMLElement) => void
    renderFooter?: (footer: HTMLElement, confirm: () => void, cancel: () => void) => void
    onClose?: () => void
    variant?: string
  }) => void
}

export interface FieldSchema {
  type?: string
  description?: string
  default?: unknown
  hint?: string
  options?: unknown[]
  slider?: { min?: number; max?: number; step?: number }
  condition?: Record<string, unknown>
  [key: string]: unknown
}

export interface EntryValues {
  enabled?: boolean
  model?: string
  endpoint_id?: string
  api_keys?: unknown
  priority?: number
  service_account_files?: unknown
  [key: string]: unknown
}

export interface DraftEntry {
  id: string | null
  api_type: string
  values: EntryValues
  secret_actions: Record<string, { mode: string; value?: unknown }>
}

export interface SnapshotEntry {
  id: string | null
  api_type: string
  supported?: boolean
  values?: EntryValues
  secrets?: Record<string, { present?: boolean; count?: number; previews?: string[] }>
  unknown_fields?: string[]
  [key: string]: unknown
}

export interface ProviderSnapshot {
  revision?: string
  entries?: SnapshotEntry[]
  templates?: Record<string, { label?: string; fields?: Record<string, FieldSchema> }>
  provider_types?: { id: string; label: string }[]
  common?: { values?: Record<string, unknown>; secrets?: Record<string, { present?: boolean; count?: number; previews?: string[] }> }
  common_fields?: Record<string, FieldSchema>
  provider_polling?: string[]
  model_catalog?: Record<string, { supported?: boolean; message?: string }>
  vision_providers?: { id: string; label?: string; model?: string; available?: boolean }[]
  vision_providers_available?: boolean
  vision_providers_warning?: string
  requires_reload?: boolean
  busy?: boolean
  [key: string]: unknown
}

export interface DraftState {
  revision: string
  provider_polling: string[]
  entries: DraftEntry[]
  common: {
    values: Record<string, unknown>
    secret_actions: Record<string, { mode: string; value?: unknown }>
  }
}

export interface CatalogState {
  token: number
  loading: boolean
  confirmation: { payload: Record<string, unknown>; target: string } | null
  models: { id: string; label: string }[]
  search: string
  message: string
  field?: string
  host: HTMLElement | null
  results: HTMLElement | null
}

export interface KeysState {
  items: string[]
  newValue: string
  editIndex: number
  editValue: string
  batch: boolean
  batchValue: string
  message: string
}

export interface EditorState {
  entry: DraftEntry | null
  index: number | null
  body: HTMLElement | null
  footer: HTMLElement | null
  accept?: () => void
  cancel?: () => void
  feedback?: HTMLElement
  keys: KeysState | null
}
