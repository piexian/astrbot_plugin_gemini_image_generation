<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { BridgeClientLike, ModalLike, Translator } from '../provider-config/types'

interface RateConfig {
  enabled: boolean
  period_seconds: number | string
  max_requests: number | string
}

interface RateRule extends RateConfig {
  rule_name: string
  umos: string[]
  group_ids?: string[]
}

interface LimitsDraft {
  group_limit_mode: string
  group_limit_list: string[]
  global_rate_limit: RateConfig
  default_rate_limit: RateConfig
  rate_limit_rules: RateRule[]
}

interface SessionsPayload {
  sessions?: {
    umo: string
    display_name?: string
    platform: string
    message_type: string
    session_id: string
  }[]
  available?: boolean
  page?: number
  total?: number
  page_size?: number
  warning?: string
}

const props = defineProps<{
  bridge: BridgeClientLike
  modal: ModalLike
  t: Translator
}>()

// ---------- 状态 ----------
const draft = ref<LimitsDraft | null>(null)
let baseline = ''
const revision = ref<string | null>(null)
const loaded = ref(false)
const attempted = ref(false)
const loading = ref(false)
const saving = ref(false)
const conflict = ref(false)
const statusText = ref(props.t('打开此页签后加载配置。'))
const statusWarning = ref(false)
const migrationText = ref('')
const sessionNames = ref<Record<string, string>>({})
let requestId = 0
let pickerRequestId = 0
let destroyed = false

interface SessionRow {
  umo: string
  name: string
  platform: string
  message_type: string
  session_id: string
}

interface PickerState {
  index: number
  page: number
  search: string
  type: string
  platform: string
  manual: string
  feedback: string
  resultStatus: string
  pageLabel: string
  previousDisabled: boolean
  nextDisabled: boolean
  results: SessionRow[]
}

const picker = ref<PickerState | null>(null)
const groupListEl = ref<HTMLTextAreaElement | null>(null)

// ---------- 派生 ----------
const dirty = computed(
  () => draft.value !== null && JSON.stringify(draft.value) !== baseline
)
const unavailable = computed(() => !props.bridge.isAvailable?.())
const saveDisabled = computed(
  () =>
    !loaded.value || unavailable.value || loading.value || saving.value ||
    conflict.value || !dirty.value
)
const reloadDisabled = computed(() => loading.value || saving.value || unavailable.value)
const editorDisabled = computed(
  () => !loaded.value || loading.value || saving.value || unavailable.value
)
const dirtyText = computed(() =>
  dirty.value
    ? props.t('有未保存的草稿 · 切换页签会保留，关闭页面会丢失。')
    : loaded.value
      ? props.t('与服务器配置一致')
      : props.t('尚未加载可编辑配置')
)

const groupAccessNote = computed(() => {
  if (!draft.value) return ''
  const mode = draft.value.group_limit_mode
  const count = draft.value.group_limit_list.length
  const hint =
    mode === 'none'
      ? props.t('不限制群访问；切换模式会保留名单。')
      : mode === 'whitelist'
        ? count
          ? props.t('仅允许名单中的群使用，其他群静默拒绝。')
          : props.t('白名单为空，沿用原行为：所有群均可使用。')
        : count
          ? props.t('禁止名单中的群使用。')
          : props.t('黑名单为空，未禁止任何群。')
  return props.t('{v0} 已填 {v1} / 1000 个群号，每行一个。', { v0: hint, v1: count })
})
const groupListTitle = computed(() => {
  const mode = draft.value?.group_limit_mode
  return mode === 'whitelist'
    ? props.t('白名单群号')
    : mode === 'blacklist'
      ? props.t('黑名单群号')
      : props.t('群号列表（暂不生效）')
})

// ---------- 校验与响应处理 ----------
function validUmo(umo: unknown): boolean {
  if (typeof umo !== 'string' || [...umo].length > 1024 || /[\x00-\x1f\x7f]/.test(umo)) return false
  const match = umo.match(/^([^:]+):(GroupMessage|FriendMessage|OtherMessage):([\s\S]+)$/)
  return !!match && !!match[1]!.trim() && !!match[3]!.trim()
}

function notice(message: string, warning = false) {
  statusText.value = message
  statusWarning.value = warning
}

function adopt(payload: unknown) {
  const body = payload as {
    revision?: string
    limits?: Record<string, unknown>
    migration?: { message?: string; pending?: boolean; cooldown_until?: number }
  } | null
  const limits = body?.limits as Record<string, any> | undefined
  if (
    typeof body?.revision !== 'string' || !limits ||
    !limits.global_rate_limit || !limits.default_rate_limit ||
    !['none', 'whitelist', 'blacklist'].includes(limits.group_limit_mode) ||
    !Array.isArray(limits.group_limit_list) ||
    !limits.group_limit_list.every((id: unknown) => typeof id === 'string') ||
    !Array.isArray(limits.rate_limit_rules)
  ) {
    throw new Error('Invalid limits response')
  }
  // 只复制管理范围内字段，绝不回传服务端其它配置。
  const rate = (value: Record<string, unknown>): RateConfig => ({
    enabled: value.enabled as boolean,
    period_seconds: value.period_seconds as number,
    max_requests: value.max_requests as number,
  })
  const next: LimitsDraft = {
    group_limit_mode: limits.group_limit_mode,
    group_limit_list: [...(limits.group_limit_list as string[])],
    global_rate_limit: rate(limits.global_rate_limit),
    default_rate_limit: rate(limits.default_rate_limit),
    rate_limit_rules: (limits.rate_limit_rules as Record<string, any>[]).map(rule => {
      if (!Array.isArray(rule.umos) || (rule.group_ids !== undefined && !Array.isArray(rule.group_ids))) {
        throw new Error('Invalid rule response')
      }
      return {
        rule_name: rule.rule_name,
        ...rate(rule),
        umos: [...rule.umos],
        ...(rule.group_ids === undefined ? {} : { group_ids: [...rule.group_ids] }),
      }
    }),
  }
  draft.value = next
  revision.value = body!.revision!
  baseline = JSON.stringify(next)
  loaded.value = true
  conflict.value = false
  closePicker()
  const migration = body!.migration || {}
  const notices: string[] = []
  if (migration.message) notices.push(String(migration.message))
  else if (migration.pending) {
    notices.push(props.t('存在启用的未迁移规则，聊天请求已暂停；请完成迁移后保存。'))
  }
  if (Number(migration.cooldown_until) > Date.now() / 1000) {
    notices.push(
      props.t('冷却至 {v0}（服务器状态，以实际请求为准）。', {
        v0: new Date(migration.cooldown_until! * 1000).toLocaleString(),
      })
    )
  }
  migrationText.value = notices.join(' ')
  void nextTick().then(() => {
    const el = groupListEl.value
    if (el) el.value = next.group_limit_list.join('\n')
  })
}

async function load() {
  if (destroyed || loading.value || saving.value) return
  if (unavailable.value) {
    loaded.value = false
    notice(props.t('页面桥接不可用，请在 AstrBot Dashboard 中打开 Studio。'), true)
    return
  }
  const id = ++requestId
  loading.value = true
  closePicker()
  notice(props.t('正在加载服务器限流配置…'))
  try {
    const payload = await props.bridge.get('webui/limits')
    if (destroyed || id !== requestId) return
    adopt(payload)
    notice(props.t('配置已加载。修改后请显式保存。'))
  } catch (error) {
    if (destroyed || id !== requestId) return
    loaded.value = false
    console.warn('[LimitsView] 加载失败:', error)
    notice(props.t('限流配置加载失败，保存已禁用；现有草稿保留，请重新加载。'), true)
  } finally {
    if (!destroyed && id === requestId) loading.value = false
  }
}

async function loadOnce() {
  if (attempted.value || destroyed) return
  attempted.value = true
  await load()
}

function validate() {
  const current = draft.value!
  if (!['none', 'whitelist', 'blacklist'].includes(current.group_limit_mode)) {
    throw new Error(props.t('请选择群限制模式。'))
  }
  if (
    current.group_limit_list.length > 1000 ||
    current.group_limit_list.some(
      id => typeof id !== 'string' || !id.trim() || [...id].length > 1024 || /[\x00-\x1f\x7f]/.test(id)
    )
  ) {
    throw new Error(props.t('群号列表最多 1000 项，每项须为不含控制字符的非空群号，最多 1024 字符。'))
  }
  const rules = current.rate_limit_rules
  if (rules.length > 100) throw new Error(props.t('最多允许 100 条指定会话规则。'))
  const entries: [string, RateConfig][] = [
    [props.t('全局限流'), current.global_rate_limit],
    [props.t('默认会话限流'), current.default_rate_limit],
    ...rules.map((rule, index) => [props.t('规则 {v0}', { v0: index + 1 }), rule] as [string, RateConfig]),
  ]
  for (const [name, rate] of entries) {
    if (typeof rate.enabled !== 'boolean') {
      throw new Error(props.t('{v0}：请选择启停状态。', { v0: name }))
    }
    for (const [key, max, label] of [
      ['period_seconds', 604800, props.t('周期')],
      ['max_requests', 10000, props.t('次数')],
    ] as [string, number, string][]) {
      const value = Number(rate[key as 'period_seconds'])
      if (!Number.isInteger(value) || value < 1 || value > max) {
        throw new Error(props.t('{v0}：{v1}须为 1–{v2} 的整数。', { v0: name, v1: label, v2: max }))
      }
    }
  }
  rules.forEach((rule, index) => {
    if (typeof rule.rule_name !== 'string' || !rule.rule_name.trim() || [...rule.rule_name].length > 100) {
      throw new Error(props.t('规则 {v0}：名称须为 1–100 个字符。', { v0: index + 1 }))
    }
    if (rule.umos.length > 500 || !rule.umos.every(validUmo)) {
      throw new Error(
        props.t('规则 {v0}：最多 500 个完整 UMO，每个不超过 1024 字符；请检查格式。', { v0: index + 1 })
      )
    }
    if (rule.enabled && rule.group_ids?.length) {
      throw new Error(
        props.t('规则 {v0}：启用的旧群号规则尚未迁移，聊天请求将保持暂停。请选择非空 UMO 并点击“确认迁移”，或明确停用此规则后保存。', {
          v0: index + 1,
        })
      )
    }
  })
}

async function save() {
  if (destroyed) return
  if (saveDisabled.value) return
  try {
    validate()
  } catch (error) {
    notice((error as Error).message, true)
    return
  }
  const limits = JSON.parse(JSON.stringify(draft.value)) as LimitsDraft
  for (const rate of [limits.global_rate_limit, limits.default_rate_limit, ...limits.rate_limit_rules]) {
    rate.period_seconds = Number(rate.period_seconds)
    rate.max_requests = Number(rate.max_requests)
  }
  const id = ++requestId
  saving.value = true
  closePicker()
  notice(props.t('正在保存…'))
  try {
    const payload = await props.bridge.post('webui/limits', { revision: revision.value, limits })
    if (destroyed || id !== requestId) return
    adopt(payload)
    notice(props.t('限流配置已保存。'))
  } catch (error) {
    if (destroyed || id !== requestId) return
    console.warn('[LimitsView] 保存失败:', error)
    const err = error as { status?: unknown; statusCode?: unknown; status_code?: unknown; code?: unknown; response?: { status?: unknown }; message?: string }
    const status = Number(err?.status ?? err?.statusCode ?? err?.status_code ?? err?.code ?? err?.response?.status)
    conflict.value =
      status === 409 || /\b409\b|conflict|revision|配置.*(?:变更|冲突|过期|更新)/i.test(String(err?.message || ''))
    notice(
      conflict.value
        ? props.t('服务器配置已变更（409），草稿完整保留。请先核对草稿，再点击“重新加载服务器配置”；不会自动重试或覆盖。')
        : props.t('保存失败，草稿已保留。请检查规则、迁移状态与连接后重试。'),
      true
    )
  } finally {
    if (!destroyed && id === requestId) saving.value = false
  }
}

async function reload() {
  if (loading.value || saving.value) return
  const confirmed = await props.modal.confirm({
    title: props.t('重新加载服务器配置？'),
    content: dirty.value
      ? props.t('当前未保存草稿将被丢弃，以服务器配置替换。请先核对或复制需要保留的内容。')
      : props.t('将重新读取服务器配置。'),
    confirmText: props.t('确认重新加载'),
    danger: dirty.value,
  })
  if (confirmed && !destroyed) await load()
}

// ---------- 会话选择器 ----------
function pickerNotice(message: string) {
  if (picker.value) picker.value.feedback = message
  else notice(message, true)
}

function closePicker(restoreFocus = false) {
  ++pickerRequestId
  if (!picker.value) return
  const index = picker.value.index
  picker.value = null
  if (restoreFocus) {
    ;(
      document.querySelector(
        `[data-limits-action="choose-sessions"][data-index="${index}"]`
      ) as HTMLElement | null
    )?.focus()
  }
}

function openPicker(index: number) {
  if (picker.value?.index === index) {
    closePicker(true)
    return
  }
  closePicker()
  picker.value = {
    index,
    page: 1,
    search: '',
    type: 'all',
    platform: '',
    manual: '',
    feedback: '',
    resultStatus: props.t('正在加载会话…'),
    pageLabel: '',
    previousDisabled: true,
    nextDisabled: true,
    results: [],
  }
  void nextTick().then(() => {
    ;(document.querySelector('[data-limits-search-input]') as HTMLElement | null)?.focus()
  })
  void searchSessions(1)
}

function addUmos(rule: RateRule, values: string[]): boolean {
  if (!values.every(validUmo)) {
    pickerNotice(
      props.t('UMO 格式无效：需要 Platform:GroupMessage|FriendMessage|OtherMessage:非空会话段，每个最多 1024 字符。不会按群号拼接。')
    )
    return false
  }
  const merged = [...new Set([...rule.umos, ...values])]
  if (merged.length > 500) {
    pickerNotice(props.t('每条规则最多选择 500 个 UMO，本次添加未生效。'))
    return false
  }
  rule.umos = merged
  pickerNotice(props.t('已填入草稿，尚未保存。翻页或筛选不会丢失已选会话。'))
  return true
}

async function searchSessions(page: number) {
  const current = picker.value
  if (!current || destroyed || editorDisabled.value) return
  const id = ++pickerRequestId
  current.previousDisabled = true
  current.nextDisabled = true
  current.results = []
  current.resultStatus = props.t('正在加载会话…')
  const params = {
    page: String(page),
    page_size: '20',
    search: current.search.trim(),
    message_type: current.type || 'all',
    platform: current.platform.trim(),
  }
  try {
    const payload = (await props.bridge.get('webui/sessions', params)) as SessionsPayload | null
    if (destroyed || picker.value !== current || id !== pickerRequestId) return
    if (!Array.isArray(payload?.sessions) || typeof payload?.available !== 'boolean') {
      throw new Error('Invalid sessions response')
    }
    current.page = Number(payload!.page) || page
    const total = Math.max(0, Number(payload!.total) || 0)
    const pageSize = Math.max(1, Number(payload!.page_size) || 20)
    const pages = Math.max(1, Math.ceil(total / pageSize))
    current.pageLabel = props.t('第 {v0} / {v1} 页 · 共 {v2} 个', {
      v0: current.page, v1: pages, v2: total,
    })
    current.resultStatus = [
      payload!.warning,
      !payload!.available
        ? props.t('会话列表暂不可用，已选项保留；仍可手动粘贴完整 UMO。')
        : !payload!.sessions!.length
          ? props.t('没有匹配的会话。')
          : props.t('勾选即填入草稿；名称仅供辨认，以完整 UMO 为准。'),
    ].filter(Boolean).join(' ')
    const rule = draft.value!.rate_limit_rules[current.index]
    if (payload!.available) {
      const names = { ...sessionNames.value }
      current.results = payload!.sessions!.flatMap(session => {
        if (!validUmo(session.umo)) return []
        names[session.umo] = String(session.display_name || props.t('未命名会话'))
        return [{
          umo: session.umo,
          name: String(session.display_name || props.t('未命名会话')),
          platform: session.platform,
          message_type: session.message_type,
          session_id: session.session_id,
        }]
      })
      sessionNames.value = names
      current.previousDisabled = current.page <= 1
      current.nextDisabled = current.page >= pages
    }
    // 已选勾选状态由模板按 rule.umos 计算，无需手动刷新。
    void rule
  } catch (error) {
    if (destroyed || picker.value !== current || id !== pickerRequestId) return
    console.warn('[LimitsView] 会话列表加载失败:', error)
    current.resultStatus = props.t('会话列表加载失败，已选项保留。可重新搜索或手动粘贴完整 UMO。')
  }
}

// ---------- 事件路由 ----------
function handleInput(event: Event) {
  if (destroyed || editorDisabled.value) return
  const node = event.target as HTMLElement
  if (node.dataset.accessField === 'group_limit_list') {
    const value = (node as HTMLTextAreaElement).value
    draft.value!.group_limit_list = [
      ...new Set(value.split(/\r?\n/).map(id => id.trim()).filter(Boolean)),
    ]
    return
  }
  const scope = node.dataset.scope
  const field = node.dataset.field
  if (scope === undefined || !['rule_name', 'period_seconds', 'max_requests'].includes(field as string)) return
  const rate = (draft.value as unknown as Record<string, RateConfig>)[scope] ||
    draft.value!.rate_limit_rules[Number(scope)]
  if (!rate) return
  const value = (node as HTMLInputElement).value
  ;(rate as unknown as Record<string, number | string>)[field as string] =
    field !== 'rule_name' && value !== '' && Number.isFinite(Number(value)) ? Number(value) : value
}

function handleChange(event: Event) {
  if (destroyed || editorDisabled.value) return
  const input = event.target as HTMLInputElement & { dataset: Record<string, string> }
  if (input.dataset.accessField === 'group_limit_mode') {
    draft.value!.group_limit_mode = input.value
    return
  }
  if (input.dataset.field === 'enabled') {
    const scope = input.dataset.scope
    if (!scope) return
    const rate = (draft.value as unknown as Record<string, RateConfig>)[scope] ||
      draft.value!.rate_limit_rules[Number(scope)]
    if (rate) rate.enabled = input.checked
  } else if (input.dataset.sessionUmo !== undefined && picker.value) {
    const rule = draft.value?.rate_limit_rules[picker.value.index]
    const umo = input.dataset.sessionUmo
    if (!rule || !umo) return
    if (input.checked) {
      if (!addUmos(rule, [umo])) input.checked = false
    } else {
      rule.umos = rule.umos.filter(item => item !== umo)
    }
  } else if (input.dataset.sessionFilter !== undefined) {
    void searchSessions(1)
  }
}

function handleSearchSubmit(event: Event) {
  const form = event.target as HTMLElement
  if (form.dataset.limitsSearch === undefined) return
  void searchSessions(1)
}

async function handleClick(event: Event) {
  if (destroyed) return
  const target = event.target as HTMLElement
  const button = target.closest('button') as HTMLButtonElement | null
  if (!button || button.disabled) return
  const action = button.dataset.limitsAction
  if (action === 'save') {
    await save()
    return
  }
  if (action === 'reload') {
    await reload()
    return
  }
  if (editorDisabled.value) return
  const index = Number(button.dataset.index)
  const rules = draft.value!.rate_limit_rules
  const rule = rules[index] as RateRule | undefined
  if (action === 'choose-sessions') {
    openPicker(index)
    return
  }
  if (action === 'close-picker') {
    closePicker(true)
    return
  }
  if (action === 'previous-sessions' || action === 'next-sessions') {
    if (picker.value) await searchSessions(picker.value.page + (action === 'next-sessions' ? 1 : -1))
    return
  }
  if (action === 'paste-umos' && picker.value) {
    const values = picker.value.manual.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
    if (!values.length) {
      pickerNotice(props.t('请每行粘贴一个完整 UMO。'))
      return
    }
    if (rule && addUmos(rule, values) && picker.value) picker.value.manual = ''
    return
  }
  if (action === 'remove-umo' && rule) {
    rule.umos = rule.umos.filter(umo => umo !== button.dataset.umo)
    void nextTick(() => {
      ;(
        document.querySelector(
          `[data-limits-action="choose-sessions"][data-index="${index}"]`
        ) as HTMLElement | null
      )?.focus()
    })
    return
  }
  if (action === 'add-rule' && rules.length < 100) {
    rules.push({
      rule_name: props.t('规则 {v0}', { v0: rules.length + 1 }),
      enabled: false, period_seconds: 60, max_requests: 5, umos: [],
    })
    return
  }
  if (action === 'delete-rule' && rule) {
    rules.splice(index, 1)
    return
  }
  if ((action === 'up-rule' || action === 'down-rule') && rule) {
    const next = index + (action === 'up-rule' ? -1 : 1)
    if (next < 0 || next >= rules.length) return
    const moved = rules[next]
    if (!moved || !rule) return
    rules[index] = moved
    rules[next] = rule
    return
  }
  if (action === 'migrate-rule' && rule?.group_ids?.length && rule.umos.length) {
    if (!rule.umos.every(validUmo) || rule.umos.length > 500) {
      notice(props.t('请先修正 UMO 格式与数量，再确认迁移。'), true)
      return
    }
    rule.group_ids = []
    notice(props.t('已在草稿中确认迁移，旧群号仅在保存成功后清除。'))
  }
}

// ---------- 生命周期 ----------
function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
})

onBeforeUnmount(() => {
  destroyed = true
  ++requestId
  closePicker()
  window.removeEventListener('beforeunload', handleBeforeUnload)
})

defineExpose({
  loadOnce,
  dirty,
  draft,
  loaded,
  saveDisabled,
  editorDisabled,
  sessionNames,
})
</script>

<template>
  <div
    @click="handleClick"
    @input="handleInput"
    @change="handleChange"
    @submit.prevent="handleSearchSubmit"
  >
    <div class="limits-toolbar">
      <div>
        <h2 class="card-title">{{ t('访问限制与生图限流') }}</h2>
        <p class="field-hint">{{ t('群限制模式、黑白名单与全局／会话限流统一管理、统一保存，不修改供应商配置。') }}</p>
      </div>
      <div class="limits-actions">
        <button
          type="button"
          id="btn-reload-limits"
          class="comic-btn comic-btn--outline"
          data-limits-action="reload"
          data-index=""
          :disabled="reloadDisabled"
        >
          {{ t('重新加载服务器配置') }}
        </button>
        <button
          type="button"
          id="btn-save-limits"
          class="comic-btn comic-btn--cta"
          data-limits-action="save"
          data-index=""
          :disabled="saveDisabled"
        >
          {{ saving ? t('保存中…') : t('保存限流配置') }}
        </button>
      </div>
    </div>
    <p id="limits-dirty" class="field-hint" role="status" aria-live="polite">{{ dirtyText }}</p>
    <p
      id="limits-status"
      class="limits-notice"
      :class="{ 'limits-notice--warning': statusWarning }"
      role="status"
      aria-live="polite"
    >
      {{ statusText }}
    </p>
    <p
      id="limits-migration"
      class="limits-notice limits-notice--warning"
      role="status"
      :hidden="!migrationText"
    >
      {{ migrationText }}
    </p>
    <fieldset id="limits-editor" class="limits-editor" :disabled="editorDisabled" :aria-label="t('限流配置草稿')">
      <template v-if="draft">
        <!-- 01 群限制与黑白名单 -->
        <section class="comic-card limits-card limits-card--access">
          <div class="section-heading">
            <span class="section-number">01</span>
            <h3 class="card-title">{{ t('群限制与黑白名单') }}</h3>
          </div>
          <div class="limits-access-fields">
            <label class="form-item">
              <span class="form-label">{{ t('群限制模式') }}</span>
              <select
                class="comic-select"
                :aria-label="t('群限制模式')"
                data-access-field="group_limit_mode"
                v-model="draft.group_limit_mode"
                @change="handleChange"
              >
                <option value="none">{{ t('不限制') }}</option>
                <option value="whitelist">{{ t('白名单') }}</option>
                <option value="blacklist">{{ t('黑名单') }}</option>
              </select>
            </label>
            <label class="form-item">
              <span class="form-label">{{ groupListTitle }}</span>
              <textarea
                ref="groupListEl"
                class="comic-input"
                rows="4"
                spellcheck="false"
                :placeholder="t('每行一个群号，不填写 UMO')"
                data-access-field="group_limit_list"
              ></textarea>
            </label>
          </div>
          <p class="field-hint" role="status">{{ groupAccessNote }}</p>
          <p class="field-hint">{{ t('按群号控制访问；私聊和 Studio 不受群名单影响，周期限流仍按 UMO 独立计数。') }}</p>
        </section>

        <!-- 02/03 限流卡片 -->
        <section
          v-for="[key, number, title, hint] in [
            ['global_rate_limit', '02', t('全局限流'), t('命令、LLM 与 Studio 生成共享此额度；会话规则不能绕过全局限制。')],
            ['default_rate_limit', '03', t('默认会话限流'), t('未命中指定规则的会话使用此额度，每个会话分别计数。')],
          ]"
          :key="key"
          class="comic-card limits-card"
        >
          <div class="section-heading">
            <span class="section-number">{{ number }}</span>
            <h3 class="card-title">{{ title }}</h3>
          </div>
          <p class="field-hint">{{ hint }}</p>
          <div class="limits-rate-fields">
            <label class="limits-check">
              <input
                type="checkbox"
                :data-scope="key"
                data-field="enabled"
                :checked="(draft as any)[key].enabled === true"
                @change="handleChange"
              >
              {{ t('启用') }}
            </label>
            <label class="form-item">
              <span class="form-label">{{ t('周期（秒）') }}</span>
              <input
                type="number"
                class="comic-input"
                min="1"
                max="604800"
                step="1"
                required
                :data-scope="key"
                data-field="period_seconds"
                :value="(draft as any)[key].period_seconds"
                @input="handleInput"
              >
            </label>
            <label class="form-item">
              <span class="form-label">{{ t('最大次数') }}</span>
              <input
                type="number"
                class="comic-input"
                min="1"
                max="10000"
                step="1"
                required
                :data-scope="key"
                data-field="max_requests"
                :value="(draft as any)[key].max_requests"
                @input="handleInput"
              >
            </label>
          </div>
        </section>

        <!-- 04 指定会话规则 -->
        <section class="comic-card limits-card limits-card--rules">
          <div class="section-heading">
            <span class="section-number">04</span>
            <h3 class="card-title">{{ t('指定会话规则') }}</h3>
          </div>
          <p class="field-hint">{{ t('按列表顺序匹配，首条命中的启用规则生效。最多 100 条，每条最多 500 个 UMO。') }}</p>
          <div class="limits-rules">
            <article
              v-for="(rule, index) in draft.rate_limit_rules"
              :key="index"
              class="limits-rule"
              :data-rule-index="index"
            >
              <div class="limits-toolbar">
                <h4>{{ t('规则 {v0}', { v0: index + 1 }) }}</h4>
                <div class="limits-actions">
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    data-limits-action="up-rule"
                    :data-index="index"
                    :disabled="index === 0"
                  >
                    {{ t('上移') }}
                  </button>
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    data-limits-action="down-rule"
                    :data-index="index"
                    :disabled="index === draft.rate_limit_rules.length - 1"
                  >
                    {{ t('下移') }}
                  </button>
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    data-limits-action="delete-rule"
                    :data-index="index"
                  >
                    {{ t('删除规则') }}
                  </button>
                </div>
              </div>
              <label class="form-item">
                <span class="form-label">{{ t('规则名称') }}</span>
                <input
                  type="text"
                  class="comic-input"
                  :value="rule.rule_name"
                  :data-scope="String(index)"
                  data-field="rule_name"
                  @input="handleInput"
                >
              </label>
              <div class="limits-rate-fields">
                <label class="limits-check">
                  <input
                    type="checkbox"
                    :data-scope="String(index)"
                    data-field="enabled"
                    :checked="rule.enabled === true"
                    @change="handleChange"
                  >
                  {{ t('启用') }}
                </label>
                <label class="form-item">
                  <span class="form-label">{{ t('周期（秒）') }}</span>
                  <input
                    type="number"
                    class="comic-input"
                    min="1"
                    max="604800"
                    step="1"
                    required
                    :data-scope="String(index)"
                    data-field="period_seconds"
                    :value="rule.period_seconds"
                    @input="handleInput"
                  >
                </label>
                <label class="form-item">
                  <span class="form-label">{{ t('最大次数') }}</span>
                  <input
                    type="number"
                    class="comic-input"
                    min="1"
                    max="10000"
                    step="1"
                    required
                    :data-scope="String(index)"
                    data-field="max_requests"
                    :value="rule.max_requests"
                    @input="handleInput"
                  >
                </label>
              </div>
              <div v-if="rule.group_ids?.length" class="limits-notice limits-notice--warning">
                <p>{{ t('旧群号待迁移：{v0}', { v0: rule.group_ids.join('、') }) }}</p>
                <p>{{ t('旧群号不会自动转换或清除。启用的未迁移规则会暂停聊天请求，保存也会被拒绝。请选择非空完整 UMO，再明确确认迁移；停用不会清除旧群号。') }}</p>
                <button
                  type="button"
                  class="comic-btn comic-btn--sm comic-btn--outline"
                  data-limits-action="migrate-rule"
                  :data-index="index"
                  :disabled="!rule.umos.length"
                >
                  {{ t('确认迁移') }}
                </button>
              </div>
              <p class="field-hint">{{ t('UMO 留空 = 所有会话分别计数，不是全局共享额度。') }}</p>
              <div class="limits-selected" :data-selected-index="index">
                <p class="field-hint">{{ t('已选 {v0} / 500 个会话', { v0: rule.umos.length }) }}</p>
                <div v-for="umo in rule.umos" :key="umo" class="limits-session">
                  <div class="limits-session-identity">
                    <span>{{ sessionNames[umo] || t('名称未知（保留完整身份）') }}</span>
                    <code>{{ umo }}</code>
                  </div>
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    data-limits-action="remove-umo"
                    :data-index="index"
                    :data-umo="umo"
                    :aria-label="t('移除会话 {v0}', { v0: umo })"
                  >
                    {{ t('移除') }}
                  </button>
                </div>
              </div>
              <button
                type="button"
                class="comic-btn comic-btn--sm comic-btn--outline"
                data-limits-action="choose-sessions"
                :data-index="index"
                :aria-expanded="picker?.index === index ? 'true' : 'false'"
                :aria-controls="`limits-picker-${index}`"
              >
                {{ t('选择会话 / 手动粘贴 UMO') }}
              </button>
              <div :id="`limits-picker-${index}`" :data-picker-index="index">
                <section v-if="picker?.index === index" class="limits-picker" :aria-label="t('会话多选器')">
                  <div class="limits-toolbar">
                    <h4>{{ t('选择会话') }}</h4>
                    <button
                      type="button"
                      class="comic-btn comic-btn--sm comic-btn--outline"
                      data-limits-action="close-picker"
                      :data-index="index"
                    >
                      {{ t('收起') }}
                    </button>
                  </div>
                  <form class="limits-search" data-limits-search>
                    <input
                      v-model="picker.search"
                      type="search"
                      class="comic-input"
                      :placeholder="t('名称或完整 UMO')"
                      :aria-label="t('搜索会话')"
                      data-limits-search-input
                    >
                    <select
                      v-model="picker.type"
                      class="comic-select"
                      data-session-filter="type"
                      :aria-label="t('会话类型')"
                      @change="handleChange"
                    >
                      <option value="all">{{ t('全部类型') }}</option>
                      <option value="group">{{ t('群聊') }}</option>
                      <option value="private">{{ t('私聊') }}</option>
                    </select>
                    <input
                      v-model="picker.platform"
                      type="text"
                      class="comic-input"
                      :placeholder="t('全部平台')"
                      :aria-label="t('平台标识（精确筛选）')"
                    >
                    <button type="submit" class="comic-btn comic-btn--sm">{{ t('搜索') }}</button>
                  </form>
                  <p class="field-hint" role="status" aria-live="polite">{{ picker.resultStatus }}</p>
                  <div class="limits-session-results">
                    <label
                      v-for="session in picker.results"
                      :key="session.umo"
                      class="limits-session limits-session--choice"
                    >
                      <input
                        type="checkbox"
                        :data-session-umo="session.umo"
                        :checked="draft.rate_limit_rules[picker.index]?.umos.includes(session.umo)"
                        @change="handleChange"
                      >
                      <span class="limits-session-identity">
                        <strong>{{ session.name }}</strong>
                        <span class="field-hint">{{ t('{v0} · {v1} · 会话 ID：{v2}', { v0: session.platform, v1: session.message_type, v2: session.session_id }) }}</span>
                        <code>{{ session.umo }}</code>
                      </span>
                    </label>
                  </div>
                  <div class="limits-actions">
                    <button
                      type="button"
                      class="comic-btn comic-btn--sm comic-btn--outline"
                      data-limits-action="previous-sessions"
                      :data-index="index"
                      :disabled="picker.previousDisabled"
                    >
                      {{ t('上一页') }}
                    </button>
                    <span class="field-hint">{{ picker.pageLabel }}</span>
                    <button
                      type="button"
                      class="comic-btn comic-btn--sm comic-btn--outline"
                      data-limits-action="next-sessions"
                      :data-index="index"
                      :disabled="picker.nextDisabled"
                    >
                      {{ t('下一页') }}
                    </button>
                  </div>
                  <label class="form-item">
                    <span class="form-label">{{ t('手动添加完整 UMO（不接受裸群号）') }}</span>
                    <textarea
                      v-model="picker.manual"
                      class="comic-input"
                      rows="3"
                      spellcheck="false"
                      :placeholder="t('每行一个完整 UMO，例如 platform:GroupMessage:session:id')"
                      :aria-label="t('手动粘贴完整 UMO')"
                    ></textarea>
                  </label>
                  <button
                    type="button"
                    class="comic-btn comic-btn--sm comic-btn--outline"
                    data-limits-action="paste-umos"
                    :data-index="index"
                  >
                    {{ t('添加到已选会话') }}
                  </button>
                  <p class="field-hint" role="status" aria-live="polite">{{ picker.feedback }}</p>
                </section>
              </div>
            </article>
          </div>
          <button
            type="button"
            class="comic-btn comic-btn--sm comic-btn--outline"
            data-limits-action="add-rule"
            data-index=""
            :disabled="draft.rate_limit_rules.length >= 100"
          >
            {{ t('添加规则') }}
          </button>
        </section>
      </template>
    </fieldset>
  </div>
</template>
