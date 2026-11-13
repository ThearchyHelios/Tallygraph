import { LEDGER_UI } from '../../../lib/termsLedger'
import type {
  CaliberDraft, CaliberDraftStage, CaliberDraftSummary, DraftBinding, DraftEditableField, DraftEntryField,
  DraftImportField, DraftImportItem, DraftImportRun, DraftOperation, DraftTrialValidation,
  DraftValidationRecord, DraftValidationReview, QualityReport,
} from '../../../typesLedger'

const I = LEDGER_UI.imports
const N = LEDGER_UI.nl
const V = LEDGER_UI.validation

/** 列表按阶段筛：全部、起草中、待审、已丢弃、失败；细状态不再单独占筛选项 */
export type Filter = '' | CaliberDraftStage
export const FILTERS: Filter[] = ['', 'drafting', 'review', 'discarded', 'failed']

/** 细状态 → 界面阶段；旧服务端没有 stage 时按 status 推，页面不会因为少一个字段就空 */
export const stageOf = (draft: CaliberDraftSummary | CaliberDraft): CaliberDraftStage =>
  draft.stage ?? (draft.status === 'drafting' ? 'drafting' : draft.status === 'rejected' ? 'discarded'
    : draft.status === 'failed' ? 'failed' : 'review')

/** 起草轮询：每两秒一次，多轮续跑要几分钟，最多等十五分钟无进展 */
export const DRAFT_POLL_MS = 2_000
export const DRAFT_POLL_LIMIT_MS = 15 * 60_000

/** 验证记录默认摆最近几条，其余收在「展开其余 N 条」后面 */
export const RECORDS_SHOWN = 3

/**
 * 幂等键（K02）：服务端按「主体 + 工作流 + 目标卡 + request_id」认这一次修改。同一批勾选的问题反复提交（网络
 * 断了、手点重试）用同一个键，服务端认得出是同一件事、不会多存一版；勾选或草稿变了才换一个。
 */
export const newRequestId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `patch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`

/** span 是引用在段落原文里的 [start, end)：切成「前 / 引用 / 后」三段高亮 */
export function spanParts(content: string, span?: [number, number]): [string, string, string] | null {
  if (!span) return null
  const [start, end] = span
  if (start < 0 || end > content.length || start >= end) return null
  return [content.slice(0, start), content.slice(start, end), content.slice(end)]
}

/** 缺省值直接读：字符串照写，其他转成 JSON 文本 */
export function defaultText(value: unknown): string {
  if (typeof value === 'string') return value
  const text = JSON.stringify(value)
  return text ?? String(value)
}

/** 入口字段预填的日期：YYYY-MM-DD，按本地日期算 */
export function dayText(offsetDays: number): string {
  const day = new Date()
  day.setDate(day.getDate() + offsetDays)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`
}

/**
 * 名字或说明里带「开始 / start」的日期字段预填七天前，带「结束 / end」的预填今天：
 * 这两个值基本总是要填的，给个能改的范围比留空省一步。其余必填字段留空，让人自己写。
 */
export function datePrefill(field: DraftEntryField): string | null {
  if ((field.type || 'string') !== 'string') return null
  const hint = `${field.name} ${field.description ?? ''}`.toLowerCase()
  if (hint.includes('开始') || /(^|[^a-z])start([^a-z]|$)/.test(hint)) return dayText(-7)
  if (hint.includes('结束') || /(^|[^a-z])end([^a-z]|$)/.test(hint)) return dayText(0)
  return null
}

/** 入口字段的初值：有缺省值就用缺省值，日期字段按上面的规则预填，其余留空 */
export function entryInitialValues(fields: DraftEntryField[], prefilled: Record<string, boolean>): Record<string, string> {
  const values: Record<string, string> = {}
  for (const field of fields) {
    const hasDefault = field.default !== undefined && field.default !== null && field.default !== ''
    if (hasDefault) { values[field.name] = defaultText(field.default); continue }
    const guess = datePrefill(field)
    values[field.name] = guess ?? ''
    if (guess) prefilled[field.name] = true
  }
  return values
}

/** 必填字段是不是都填了：布尔量总有值，数字要能读成数 */
export function entryReady(fields: DraftEntryField[], values: Record<string, string>): boolean {
  return fields.every((field) => {
    if (!field.required) return true
    const type = field.type || 'string'
    if (type === 'boolean') return true
    const text = (values[field.name] ?? '').trim()
    if (!text) return false
    return type === 'number' || type === 'integer' ? Number.isFinite(Number(text)) : true
  })
}

/** 表单值换成接口要的 inputs：空字符串不传（服务端会用缺省值），数字转成数，布尔转成开关 */
export function entryInputs(fields: DraftEntryField[], values: Record<string, string>): Record<string, unknown> {
  const inputs: Record<string, unknown> = {}
  for (const field of fields) {
    const type = field.type || 'string'
    const text = values[field.name] ?? ''
    if (type === 'boolean') { inputs[field.name] = text === 'true'; continue }
    if (!text.trim()) continue
    if (type === 'number' || type === 'integer') {
      const num = Number(text)
      if (Number.isFinite(num)) inputs[field.name] = num
      continue
    }
    inputs[field.name] = text
  }
  return inputs
}

// ---------------------------------------------------------------- K06：业务规则、变更预览、验证记录

/** 业务值的说法：能力描述给了机器值 → 给人看的说法就用它，没有就照原样写 */
export const choiceLabel = (field: DraftEditableField | undefined, value: string): string =>
  field?.value_labels?.[value] ?? value

/** 一个值转成页面上的一行文字：字符串照写，数字、布尔照写，对象转成 JSON 文本 */
export function valueText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  const text = JSON.stringify(value)
  return text ?? String(value)
}

/**
 * 表单初值：这一项在**所有**声明它的指标上写法一致时就是那个值，不一致（或没人声明）时留空。
 * 留空表示「保持各自现在的写法」——不替人选一个默认值，也就不会因为打开页面就凭空改掉谁。
 */
export function ruleInitialValues(binding?: DraftBinding): Record<string, string> {
  const out: Record<string, string> = {}
  if (!binding) return out
  const declared = Object.values(binding.metric_values ?? {})
  for (const field of binding.editable_fields ?? []) {
    const seen = new Set(declared.map((values) => values[field.field]).filter((value): value is string => !!value))
    out[field.field] = seen.size === 1 ? [...seen][0] : ''
  }
  return out
}

/** 这一项这些指标里有没有人声明：一个都没有时这一项改不了（没有落点的取数不会被硬改） */
export const declaringMetrics = (binding: DraftBinding | undefined, field: string): string[] =>
  Object.entries(binding?.metric_values ?? {})
    .filter(([, values]) => !!values[field]).map(([metricId]) => metricId)

/** 这次选中的写法会改到哪几条指标、每条从什么变成什么：只有声明了这一项、而且写法和选中的不一样才算 */
export function rulePlan(binding: DraftBinding | undefined, values: Record<string, string>) {
  const operations: DraftOperation[] = []
  const changes: { metricId: string; name: string; field: DraftEditableField; from: string; to: string }[] = []
  for (const field of binding?.editable_fields ?? []) {
    const chosen = values[field.field] ?? ''
    if (!chosen) continue
    for (const [metricId, declared] of Object.entries(binding?.metric_values ?? {})) {
      const now = declared[field.field]
      if (!now || now === chosen) continue
      operations.push({ op: 'set', metric_id: metricId, path: `/${field.field}`, value: chosen })
      changes.push({ metricId, name: '', field, from: now, to: chosen })
    }
  }
  return { operations, changes }
}

/**
 * 撤销这次修改要提交的逆操作：按**保存前**每一条指标声明的值构造——原来有值就设回去，原来没声明就删掉。
 * 撤销和保存一样是新增一版（当前版本 + 新的请求键），不整份覆盖、不改写已经存下的版本。
 */
export function undoOperations(binding: DraftBinding | undefined, operations: DraftOperation[]): DraftOperation[] {
  return operations.map((op) => {
    const field = op.path.replace(/^\//, '')
    const before = binding?.metric_values?.[op.metric_id]?.[field]
    return before
      ? { op: 'set', metric_id: op.metric_id, path: op.path, value: before }
      : { op: 'remove', metric_id: op.metric_id, path: op.path }
  })
}

// ---------------------------------------------------------------- K11 / K12：字段的说法、三方值、一句话的影响

/**
 * 字段路径 → 给人看的标签（K03 白名单和 app/ledger/changes.FIELDS 同一套，业务字段另有能力描述里的标签）。
 * 页面上只写这些业务说法，路径本身不上来。
 */
const FIELD_LABELS: Record<string, string> = {
  '/synonyms': '同义词',
  '/unit': '单位',
  '/decimals': '小数位',
  '/format': '展示格式',
  '/expression': '取值表达式',
  '/grain': '粒度',
  '/time_field': '时间字段',
  '/numerator': '分子',
  '/denominator': '分母',
  '/dimensions': '维度',
  '/filters': '过滤条件',
  '/definition/text': '口径说明',
  '/definition/scope': '适用范围',
  '/definition/exclusions': '不包含',
  '/definition/citations': '引用原文',
  '/time_role': '统计时间口径',
  '/refund_rule': '退款处理',
}
/** 引用原文在 B / L / R 里是一段带段落 id 的结构化内容：只写有几段，不摆内容 */
const CITATIONS_PATH = '/definition/citations'

export const fieldLabel = (path: string, binding?: DraftBinding): string => {
  const name = path.replace(/^\//, '')
  const editable = (binding?.editable_fields ?? []).find((field) => field.field === name)
  return editable?.label ?? FIELD_LABELS[path] ?? I.otherField
}

/** 业务字段的机器值 → 给人看的说法（能力描述里有就用它，没有就照原样写） */
export const valueLabelOf = (value: unknown, binding?: DraftBinding, path?: string): string => {
  if (value == null) return I.noValue
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') {
    const name = String(path ?? '').replace(/^\//, '')
    const editable = (binding?.editable_fields ?? []).find((field) => field.field === name)
    return editable?.value_labels?.[value] ?? value
  }
  if (Array.isArray(value)) {
    if (path === CITATIONS_PATH) return I.citationCount(value.length)
    const parts = value.map((item) => valueLabelOf(item, binding, path))
    return parts.length ? parts.join('、') : I.emptyValue
  }
  if (typeof value === 'object') return I.objectValue
  return String(value)
}

/** 一格的一行字：这一格没有时返回 null（整行不画），不把「没有这一格」写成空值 */
export function cellText(cell: DraftImportField, which: 'b' | 'l' | 'r', binding?: DraftBinding): string | null {
  const present = which === 'b' ? cell.b_present : which === 'l' ? cell.l_present : cell.r_present
  if (!present) return null
  return valueLabelOf(cell[which], binding, cell.path)
}

/** 选中的格子：一条工作项 + 一条字段路径（未对齐的工作项没有可接受的字段，拒绝对齐前不能选） */
export const pickKey = (itemId: string, path: string): string => `${itemId}\u0000${path}`

/** 服务端标的「这条抽坏了」（引用没有给过模型的原文）：也可能是老服务端只给了状态 */
export const itemFailed = (item: DraftImportItem): boolean => item.failed ?? item.status === 'failed'

/** 这个工作项现在能不能接受：抽坏了的（failed）、没对齐上的，一律不能 */
export const itemAccepts = (item: DraftImportItem): boolean => !itemFailed(item) && !!item.metric_id

/**
 * 这条工作项上「无冲突更新 + 新增候选」的格子：一次收掉的按钮按它算有几格，并把这几格**明确列给服务端**
 * （不发 auto：页面列了几格就只接受几格）。已经接受 / 拒绝过的格子不再算进来，抽坏了的工作项一个也不收。
 */
export const cleanPaths = (item: DraftImportItem): string[] =>
  itemFailed(item) ? [] : item.fields.filter((cell) => (cell.state === 'update' || cell.state === 'new')
    && cell.decided !== 'accepted' && cell.decided !== 'rejected').map((cell) => cell.path)

/**
 * 按这一次导入**现在**的现状裁掉不再适用的选择：格子没了、已经决定过、不能接受、工作项抽坏了的都不留下。
 * stale 之后重新读回现状时用它——不拿旧的选择去提交（旧的选择可能对着已经变了的 L）。
 */
export function prunePicks(picks: Record<string, 'accept' | 'reject'>, run: DraftImportRun):
  Record<string, 'accept' | 'reject'> {
  const out: Record<string, 'accept' | 'reject'> = {}
  for (const item of run.items) {
    for (const cell of item.fields) {
      const choice = picks[pickKey(item.item_id, cell.path)]
      if (!choice || cell.decided != null) continue
      if (choice === 'accept' && (!itemAccepts(item) || !cell.acceptable)) continue
      out[pickKey(item.item_id, cell.path)] = choice
    }
  }
  return out
}

/** 还没处置完的工作项：恢复只对这些有意义（已接受 / 拒绝 / 不变的不动） */
export const unfinishedItems = (run: DraftImportRun): number =>
  run.items.filter((item) => !['accepted', 'rejected', 'unchanged'].includes(item.status)).length

/** 一句话提案里的一处影响：哪条指标、哪一项写法、现在什么 → 保存后什么（页面上只写业务说法） */
export interface NlChange {
  metricId: string
  name: string
  label: string
  from: string
  to: string
  remove: boolean
}

/** 提案里的字段操作 → 人读得懂的影响：指标名 + 字段标签 + 前后值，不摆 path / metric_id / JSON */
export function nlChanges(draft: CaliberDraft, operations: DraftOperation[]): NlChange[] {
  const metrics = new Map((draft.metrics ?? []).filter((metric) => metric.id).map((metric) => [metric.id as string, metric]))
  return (operations ?? []).map((operation) => {
    const name = operation.path.replace(/^\//, '')
    const metric = metrics.get(operation.metric_id)
    const declared = draft.binding?.metric_values?.[operation.metric_id]?.[name]
      ?? (metric ? (metric as unknown as Record<string, unknown>)[name] : undefined)
    const remove = operation.op === 'remove'
    return {
      metricId: operation.metric_id,
      name: metric?.name ?? N.unknownMetric,
      label: fieldLabel(operation.path, draft.binding),
      from: valueLabelOf(declared, draft.binding, operation.path),
      to: remove ? N.removeValue : valueLabelOf(operation.value, draft.binding, operation.path),
      remove,
    }
  })
}

/**
 * 落版之后服务端交回的操作和预览里的是不是同一批（按「改哪条指标的哪个字段、怎么改」比）。
 * 同一段原文应当解析出同一批；不一样就说明页面显示的和实际落下的不是一回事，要照实提示。
 * 审计读不回来（老服务端不给这一段）时返回 null：判不了就说判不了，不假装一致。
 */
export function sameOperations(previewed: DraftOperation[],
                               applied: { op: string; metric_id: string; path: string }[] | undefined): boolean | null {
  if (!applied) return null
  const key = (op: { op: string; metric_id: string; path: string }) => `${op.op}|${op.metric_id}|${op.path}`
  const before = [...new Set(previewed.map(key))].sort()
  const after = [...new Set(applied.map(key))].sort()
  return before.length === after.length && before.every((item, index) => item === after[index])
}

/**
 * 独立期望里填的一格：整数、小数写成数，true / false 写成布尔，方括号花括号按 JSON 读，其余照字面写。
 * 服务端按**原值**比较（不做隐式舍入），这里也不替它换算。
 */
export function expectValue(text: string): unknown {
  const trimmed = text.trim()
  if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) return Number(trimmed)
  if (trimmed === 'true' || trimmed === 'false') return trimmed === 'true'
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    try { return JSON.parse(trimmed) } catch { return trimmed }
  }
  return trimmed
}

/**
 * 试跑响应里的质量结论：只有服务端给了认得的状态才算一份质量报告。
 * stale / needs_input / running 这些分支的 validation 可能只有验证记录那一段、甚至什么都没有——
 * 读不到状态就是「没有质量结论」，不能当成通过（也不能拿一次试跑成功代替验证）。
 */
export function qualityReportOf(validation?: DraftTrialValidation | null): QualityReport | null {
  const status = validation?.status
  if (status !== 'pass' && status !== 'fail' && status !== 'unknown') return null
  return { status, checks: validation?.checks ?? [] }
}

/**
 * 覆盖判断：**只有 === true 才算覆盖**。false 是不覆盖，null / 缺字段是判不了（读不到当前依据），
 * 三者要在界面上分开写，不能把判不了默认成覆盖。
 */
export const coversText = (value: boolean | null | undefined): string =>
  value === true ? V.covers : value === false ? V.notCovers : V.coversUnknown

/**
 * 一条审阅的确认范围有没有盖住整条记录的全部指标：只确认了其中几条时不能写成整卡通过。
 * scope 空着（或者读数缺字段）一律按「没盖住」算——宁可不写通过，不替服务端把范围补全。
 */
export function scopeCovers(record: DraftValidationRecord, review: DraftValidationReview | null | undefined): boolean {
  const scope = new Set(review?.scope ?? [])
  const ids = record.metric_ids ?? []
  if (!ids.length) return scope.size > 0
  return ids.every((id) => scope.has(id))
}

/** 这条记录里没有被确认范围盖住的指标 */
export function uncoveredMetrics(record: DraftValidationRecord, review: DraftValidationReview | null | undefined): string[] {
  const scope = new Set(review?.scope ?? [])
  return (record.metric_ids ?? []).filter((id) => !scope.has(id))
}
