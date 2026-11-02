<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { GenerationSettingsEditor, type GenerationField } from './generation-settings'
import { StudioPreferences } from './studio-preferences'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>

interface Capability {
  provider?: string
  provider_display?: string
  model?: string
  model_alias?: string
  candidate_id: string
  resolutions?: string[]
  aspect_ratios?: string[]
  generation_fields?: Record<string, GenerationField>
  max_reference_images?: number
  native_max_reference_images?: number
  parameters?: Record<string, { maximum?: number }>
}

interface BatchItem {
  id: string
  name: string
  prompt: string
  image_count: number
}

interface TrayItem {
  id: string
  type: 'upload' | 'gallery'
  name: string
  previewUrl: string
  file?: File
}

interface ModalLike {
  confirm: (options: Record<string, unknown>) => Promise<boolean>
  openCustom: (options: {
    title?: string
    variant?: string
    onClose?: () => void
    renderBody?: (body: HTMLElement) => void
    renderFooter?: (footer: HTMLElement, confirm: () => void, cancel: () => void) => void
  }) => void
  close: (accepted?: boolean) => void
}

interface Props {
  bridge: {
    get: (path: string, params?: unknown) => Promise<unknown>
    post: (path: string, body?: unknown) => Promise<unknown>
    upload: (path: string, file: File) => Promise<unknown>
  }
  modal: ModalLike
  t: (key: string, params?: Record<string, unknown>) => string
  icons: Record<string, string>
  toast: {
    success: (m: string, duration?: number) => void
    warning: (m: string, duration?: number) => void
    error: (m: string, duration?: number) => void
    info: (m: string, duration?: number) => void
  }
  errorFeedback: {
    show: (err: unknown, fallback: string) => void
    isAuth: (err: unknown) => boolean
  }
  clipboard: { copy: (text: string, message?: string) => Promise<void> | void }
  imageLoader: { attach: (img: HTMLImageElement, name: string, opts?: Record<string, unknown>) => void }
  installImageFallback: (img: HTMLImageElement, wrap: HTMLElement, text: string) => void
  onJobSubmitted?: (newJobId?: string) => void
}

const props = defineProps<Props>()

// ---------- 基建实例 ----------
const prefs = new StudioPreferences(props.bridge, props.t)
const offscreen = document.createElement('div')
const settingsEditor = new GenerationSettingsEditor(offscreen, props.t)

// ---------- 状态 ----------
const prompt = ref('')
const formAvailable = ref(false)
const isSubmitting = ref(false)
const isUploading = ref(false)
const mode = ref<'single' | 'batch'>('single')
const models = ref<Capability[]>([])
const modelIndex = ref<number>(NaN)
const resolutions = ref<string[]>([])
const aspectRatios = ref<string[]>([])
const resolution = ref('')
const aspectRatio = ref('')
const imageCount = ref(1)
const imageCountMax = ref(10)
const resolutionVisible = ref(true)
const aspectVisible = ref(true)
const overrideCountText = ref(props.t('使用供应商配置'))
const moreParamsVisible = ref(false)
const moreParamsExpanded = ref(false)
const batchItems = reactive<BatchItem[]>([])
const batchCounter = ref(0)
const batchBudgetLimit = ref(0)
const batchMaxTasks = ref(0)
const uploadMaxMb = ref(0)
const batchBudgetValid = ref(true)
const referencesValid = ref(true)
const tray = reactive<TrayItem[]>([])
const prefsStatus = ref('')
const bannerText = ref('')
const bannerVisible = ref(false)
let activeCandidate: Capability | null = null
interface ParameterDialogState {
  candidate: Capability
  fields: Record<string, GenerationField>
  editor: GenerationSettingsEditor | null
  feedback: HTMLElement | null
}

let parameterDialog: ParameterDialogState | null = null
let destroyed = false
const fileInput = ref<HTMLInputElement | null>(null)

prefs.onStatus = status => {
  prefsStatus.value = status
}

// ---------- 派生 ----------
const promptOverflow = computed(() => prompt.value.length > 2000)
const charCountText = computed(() => `${prompt.value.length} / 2000`)
const selectedModel = computed<Capability | null>(() => {
  if (!Number.isInteger(modelIndex.value)) return null
  return models.value[modelIndex.value] || null
})
const modelGroups = computed(() => {
  const groups = new Map<string, { entry: Capability; index: number }[]>()
  models.value.forEach((entry, index) => {
    const groupLabel = entry.provider_display || entry.provider || props.t('其他')
    if (!groups.has(groupLabel)) groups.set(groupLabel, [])
    groups.get(groupLabel)!.push({ entry, index })
  })
  return [...groups.entries()].map(([label, items]) => ({
    label,
    items: items.map(({ entry, index }) => {
      const modelName = entry.model || props.t('默认模型')
      const displayName = entry.model_alias ? `${modelName}（${entry.model_alias}）` : modelName
      return { index, text: `${displayName} · ${entry.candidate_id}` }
    }),
  }))
})
const batchTotal = computed(() =>
  batchItems.reduce((acc, item) => acc + (item.image_count || 1), 0)
)
const batchBudgetDanger = computed(() => batchTotal.value > batchBudgetLimit.value)
const submitDisabled = computed(
  () =>
    !formAvailable.value ||
    isSubmitting.value ||
    isUploading.value ||
    !batchBudgetValid.value ||
    !referencesValid.value ||
    (mode.value === 'batch' && batchItems.length === 0)
)
const addBatchDisabled = computed(
  () => !formAvailable.value || batchItems.length >= batchMaxTasks.value
)
const trayFull = computed(
  () => tray.length >= getMaxReferenceImages()
)
const uploadDisabled = computed(
  () => !formAvailable.value || isSubmitting.value || isUploading.value || trayFull.value
)
const trayCounterDanger = computed(() => !referencesValid.value)
const trayCountText = computed(
  () => `${tray.length} / ${getMaxReferenceImages()}`
)

// ---------- 内部工具 ----------
function showBanner(msg: string) {
  bannerText.value = msg
  bannerVisible.value = true
}

function hideBanner() {
  bannerVisible.value = false
}

function rememberParameters() {
  if (!activeCandidate) return
  prefs.remember(activeCandidate, {
    resolution: resolution.value,
    aspect_ratio: aspectRatio.value,
    image_count: Number(imageCount.value) || 1,
    generation_settings: settingsEditor.overrides({ validate: false, includeHidden: true }),
  })
}

function updateGenerationConditions() {
  const entry = selectedModel.value
  if (!entry) return
  resolutionVisible.value = Boolean(
    (entry.resolutions?.length || 0) > 0 &&
      settingsEditor.matchesCondition(entry.generation_fields?.resolution)
  )
  aspectVisible.value = Boolean(
    (entry.aspect_ratios?.length || 0) > 0 &&
      settingsEditor.matchesCondition(entry.generation_fields?.aspect_ratio)
  )
  const count = Object.keys(settingsEditor.overrides({ validate: false })).length
  overrideCountText.value = count
    ? props.t('{v0} 项临时覆盖', { v0: count })
    : props.t('使用供应商配置')
}

function syncParameterDialogButton(hasAdvancedParameters: boolean) {
  moreParamsVisible.value = hasAdvancedParameters
  moreParamsExpanded.value = !!parameterDialog
}

function getMaxReferenceImages(): number {
  const entry = selectedModel.value
  const fields = entry?.generation_fields || {}
  const values = settingsEditor?.effectiveValues() || {}
  if (
    Object.entries(fields).some(
      ([name, field]) =>
        Object.prototype.hasOwnProperty.call(field, 'disables_references_when') && values[name] === field.disables_references_when
    )
  ) {
    return 0
  }
  const override = settingsEditor?.overrides({ validate: false }).max_reference_images as
    | number
    | undefined
  const maximum = override ?? fields.max_reference_images?.value ?? entry?.max_reference_images
  const nativeMaximum = entry?.native_max_reference_images ?? maximum
  return Number.isSafeInteger(maximum) && (maximum as number) >= 0
    ? Math.min(maximum as number, nativeMaximum as number)
    : 0
}

function updateReferenceCounter() {
  const max = getMaxReferenceImages()
  const current = tray.length
  referencesValid.value = current <= max
  updateActionState()
}

function updateActionState() {
  // submitDisabled 为 computed；保留方法以对齐调用点语义。
  const total = batchTotal.value
  batchBudgetValid.value =
    mode.value !== 'batch' ||
    (total > 0 && total <= batchBudgetLimit.value && batchItems.length <= batchMaxTasks.value)
  if (total > batchBudgetLimit.value) {
    props.toast.warning(
      props.t('总预算消耗 {v0} 张超出上限 {v1} 张', { v0: total, v1: batchBudgetLimit.value })
    )
  }
}

// ---------- 模型联动 ----------
function handleModelChange(
  { remember = true, restore = true, persistSelection = true } = {}
) {
  closeParameterDialog()
  if (remember) rememberParameters()
  const entry = selectedModel.value
  if (!entry) return
  activeCandidate = entry
  const saved = restore ? prefs.get(entry) : null
  const fields = entry.generation_fields || {}
  settingsEditor.render(fields, (saved?.generation_settings as Record<string, unknown>) || {})
  for (const [name, select, choices] of [
    ['resolution', 'resolution', entry.resolutions || []],
    ['aspect_ratio', 'aspectRatio', entry.aspect_ratios || []],
  ] as [string, 'resolution' | 'aspectRatio', string[]][]) {
    const defaultValue = (fields[name]?.value as string) ?? ''
    const desired = saved?.[name] ?? defaultValue
    const desiredText = String(desired)
    const target = choices.some(choice => choice === desiredText) ? desiredText : String(defaultValue)
    if (name === 'resolution') {
      resolutions.value = [...choices]
      resolution.value = target
    } else {
      aspectRatios.value = [...choices]
      aspectRatio.value = target
    }
    void select
  }
  imageCountMax.value = entry.parameters?.image_count?.maximum || 10
  const maximum = imageCountMax.value
  imageCount.value = Math.min(Math.max(Number(saved?.image_count) || 1, 1), maximum)
  syncParameterDialogButton(Object.keys(settingsEditor.controls).length > 0 || settingsEditor.controls.size > 0)
  updateGenerationConditions()
  updateReferenceCounter()
  if (persistSelection) prefs.select(entry)
}

async function loadCapabilities() {
  try {
    await prefs.load()
    const data = (await props.bridge.get('webui/capabilities')) as {
      models?: Capability[]
      limits?: Record<string, number>
    } | null
    const list = Array.isArray(data?.models) ? data!.models : []
    models.value = list
    const limits = (data?.limits ?? undefined) as Record<string, number> | undefined
    if (
      !limits ||
      !(['batch_total_budget', 'batch_max_tasks', 'upload_max_mb'] as const).every(
        key => Number.isSafeInteger(limits[key]) && (limits[key] as number) > 0
      )
    ) {
      throw new Error(props.t('模型能力响应缺少有效限制'))
    }
    batchBudgetLimit.value = limits.batch_total_budget!
    batchMaxTasks.value = limits.batch_max_tasks!
    uploadMaxMb.value = limits.upload_max_mb!

    if (list.length === 0) {
      showBanner(props.t('未检测到可用的图像生成供应商，请在插件配置中添加 provider_candidates 后刷新'))
      setFormDisabled(true)
      return
    }

    hideBanner()
    setFormDisabled(false)
    const rememberedIndex = list.findIndex(candidate =>
      prefs.matches(prefs.data.selected, candidate)
    )
    if (rememberedIndex >= 0) modelIndex.value = rememberedIndex
    else modelIndex.value = Number.isInteger(modelIndex.value) ? modelIndex.value : 0
    handleModelChange({ remember: false, persistSelection: false })
    updateActionState()
  } catch (err) {
    console.error('[WorkbenchView] 加载 capabilities 失败:', err)
    const message = props.errorFeedback.isAuth(err)
      ? props.t('登录状态已失效，请刷新 Dashboard 并重新登录')
      : props.t('加载模型能力失败，请检查插件状态或网络连接')
    showBanner(message)
    setFormDisabled(true)
  }
}

function setFormDisabled(disabled: boolean) {
  formAvailable.value = !disabled
  if (disabled) closeParameterDialog()
  settingsEditor.setDisabled(disabled)
  updateReferenceCounter()
  updateActionState()
}

// ---------- 参数弹窗 ----------
function openParameterDialog() {
  const candidate = selectedModel.value
  if (
    destroyed ||
    !formAvailable.value ||
    parameterDialog ||
    !candidate ||
    !settingsEditor.controls.size
  ) {
    return
  }
  const fields = candidate.generation_fields || {}
  const committed = settingsEditor.overrides({ validate: false, includeHidden: true })
  const dialog: ParameterDialogState = {
    candidate,
    fields,
    editor: null,
    feedback: null,
  }
  parameterDialog = dialog
  props.modal.openCustom({
    title: props.t('供应商生成参数 · {v0}', { v0: candidate.model || candidate.provider }),
    variant: 'parameters',
    onClose: () => {
      if (parameterDialog !== dialog) return
      parameterDialog = null
      syncParameterDialogButton(settingsEditor.controls.size > 0)
    },
    renderBody: body => {
      body.appendChild(
        el('p', { className: 'field-hint parameter-dialog-description' }, [
          props.t('勾选要临时覆盖的参数，保存并应用后生效。按浏览器记忆当前模型，不修改供应商配置。'),
        ])
      )
      dialog.feedback = el('p', {
        className: 'field-hint parameter-dialog-feedback',
        role: 'status',
        'aria-live': 'polite',
      })
      const grid = el('div', { id: 'provider-parameters-grid', className: 'provider-parameters-grid' })
      body.appendChild(dialog.feedback)
      body.appendChild(grid)
      dialog.editor = new GenerationSettingsEditor(grid, props.t)
      dialog.editor.onChange = () => {
        if (!dialog.feedback) return
        dialog.feedback.textContent = props.t('修改尚未应用，请点击保存并应用。')
        dialog.feedback.classList.remove('parameter-dialog-feedback--error')
      }
      dialog.editor.render(fields, committed)
    },
    renderFooter: (footer, confirm, cancel) => {
      footer.appendChild(
        el(
          'button',
          {
            type: 'button',
            id: 'btn-reset-provider-parameters',
            className: 'comic-btn comic-btn--outline parameter-dialog-reset',
            onClick: () => {
              dialog.editor!.render(fields, {})
              if (!dialog.feedback) return
              dialog.feedback.textContent = props.t('已在草稿中恢复默认参数，保存后才会应用。')
              dialog.feedback.classList.remove('parameter-dialog-feedback--error')
            },
          },
          [props.t('恢复默认参数')]
        )
      )
      footer.appendChild(
        el(
          'button',
          {
            type: 'button',
            className: 'comic-btn comic-btn--outline',
            dataset: { dialogInitialFocus: '' },
            onClick: cancel,
          },
          [props.t('取消')]
        )
      )
      footer.appendChild(
        el(
          'button',
          {
            type: 'button',
            id: 'btn-apply-provider-parameters',
            className: 'comic-btn comic-btn--cta',
            onClick: () => applyParameterDialog(dialog, confirm),
          },
          [props.t('保存并应用')]
        )
      )
    },
  })
  syncParameterDialogButton(true)
}

function applyParameterDialog(dialog: ParameterDialogState, confirm: () => void) {
  if (destroyed || parameterDialog !== dialog) return
  if (
    !formAvailable.value ||
    activeCandidate !== dialog.candidate ||
    selectedModel.value !== dialog.candidate
  ) {
    closeParameterDialog()
    props.toast.warning(props.t('模型已变化，请重新打开参数弹窗。'))
    return
  }
  try {
    dialog.editor!.overrides()
  } catch (error) {
    if (dialog.feedback) {
      dialog.feedback.textContent = (error as Error).message
      dialog.feedback.classList.add('parameter-dialog-feedback--error')
    }
    return
  }
  const overrides = dialog.editor!.overrides({ validate: false, includeHidden: true })
  settingsEditor.render(dialog.fields, overrides)
  updateGenerationConditions()
  updateReferenceCounter()
  rememberParameters()
  confirm()
  props.toast.success(props.t('已应用临时生成参数。'))
}

function closeParameterDialog() {
  if (parameterDialog) props.modal.close(false)
}

// ---------- 模式与批量 ----------
function switchMode(next: 'single' | 'batch') {
  mode.value = next
  if (next === 'batch' && batchItems.length === 0) addBatchItem()
  updateActionState()
}

function addBatchItem(initialName = '', initialPrompt = '', initialCount = 1) {
  if (batchItems.length >= batchMaxTasks.value) {
    props.toast.warning(props.t('批量任务数不能超过 {v0}', { v0: batchMaxTasks.value }))
    return
  }
  batchCounter.value++
  const itemId = `batch_item_${batchCounter.value}`
  const name = initialName || props.t('子任务 {v0}', { v0: batchCounter.value })
  batchItems.push({
    id: itemId,
    name,
    prompt: initialPrompt,
    image_count: initialCount,
  })
  updateActionState()
}

function removeBatchItem(itemId: string) {
  const index = batchItems.findIndex(item => item.id === itemId)
  if (index >= 0) batchItems.splice(index, 1)
  updateActionState()
}

function setBatchCount(item: BatchItem, value: number) {
  item.image_count = Math.max(1, Math.min(10, Number.isFinite(value) ? value : 1))
  updateActionState()
}

// ---------- 参考图托盘 ----------
function isLocalBlob(item: TrayItem) {
  return item.type === 'upload' && typeof item.previewUrl === 'string' && item.previewUrl.startsWith('blob:')
}

// 与 SafeDOM.isSafeImageName 完全一致的判定（. / .. 拒绝 + 受控字符集）
const SAFE_IMAGE_NAME_REGEX = /^[A-Za-z0-9_.-]+$/

function safeImageName(name: unknown): name is string {
  return (
    typeof name === 'string' && name !== '.' && name !== '..' && SAFE_IMAGE_NAME_REGEX.test(name)
  )
}

function refreshTrayThumbs() {
  for (const img of Array.from(
    document.querySelectorAll<HTMLImageElement>('[data-tray-thumb]')
  )) {
    if (img.dataset.attached) continue
    img.dataset.attached = '1'
    props.installImageFallback(img, img.parentElement as HTMLElement, props.t('图片加载失败或已清理'))
    props.imageLoader.attach(img, img.dataset.trayThumb as string, { thumb: true })
  }
}

watch(tray, () => void Promise.resolve().then(refreshTrayThumbs), { flush: 'post' })

function removeReferenceItem(index: number) {
  const item = tray[index]
  if (item && isLocalBlob(item)) {
    try {
      URL.revokeObjectURL(item.previewUrl)
    } catch {
      /* ignore */
    }
  }
  tray.splice(index, 1)
  updateReferenceCounter()
}

async function handleFileUpload(event: Event) {
  if (isUploading.value) return
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (files.length === 0) return

  const max = getMaxReferenceImages()
  const current = tray.length
  const remaining = max - current

  if (remaining <= 0) {
    props.toast.warning(props.t('参考图数量已达上限'))
    return
  }

  isUploading.value = true
  updateReferenceCounter()
  try {
    const toUpload = files.slice(0, remaining)
    if (files.length > toUpload.length) {
      props.toast.warning(
        props.t('仅上传前 {v0} 张，参考图数量不能超过 {v1} 张', { v0: toUpload.length, v1: max })
      )
    }
    for (const file of toUpload) {
      if (file.type && !['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp'].includes(file.type)) {
        props.toast.error(props.t('文件 {v0} 不是支持的图片格式', { v0: file.name }))
        continue
      }
      if (file.size > uploadMaxMb.value * 1024 * 1024) {
        props.toast.error(
          props.t('文件 {v0} 大小超过 {v1}MB 限制', { v0: file.name, v1: uploadMaxMb.value })
        )
        continue
      }

      try {
        props.toast.info(props.t('正在上传 {v0}...', { v0: file.name }))
        const res = (await props.bridge.upload('webui/upload', file)) as {
          names?: unknown[]
        } | null
        const names = Array.isArray(res?.names)
          ? (res!.names as unknown[]).filter(name => safeImageName(name))
          : []
        if (names.length === 0) {
          throw new Error(props.t('上传接口未返回有效图片文件名'))
        }
        for (const name of names) {
          if (tray.length >= max) break
          const blobUrl = URL.createObjectURL(file)
          tray.push({
            id: `ref_${Date.now()}_${Math.random()}`,
            type: 'upload',
            name,
            previewUrl: blobUrl,
            file,
          })
        }
      } catch (err) {
        console.error('[WorkbenchView] 上传参考图失败:', err)
        props.errorFeedback.show(err, props.t('上传失败'))
      }
    }
  } finally {
    isUploading.value = false
    updateReferenceCounter()
  }
}

async function openGalleryPicker() {
  const max = getMaxReferenceImages()
  const current = tray.length
  const remaining = max - current
  if (remaining <= 0) {
    props.toast.warning(props.t('参考图数量已达上限'))
    return
  }

  try {
    const historyData = (await props.bridge.get('webui/history', {
      page: 1,
      size: 24,
    })) as { items?: { job_id: string; prompt?: string; images?: unknown[] }[] } | null
    const items = Array.isArray(historyData?.items) ? historyData!.items : []

    const imageItems: { job_id: string; imageName: string; prompt?: string }[] = []
    for (const job of items) {
      if (Array.isArray(job.images)) {
        for (const imgName of job.images) {
          if (!safeImageName(imgName)) continue
          if (tray.some(entry => entry.type === 'gallery' && entry.name === imgName)) continue
          imageItems.push({ job_id: job.job_id, imageName: imgName, prompt: job.prompt })
        }
      }
    }

    if (imageItems.length === 0) {
      props.toast.info(props.t('历史画廊中暂无可用图片'))
      return
    }

    const selectedNames = new Set<string>()

    props.modal.openCustom({
      title: props.t('从历史画廊拾取参考图 (最多可选 {v0} 张)', { v0: remaining }),
      renderBody: container => {
        const grid = el('div', {
          style: {
            display: 'grid',
            'grid-template-columns': 'repeat(auto-fill, minmax(100px, 1fr))',
            gap: '10px',
            'max-height': '360px',
            'overflow-y': 'auto',
            padding: '4px',
          },
        })
        imageItems.forEach((item, index) => {
          const card = el('button', {
            type: 'button',
            className: 'gallery-picker-item',
            'aria-label': props.t('选择第 {v0} 张参考图', { v0: index + 1 }),
            'aria-pressed': 'false',
          })
          const img = el('img', {
            alt: props.t('历史参考图'),
            style: { width: '100%', height: '100%', objectFit: 'cover' },
          }) as HTMLImageElement
          props.installImageFallback(img, card, props.t('图片加载失败或已清理'))
          props.imageLoader.attach(img, item.imageName, { thumb: true })

          const checkBadge = el('span', {
            style: {
              position: 'absolute',
              top: '4px',
              right: '4px',
              width: '18px',
              height: '18px',
              backgroundColor: 'var(--pop)',
              border: '1px solid var(--ink)',
              display: 'none',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '11px',
              fontWeight: 'bold',
            },
          })
          checkBadge.innerHTML = props.icons.check || ''

          card.addEventListener('click', () => {
            if (selectedNames.has(item.imageName)) {
              selectedNames.delete(item.imageName)
              card.style.borderColor = 'var(--ink)'
              checkBadge.style.display = 'none'
              card.setAttribute('aria-pressed', 'false')
            } else {
              if (selectedNames.size >= remaining) {
                props.toast.warning(props.t('单次最多还能选择 {v0} 张参考图', { v0: remaining }))
                return
              }
              selectedNames.add(item.imageName)
              card.style.borderColor = 'var(--pop)'
              checkBadge.style.display = 'flex'
              card.setAttribute('aria-pressed', 'true')
            }
          })

          card.appendChild(img)
          card.appendChild(checkBadge)
          grid.appendChild(card)
        })
        container.appendChild(grid)
      },
      renderFooter: (footer, onConfirm, onCancel) => {
        footer.appendChild(
          el('button', { type: 'button', className: 'comic-btn comic-btn--outline', onClick: onCancel }, [
            props.t('取消'),
          ])
        )
        footer.appendChild(
          el(
            'button',
            {
              type: 'button',
              className: 'comic-btn comic-btn--cta',
              onClick: () => {
                for (const name of selectedNames) {
                  tray.push({
                    id: `ref_${Date.now()}_${Math.random()}`,
                    type: 'gallery',
                    name,
                    previewUrl: '',
                  })
                }
                updateReferenceCounter()
                onConfirm()
              },
            },
            [props.t('确认添加选中项')]
          )
        )
      },
    })
  } catch (err) {
    console.error('[WorkbenchView] 获取画廊列表失败:', err)
    props.errorFeedback.show(err, props.t('获取画廊图片失败'))
  }
}

function addGalleryReference(imageName: string): boolean {
  if (!formAvailable.value) {
    props.toast.warning(props.t('当前没有可用的图像生成供应商'))
    return false
  }
  if (!safeImageName(imageName)) {
    props.toast.error(props.t('图片文件名无效，无法设为参考图'))
    return false
  }
  if (tray.some(item => item.type === 'gallery' && item.name === imageName)) {
    props.toast.info(props.t('该图片已在参考图托盘中'))
    return false
  }
  const max = getMaxReferenceImages()
  if (tray.length >= max) {
    props.toast.warning(props.t('参考图数量已达上限'))
    return false
  }
  tray.push({
    id: `ref_${Date.now()}_${Math.random()}`,
    type: 'gallery',
    name: imageName,
    previewUrl: '',
  })
  updateReferenceCounter()
  props.toast.success(props.t('已添加到参考图托盘'))
  return true
}

function restoreReferences(names: unknown[]): number {
  for (const item of tray) {
    if (item.type === 'upload' && item.previewUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(item.previewUrl)
    }
  }
  const available = [...new Set(names.filter(name => safeImageName(name)))] as string[]
  tray.splice(0, tray.length, ...available.slice(0, getMaxReferenceImages()).map(name => ({
    id: `ref_${Date.now()}_${Math.random()}`,
    type: 'gallery' as const,
    name,
    previewUrl: '',
  })))
  updateReferenceCounter()
  return tray.length
}

// ---------- 回填 ----------
function applyTemplate(promptText: string, template: AnyRecord = {}) {
  // 模板默认值视为显式覆盖：与提示词一起回填工作台。
  refillParameters(promptText || '', {
    resolution: template.defaults?.resolution || undefined,
    aspect_ratio: template.defaults?.aspect_ratio || undefined,
  })
}

function refillParameters(promptText: string, params: AnyRecord = {}) {
  rememberParameters()
  switchMode('single')
  prompt.value = promptText || ''
  let fellBack = false
  if (params.provider || params.model || params.candidate_id) {
    const matchIndex = models.value.findIndex(
      entry =>
        (!params.provider || entry.provider === params.provider) &&
        (!params.model || entry.model === params.model) &&
        (!params.candidate_id || entry.candidate_id === params.candidate_id)
    )
    if (matchIndex >= 0) modelIndex.value = matchIndex
    else fellBack = true
  }
  handleModelChange({ remember: false, restore: false })
  const fields = selectedModel.value?.generation_fields || {}
  const overrides = { ...(params.generation_settings || {}) } as Record<string, unknown>
  for (const name of ['quality', 'seed', 'negative_prompt', 'watermark']) {
    if (params[name] === null || params[name] === undefined) continue
    const setting = Object.entries(fields).find(([, field]) => field.runtime_parameter === name)
    if (setting) {
      if (!Object.prototype.hasOwnProperty.call(overrides, setting[0])) overrides[setting[0]] = params[name]
    } else {
      fellBack = true
    }
  }
  for (const [name] of [['resolution'], ['aspect_ratio']] as [string][]) {
    const value = params[name] ?? overrides[name]
    delete overrides[name]
    if (value === null || value === undefined) continue
    const choices = name === 'resolution' ? resolutions.value : aspectRatios.value
    if (choices.includes(String(value))) {
      if (name === 'resolution') resolution.value = String(value)
      else aspectRatio.value = String(value)
    } else {
      fellBack = true
    }
  }
  if (settingsEditor.render(fields, overrides)) fellBack = true
  if (params.image_count) {
    const maximum = imageCountMax.value || 10
    const count = Math.min(Math.max(Number(params.image_count) || 1, 1), maximum)
    imageCount.value = count
    if (count !== Number(params.image_count)) fellBack = true
  }
  syncParameterDialogButton(settingsEditor.controls.size > 0)
  updateGenerationConditions()
  updateReferenceCounter()
  rememberParameters()
  document.getElementById('input-prompt')?.focus()
  if (fellBack) props.toast.warning(props.t('已回填可用参数；部分历史设置已失效或不受当前模型支持'))
  else props.toast.info(props.t('已回填历史任务参数'))
}

// ---------- 提交 ----------
async function submit() {
  if (parameterDialog || isSubmitting.value || !formAvailable.value) return
  updateActionState()
  updateReferenceCounter()
  if (!batchBudgetValid.value || !referencesValid.value || isUploading.value) return
  const isBatch = mode.value === 'batch'
  const promptVal = prompt.value.trim()
  let batchPayload: { name: string; prompt: string; image_count: number }[] | null = null

  if (!isBatch) {
    if (!promptVal) {
      props.toast.warning(props.t('请输入提示词'))
      document.getElementById('input-prompt')?.focus()
      return
    }
  } else {
    if (batchItems.length === 0) {
      props.toast.warning(props.t('请至少添加一个批量子任务'))
      return
    }
    for (const item of batchItems) {
      if (!item.prompt || !item.prompt.trim()) {
        props.toast.warning(props.t('子任务 "{v0}" 缺少提示词', { v0: item.name }))
        return
      }
    }
    batchPayload = batchItems.map(item => ({
      name: item.name.trim() || props.t('子任务'),
      prompt: item.prompt.trim(),
      image_count: item.image_count || 1,
    }))
  }

  const uploadNames: string[] = []
  const referenceNames: string[] = []
  for (const item of tray) {
    if (item.type === 'upload') uploadNames.push(item.name)
    else referenceNames.push(item.name)
  }

  const entry = selectedModel.value
  const imageCountValue = Number(imageCount.value)
  const maximumCount = imageCountMax.value || 10
  if (!Number.isSafeInteger(imageCountValue) || imageCountValue < 1 || imageCountValue > maximumCount) {
    props.toast.warning(props.t('单任务张数必须是 1 到 {v0} 的整数', { v0: maximumCount }))
    document.getElementById('input-image-count')?.focus()
    return
  }
  let generationSettings: Record<string, unknown>
  try {
    generationSettings = settingsEditor.overrides()
  } catch (error) {
    openParameterDialog()
    props.toast.warning((error as Error).message)
    return
  }
  rememberParameters()
  const payload: Record<string, unknown> = {
    provider: entry?.provider || null,
    model: entry?.model || null,
    candidate_id: entry?.candidate_id || null,
    resolution: resolutionVisible.value ? resolution.value || null : null,
    aspect_ratio: aspectVisible.value ? aspectRatio.value || null : null,
    generation_settings: generationSettings,
    image_count: imageCountValue,
    reference_names: referenceNames,
    upload_names: uploadNames,
  }
  if (isBatch) {
    payload.batch = batchPayload
  } else {
    payload.prompt = promptVal
    payload.batch = null
  }

  isSubmitting.value = true
  updateReferenceCounter()

  try {
    const res = (await props.bridge.post('webui/generate', payload)) as {
      warning?: string
      job_id?: string
    } | null
    if (res?.warning) {
      props.toast.warning(res.warning, 5000)
    }
    props.toast.success(props.t('任务已成功提交入队'))
    props.onJobSubmitted?.(res?.job_id)
  } catch (err) {
    console.error('[WorkbenchView] 提交生成任务失败:', err)
    props.errorFeedback.show(err, props.t('提交失败'))
  } finally {
    isSubmitting.value = false
    updateReferenceCounter()
  }
}

onBeforeUnmount(() => {
  destroyed = true
  closeParameterDialog()
  for (const item of tray) {
    if (item.type === 'upload' && typeof item.previewUrl === 'string' && item.previewUrl.startsWith('blob:')) {
      URL.revokeObjectURL(item.previewUrl)
    }
  }
})

// ---------- 弹窗内 DOM 构建小工具 ----------
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, unknown> = {},
  children: (Node | string | null | undefined)[] = []
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  for (const [key, val] of Object.entries(attrs)) {
    if (val === undefined || val === null) continue
    if (key === 'className') node.className = String(val)
    else if (key === 'onClick' && typeof val === 'function') {
      node.addEventListener('click', val as EventListener)
    } else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(val as Record<string, unknown>)) {
        node.dataset[dataKey] = String(dataValue ?? '')
      }
    } else if (key === 'style' && typeof val === 'object') {
      for (const [styleKey, styleValue] of Object.entries(val as Record<string, unknown>)) {
        node.style.setProperty(styleKey.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`), String(styleValue))
      }
    } else node.setAttribute(key, String(val))
  }
  for (const child of children) {
    if (child === null || child === undefined) continue
    node.append(child)
  }
  return node
}

defineExpose({
  loadCapabilities,
  showGlobalBanner: showBanner,
  setFormDisabled,
  applyTemplate,
  refillParameters,
  addGalleryReference,
  restoreReferences,
})
</script>

<template>
  <div class="workbench-view">
    <div v-show="bannerVisible" class="comic-banner banner-container" role="alert">{{ bannerText }}</div>

    <div class="composer">
      <!-- 模式切换 -->
      <div class="mode-switcher" role="radiogroup" :aria-label="t('生成模式')">
        <button
          id="mode-btn-single"
          type="button"
          class="mode-btn"
          :class="{ 'mode-btn--active': mode === 'single' }"
          role="radio"
          :aria-checked="mode === 'single' ? 'true' : 'false'"
          :tabindex="mode === 'single' ? 0 : -1"
          :disabled="!formAvailable"
          @click="switchMode('single')"
          @keydown.arrow-left.prevent="switchMode('single')"
          @keydown.arrow-right.prevent="switchMode('batch')"
        >
          {{ t('单任务') }}
        </button>
        <button
          id="mode-btn-batch"
          type="button"
          class="mode-btn"
          :class="{ 'mode-btn--active': mode === 'batch' }"
          role="radio"
          :aria-checked="mode === 'batch' ? 'true' : 'false'"
          :tabindex="mode === 'batch' ? 0 : -1"
          :disabled="!formAvailable"
          @click="switchMode('batch')"
        >
          {{ t('批量任务') }}
        </button>
      </div>

      <!-- 提示词 -->
      <div class="form-item">
        <label for="input-prompt" class="form-label">{{ t('提示词') }}</label>
        <textarea
          id="input-prompt"
          v-model="prompt"
          class="comic-input comic-textarea"
          rows="5"
          maxlength="2000"
          :placeholder="t('描述你想生成的画面，越具体越好...')"
          :disabled="!formAvailable"
        ></textarea>
        <p id="prompt-char-count" class="char-count" :class="{ 'char-count--overflow': promptOverflow }">
          {{ charCountText }}
        </p>
      </div>

      <!-- 单任务表单 -->
      <div v-show="mode === 'single'" id="single-form-container" style="display: block;">
        <div class="params-grid">
          <div class="form-item">
            <label for="select-model" class="form-label">{{ t('供应商与模型') }}</label>
            <select
              id="select-model"
              v-model.number="modelIndex"
              class="comic-select"
              :aria-label="t('供应商与模型')"
              :disabled="!formAvailable"
              @change="handleModelChange()"
            >
              <optgroup
                v-for="group in modelGroups"
                :key="group.label"
                :label="group.label"
              >
                <option v-for="item in group.items" :key="item.index" :value="item.index">
                  {{ item.text }}
                </option>
              </optgroup>
            </select>
          </div>
          <div v-show="resolutionVisible" id="item-resolution" class="form-item">
            <label for="select-resolution" class="form-label">{{ t('分辨率') }}</label>
            <select
              id="select-resolution"
              v-model="resolution"
              class="comic-select"
              :aria-label="t('分辨率')"
              :disabled="!formAvailable"
              @change="rememberParameters()"
            >
              <option value="">{{ t('使用供应商配置') }}</option>
              <option v-for="choice in resolutions" :key="choice" :value="choice">{{ choice }}</option>
            </select>
          </div>
          <div v-show="aspectVisible" id="item-aspect-ratio" class="form-item">
            <label for="select-aspect-ratio" class="form-label">{{ t('画面比例') }}</label>
            <select
              id="select-aspect-ratio"
              v-model="aspectRatio"
              class="comic-select"
              :aria-label="t('画面比例')"
              :disabled="!formAvailable"
              @change="rememberParameters()"
            >
              <option value="">{{ t('使用供应商配置') }}</option>
              <option v-for="choice in aspectRatios" :key="choice" :value="choice">{{ choice }}</option>
            </select>
          </div>
          <div class="form-item">
            <label for="input-image-count" class="form-label">{{ t('生成张数') }}</label>
            <input
              id="input-image-count"
              v-model.number="imageCount"
              type="number"
              class="comic-input"
              min="1"
              :max="imageCountMax"
              step="1"
              :aria-label="t('生成张数')"
              :disabled="!formAvailable"
              @change="rememberParameters()"
            >
          </div>
        </div>

        <details
          v-show="moreParamsVisible"
          id="advanced-params-disclosure"
          class="advanced-params"
          :open="moreParamsExpanded"
        >
          <summary id="btn-toggle-more-params" :aria-expanded="moreParamsExpanded ? 'true' : 'false'">
            {{ t('更多生成参数') }}
            <span id="provider-override-count" class="meta-pill">{{ overrideCountText }}</span>
          </summary>
          <p class="field-hint">
            <span>{{ t('在弹窗中调整，保存后应用。') }}</span>
            <span id="preferences-save-status" role="status">{{ prefsStatus }}</span>
          </p>
          <button
            type="button"
            class="comic-btn comic-btn--outline"
            :disabled="!formAvailable"
            @click="openParameterDialog"
          >
            {{ t('打开参数面板') }}
          </button>
        </details>
      </div>

      <!-- 批量表单 -->
      <div v-show="mode === 'batch'" id="batch-form-container" style="display: block;">
        <div class="batch-toolbar">
          <button
            id="btn-add-batch-item"
            type="button"
            class="comic-btn comic-btn--outline"
            :disabled="addBatchDisabled"
            @click="addBatchItem()"
          >
            {{ t('添加子任务') }}
          </button>
          <p id="batch-budget-info" class="field-hint batch-budget-indicator" :class="{ 'batch-budget-indicator--danger': batchBudgetDanger }">
            <span>{{ t('共 {v0} 个子任务 · 合计 {v1} 张，预算上限 {v2} 张', { v0: batchItems.length, v1: batchTotal, v2: batchBudgetLimit }) }}</span>
            <span id="batch-total-count" hidden>{{ batchTotal }}</span>
            <span id="batch-max-budget" hidden>{{ batchBudgetLimit }}</span>
          </p>
        </div>
        <div id="batch-items-list" class="batch-items-list">
          <div v-for="item in batchItems" :id="`row-${item.id}`" :key="item.id" class="batch-item-row">
            <div class="batch-item-head">
              <input
                v-model="item.name"
                type="text"
                class="comic-input batch-item-name"
                maxlength="64"
                :placeholder="t('子任务名称')"
                :aria-label="t('子任务名称')"
              >
              <div class="batch-item-count-wrap">
                {{ t('张数:') }}
                <input
                  type="number"
                  class="comic-input batch-item-count"
                  min="1"
                  max="10"
                  :value="String(item.image_count)"
                  :aria-label="t('张数:')"
                  @change="setBatchCount(item, Number(($event.target as HTMLInputElement).value))"
                >
              </div>
              <button
                type="button"
                class="comic-btn comic-btn--danger comic-btn--sm"
                :aria-label="t('删除子任务')"
                @click="removeBatchItem(item.id)"
              >
                <span class="btn-icon" aria-hidden="true" v-html="icons.trash"></span>
              </button>
            </div>
            <textarea
              v-model="item.prompt"
              class="comic-input comic-textarea"
              rows="2"
              maxlength="2000"
              :placeholder="t('子任务提示词...')"
              :aria-label="t('子任务提示词...')"
            ></textarea>
          </div>
        </div>
      </div>

      <!-- 参考图托盘 -->
      <div class="reference-tray">
        <div class="tray-head">
          <span class="form-label">{{ t('参考图') }}</span>
          <span id="reference-counter" class="tray-counter" :class="{ 'tray-counter--danger': trayCounterDanger }">
            {{ trayCountText }}
          </span>
          <span id="upload-max-size" class="field-hint">{{ uploadMaxMb }}MB</span>
          <input
            ref="fileInput"
            type="file"
            class="visually-hidden"
            multiple
            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
            aria-hidden="true"
            tabindex="-1"
            @change="handleFileUpload"
          >
          <button
            id="btn-trigger-upload"
            type="button"
            class="comic-btn comic-btn--sm comic-btn--outline"
            :disabled="uploadDisabled"
            @click="fileInput?.click()"
          >
            {{ t('上传本地图片') }}
          </button>
          <button
            id="btn-pick-gallery"
            type="button"
            class="comic-btn comic-btn--sm comic-btn--outline"
            :disabled="uploadDisabled"
            @click="openGalleryPicker"
          >
            {{ t('从画廊选取') }}
          </button>
        </div>
        <div id="reference-tray-grid" class="tray-grid">
          <p v-if="!tray.length" id="empty-tray-tip" class="tray-empty" style="display: flex;">
            {{ t('尚未添加参考图。上传本地图片，或从画廊选取历史作品。') }}
          </p>
          <div v-for="(item, index) in tray" :key="item.id" class="tray-item">
            <img
              v-if="isLocalBlob(item)"
              :src="item.previewUrl"
              :alt="t('参考图')"
            >
            <img v-else :alt="t('参考图')" :data-tray-thumb="safeImageName(item) ? item.name : undefined">
            <span class="tray-item-badge">{{ item.type === 'upload' ? t('本地') : t('画廊') }}</span>
            <button
              type="button"
              class="tray-item-remove"
              :aria-label="t('移除参考图')"
              @click="removeReferenceItem(index)"
            >
              <span class="btn-icon" aria-hidden="true" v-html="icons.x"></span>
            </button>
          </div>
        </div>
      </div>

      <!-- 提交 -->
      <button
        id="btn-submit-generate"
        type="button"
        class="comic-btn comic-btn--cta comic-btn--lg"
        :disabled="submitDisabled"
        @click="submit"
      >
        <span id="submit-btn-text">{{ isSubmitting ? t('正在提交任务...') : t('立即生成图像') }}</span>
      </button>
    </div>
  </div>
</template>
