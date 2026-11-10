/**
 * 从业务文档起草口径（/ledger/drafts；服务端 app/ledger/docdraft.py、docattach.py）。
 *
 * 主路径一次到位：选文档 → 模型读文档、对照数据目录产出定义 + 取数 + 入口字段 + 指标表达式 → 服务端校验后落成草稿图
 * → 后台自动只读试运行 → 这里展示「定义 + 数 + 原文引用」→ 人去画布审改并发布。发布才需要人签字。
 * - 发起：选知识库文档和**必选的数据源**（取数 SQL 只对照它的表和列写），可加一段起草要求；页面轮询详情直到状态离开
 *   drafting（最多十五分钟，指标多时服务端会分几轮续跑，进度写在 payload.progress）；
 * - 试运行：草稿顶部显示这次自动试运行的状态和每个指标的值，running 时每两秒取一次，最多等一分钟；必填入口没有值时停在
 *   needs_input，在入口字段里填了再点「试运行」；失败照实写原因；
 * - 审读：指标默认收成一行（名字、单位、值和未决问题数），展开看单位、粒度、指标表达式、定义和逐字核对过的引用原文
 *   （第 N 段），逐条的未决问题可以勾掉；被丢弃的条目单独列出原因；入口字段列出名字、类型、必填和缺省值；
 * - 发布：主按钮去画布审改并发布。「接上一次运行」只是辅助入口：已经有答案时让它认这份定义。
 *
 * 业务规则（K06 的工作台）：服务端的能力描述（binding）说得出这张卡能按字段改哪几项业务写法（统计时间口径、
 * 退款处理）时，这里开出下拉；改完先把「哪几条指标、现在什么写法 → 保存后什么写法」列出来，再**一次**提交所有受
 * 影响指标的字段操作（PATCH 的 operations，带基准版本和请求键）。保存回执说清换成哪一版、绑定同步到哪一步、
 * 旧验证怎么失效，并按保存前的值给出「撤销这次修改」（逆操作 + 新请求键，再存一版，不整份覆盖）。写不进去
 * （基准版本过期）时保留这里的写法选择，刷新详情后由人显式再点一次；复杂或同步不了的照实写原因和技术待办。
 * 能力描述为空、认不出唯一的口径卡、没有版本号的资产保持只读。业务规则、变更预览和验证这几块只写业务说法，
 * 不出现取数 SQL、节点 id 和字段路径。
 *
 * 文档更新（K11 的增量重导入）：新一段材料重抽成候选 R，和现在图上人工维护的 L、上次被接受的导入值 B 逐字段比。
 * **R 只是候选，不改 L、不改 B**：落到图上只有「接受选定字段」这一次显式动作（一次提交里新增一版图 + 按被接受的那几格
 * 推进 B）。逐格写清 B / L / R 和状态：无冲突更新、人工修改冲突、新增候选、需要人工对齐（没有可比基线时必须人工
 * 对齐到某条指标）、**漏提取**（不当删除，L 上的原值原样留着，也不能接受）。没接受过的格子不推进 B，所以这块另写
 * 清字段基线覆盖到哪一步（有基线不等于整张都有：partial 要明确说出来）。被中断的导入由人点「恢复」显式续跑，
 * 已接受 / 拒绝的不重复动。基准版本过期时保留选好的格子，刷新后由人再点一次。
 *
 * 用一句话改口径（K12）：一句话只产出一份**只读提案**（K03 同一形状的 operations + 影响摘要 + 澄清项），页面上
 * 只写「哪条指标、哪一项写法、现在什么 → 保存后什么」，不摆 JSON、节点编号和字段路径。含糊的业务条件先显示澄清项、
 * 一条操作都不产出；确认（confirm=true）之后才按同一批操作落一版，失败时页面上什么都不改。
 *
 * 验证与审阅（K05 的读法）：这份草稿的验证记录（状态、独立期望、实际值、覆盖判断、审阅决定）和总情况一句
 * 「结论」分开写——试运行成功、绑定对齐、样例对得上都不等于验证通过；没有独立期望就照实说只记了试运行结果。
 * 审阅只有调真实接口这一条路（审批人取服务端身份），结果照服务端交回的说。
 *
 * 页面不显示执行值：取数 SQL 只作为模板只读展示，具体取数到画布上看。服务端把细状态折成四种阶段（起草中 / 待审 /
 * 已丢弃 / 失败），页面按阶段筛、按阶段画；细状态放在标签的悬停提示里。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle, Check, ChevronLeft, FileSearch, FileText, PenLine, Play, RefreshCw, RotateCw, ScrollText, Sparkles, Undo2, X,
} from 'lucide-react'
import clsx from 'clsx'
import { api, ApiError } from '../../api/client'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, promptDialog, toast } from '../../components/ui'
import { formatDateTime, formatTime } from '../../lib/format'
import { LEDGER_UI } from '../../lib/termsLedger'
import { useAtLeast } from '../../store/auth'
import { TrialResult, TrialValidation } from './TrialResult'
import type { DataSourceName, KbDocument, Run } from '../../types'
import type {
  CaliberDraft, CaliberDraftStage, CaliberDraftStatus, CaliberDraftSummary, DraftAlignment, DraftBinding, DraftChangeReceipt,
  DraftCitation, DraftEditableField, DraftEntryField, DraftExpectationSource, DraftImportAcceptOut, DraftImportField,
  DraftImportItem, DraftImportRun, DraftMetric, DraftOperation, DraftTrialMetric, DraftTrialOut, DraftTrialValidation,
  DraftValidationsOut, DraftValidationRecord, DraftValidationReview, NlChangeProposal, QualityReport,
} from '../../typesLedger'

const D = LEDGER_UI.drafts
/** 业务规则（K04 的能力 + K06 的改法）的文案 */
const R = LEDGER_UI.rules
/** 验证与审阅（K05 的记录）的文案 */
const V = LEDGER_UI.validation
/** 文档更新 / 增量重导入（K11）的文案 */
const I = LEDGER_UI.imports
/** 用一句话改口径（K12）的文案 */
const N = LEDGER_UI.nl

/** 列表按阶段筛：全部、起草中、待审、已丢弃、失败；细状态不再单独占筛选项 */
type Filter = '' | CaliberDraftStage
const FILTERS: Filter[] = ['', 'drafting', 'review', 'discarded', 'failed']

/** 细状态 → 界面阶段；旧服务端没有 stage 时按 status 推，页面不会因为少一个字段就空 */
const stageOf = (draft: CaliberDraftSummary | CaliberDraft): CaliberDraftStage =>
  draft.stage ?? (draft.status === 'drafting' ? 'drafting' : draft.status === 'rejected' ? 'discarded'
    : draft.status === 'failed' ? 'failed' : 'review')

/** 起草轮询：每两秒一次，多轮续跑要几分钟，最多等十分钟 */
const DRAFT_POLL_MS = 2_000
const DRAFT_POLL_LIMIT_MS = 15 * 60_000

/** 验证记录默认摆最近几条，其余收在「展开其余 N 条」后面 */
const RECORDS_SHOWN = 3

/**
 * 幂等键（K02）：服务端按「主体 + 工作流 + 目标卡 + request_id」认这一次修改。同一批勾选的问题反复提交（网络
 * 断了、手点重试）用同一个键，服务端认得出是同一件事、不会多存一版；勾选或草稿变了才换一个。
 */
const newRequestId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `patch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`

/** 阶段标签：起草中 / 待审 / 已丢弃 / 起草失败；细状态放在 title 里，鼠标悬停能看到「待确认问题」这类差别 */
function StatusPill({ stage, status }: { stage: CaliberDraftStage; status?: CaliberDraftStatus }) {
  const color = stage === 'review'
    ? (status === 'ready' || status === 'attached' ? 'var(--ok)' : 'var(--warn)')
    : stage === 'drafting' ? 'var(--accent)' : stage === 'discarded' ? 'var(--text-faint)' : 'var(--err)'
  return (
    <span className="shrink-0 rounded border px-1.5 py-0.5 text-[10px]" title={status ? D.status[status] : undefined}
          style={{ color, borderColor: `color-mix(in srgb, ${color} 40%, var(--border))` }}>
      {D.stage[stage] ?? stage}
    </span>
  )
}

/** span 是引用在段落原文里的 [start, end)：切成「前 / 引用 / 后」三段高亮 */
function spanParts(content: string, span?: [number, number]): [string, string, string] | null {
  if (!span) return null
  const [start, end] = span
  if (start < 0 || end > content.length || start >= end) return null
  return [content.slice(0, start), content.slice(start, end), content.slice(end)]
}

function QuoteBody({ content, span, quote }: { content: string; span?: [number, number]; quote: string }) {
  const parts = spanParts(content, span)
  if (!content) return <>{quote}</>
  if (!parts) return <>{content}</>
  return (
    <>
      {parts[0]}
      <mark className="rounded px-0.5 text-fg" style={{ background: 'color-mix(in srgb, var(--warn) 22%, transparent)' }}>
        {parts[1]}
      </mark>
      {parts[2]}
    </>
  )
}

/** 草稿和已发布口径的对齐：新的、已有出处的、定义不同的 */
function AlignmentBlock({ alignment }: { alignment: DraftAlignment }) {
  const added = alignment.new ?? []
  const duplicate = alignment.duplicate ?? []
  const conflict = alignment.conflict ?? []
  if (!added.length && !duplicate.length && !conflict.length) return null
  const row = (label: string, items: { name: string; reason?: string }[]) => items.length > 0 && (
    <li className="flex flex-wrap items-baseline gap-x-1.5">
      <span className="text-faint">{label}</span>
      {items.map((item) => (
        <span key={item.name}>
          {item.name}
          {item.reason ? `（${item.reason}）` : ''}
        </span>
      ))}
    </li>
  )
  return (
    <div data-draft-alignment="">
      <h3 className="mb-1 text-2xs font-semibold text-dim">{D.alignment}</h3>
      <ul className="space-y-1 text-2xs leading-relaxed">
        {row(D.alignmentNew, added.map((name) => ({ name })))}
        {row(D.alignmentDuplicate, duplicate)}
        {row(D.alignmentConflict, conflict)}
      </ul>
    </div>
  )
}

/** 缺省值直接读：字符串照写，其他转成 JSON 文本 */
function defaultText(value: unknown): string {
  if (typeof value === 'string') return value
  const text = JSON.stringify(value)
  return text ?? String(value)
}

/** 入口字段预填的日期：YYYY-MM-DD，按本地日期算 */
function dayText(offsetDays: number): string {
  const day = new Date()
  day.setDate(day.getDate() + offsetDays)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`
}

/**
 * 名字或说明里带「开始 / start」的日期字段预填七天前，带「结束 / end」的预填今天：
 * 这两个值基本总是要填的，给个能改的范围比留空省一步。其余必填字段留空，让人自己写。
 */
function datePrefill(field: DraftEntryField): string | null {
  if ((field.type || 'string') !== 'string') return null
  const hint = `${field.name} ${field.description ?? ''}`.toLowerCase()
  if (hint.includes('开始') || /(^|[^a-z])start([^a-z]|$)/.test(hint)) return dayText(-7)
  if (hint.includes('结束') || /(^|[^a-z])end([^a-z]|$)/.test(hint)) return dayText(0)
  return null
}

/** 入口字段的初值：有缺省值就用缺省值，日期字段按上面的规则预填，其余留空 */
function entryInitialValues(fields: DraftEntryField[], prefilled: Record<string, boolean>): Record<string, string> {
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
function entryReady(fields: DraftEntryField[], values: Record<string, string>): boolean {
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
function entryInputs(fields: DraftEntryField[], values: Record<string, string>): Record<string, unknown> {
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
const choiceLabel = (field: DraftEditableField | undefined, value: string): string =>
  field?.value_labels?.[value] ?? value

/** 一个值转成页面上的一行文字：字符串照写，数字、布尔照写，对象转成 JSON 文本 */
function valueText(value: unknown): string {
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
function ruleInitialValues(binding?: DraftBinding): Record<string, string> {
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
const declaringMetrics = (binding: DraftBinding | undefined, field: string): string[] =>
  Object.entries(binding?.metric_values ?? {})
    .filter(([, values]) => !!values[field]).map(([metricId]) => metricId)

/** 这次选中的写法会改到哪几条指标、每条从什么变成什么：只有声明了这一项、而且写法和选中的不一样才算 */
function rulePlan(binding: DraftBinding | undefined, values: Record<string, string>) {
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
function undoOperations(binding: DraftBinding | undefined, operations: DraftOperation[]): DraftOperation[] {
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

const fieldLabel = (path: string, binding?: DraftBinding): string => {
  const name = path.replace(/^\//, '')
  const editable = (binding?.editable_fields ?? []).find((field) => field.field === name)
  return editable?.label ?? FIELD_LABELS[path] ?? I.otherField
}

/** 业务字段的机器值 → 给人看的说法（能力描述里有就用它，没有就照原样写） */
const valueLabelOf = (value: unknown, binding?: DraftBinding, path?: string): string => {
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
function cellText(cell: DraftImportField, which: 'b' | 'l' | 'r', binding?: DraftBinding): string | null {
  const present = which === 'b' ? cell.b_present : which === 'l' ? cell.l_present : cell.r_present
  if (!present) return null
  return valueLabelOf(cell[which], binding, cell.path)
}

/** 选中的格子：一条工作项 + 一条字段路径（未对齐的工作项没有可接受的字段，拒绝对齐前不能选） */
const pickKey = (itemId: string, path: string): string => `${itemId}\u0000${path}`

/**
 * 这条工作项上「无冲突更新 + 新增候选」的格子：一次收掉的按钮按它算有几格，并把这几格**明确列给服务端**
 * （不发 auto：页面列了几格就只接受几格）。已经接受 / 拒绝过的格子不再算进来，抽坏了的工作项一个也不收。
 */
const cleanPaths = (item: DraftImportItem): string[] =>
  itemFailed(item) ? [] : item.fields.filter((cell) => (cell.state === 'update' || cell.state === 'new')
    && cell.decided !== 'accepted' && cell.decided !== 'rejected').map((cell) => cell.path)

/** 服务端标的「这条抽坏了」（引用没有给过模型的原文）：也可能是老服务端只给了状态 */
const itemFailed = (item: DraftImportItem): boolean => item.failed ?? item.status === 'failed'

/** 这个工作项现在能不能接受：抽坏了的（failed）、没对齐上的，一律不能 */
const itemAccepts = (item: DraftImportItem): boolean => !itemFailed(item) && !!item.metric_id

/**
 * 按这一次导入**现在**的现状裁掉不再适用的选择：格子没了、已经决定过、不能接受、工作项抽坏了的都不留下。
 * stale 之后重新读回现状时用它——不拿旧的选择去提交（旧的选择可能对着已经变了的 L）。
 */
function prunePicks(picks: Record<string, 'accept' | 'reject'>, run: DraftImportRun):
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
const unfinishedItems = (run: DraftImportRun): number =>
  run.items.filter((item) => !['accepted', 'rejected', 'unchanged'].includes(item.status)).length

/** 一句话提案里的一处影响：哪条指标、哪一项写法、现在什么 → 保存后什么（页面上只写业务说法） */
interface NlChange {
  metricId: string
  name: string
  label: string
  from: string
  to: string
  remove: boolean
}

/** 提案里的字段操作 → 人读得懂的影响：指标名 + 字段标签 + 前后值，不摆 path / metric_id / JSON */
function nlChanges(draft: CaliberDraft, operations: DraftOperation[]): NlChange[] {
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
function sameOperations(previewed: DraftOperation[],
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
function expectValue(text: string): unknown {
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
function qualityReportOf(validation?: DraftTrialValidation | null): QualityReport | null {
  const status = validation?.status
  if (status !== 'pass' && status !== 'fail' && status !== 'unknown') return null
  return { status, checks: validation?.checks ?? [] }
}

/**
 * 覆盖判断：**只有 === true 才算覆盖**。false 是不覆盖，null / 缺字段是判不了（读不到当前依据），
 * 三者要在界面上分开写，不能把判不了默认成覆盖。
 */
const coversText = (value: boolean | null | undefined): string =>
  value === true ? V.covers : value === false ? V.notCovers : V.coversUnknown

/**
 * 一条审阅的确认范围有没有盖住整条记录的全部指标：只确认了其中几条时不能写成整卡通过。
 * scope 空着（或者读数缺字段）一律按「没盖住」算——宁可不写通过，不替服务端把范围补全。
 */
function scopeCovers(record: DraftValidationRecord, review: DraftValidationReview | null | undefined): boolean {
  const scope = new Set(review?.scope ?? [])
  const ids = record.metric_ids ?? []
  if (!ids.length) return scope.size > 0
  return ids.every((id) => scope.has(id))
}

/** 这条记录里没有被确认范围盖住的指标 */
function uncoveredMetrics(record: DraftValidationRecord, review: DraftValidationReview | null | undefined): string[] {
  const scope = new Set(review?.scope ?? [])
  return (record.metric_ids ?? []).filter((id) => !scope.has(id))
}

export function CaliberDrafts() {
  const [drafts, setDrafts] = useState<CaliberDraftSummary[] | null>(null)
  const [listError, setListError] = useState<unknown>(null)
  const [filter, setFilter] = useState<Filter>('')

  const [openId, setOpenId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CaliberDraft | null>(null)
  const [detailError, setDetailError] = useState<unknown>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [docs, setDocs] = useState<KbDocument[] | null>(null)
  const [docsError, setDocsError] = useState<unknown>(null)
  const [sources, setSources] = useState<DataSourceName[]>([])
  const [pickedDocs, setPickedDocs] = useState<string[]>([])
  const [datasourceId, setDatasourceId] = useState('')
  const [guidance, setGuidance] = useState('')
  /** 每次跑都不变的条件（工厂、产线、属性组）：要求写死在 SQL 里，不做成入口字段 */
  const [fixedFilters, setFixedFilters] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [pickedQuestions, setPickedQuestions] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [runId, setRunId] = useState('')
  const [recentRuns, setRecentRuns] = useState<Run[] | null>(null)
  const [attachOut, setAttachOut] = useState<{ workflow_id: string; version: number; warnings: string[] } | null>(null)

  /** 起草中的草稿：每两秒取一次详情，状态离开 drafting 或超过十分钟就停 */
  const [pollingId, setPollingId] = useState<string | null>(null)
  const [pollTimedOut, setPollTimedOut] = useState(false)

  /** 这次自动试运行的结果；running 时单独轮询，和草稿抽取的轮询互不相干 */
  const [trial, setTrial] = useState<DraftTrialOut | null>(null)
  const [trialPollId, setTrialPollId] = useState<string | null>(null)
  const [trialTimedOut, setTrialTimedOut] = useState(false)
  const [trialError, setTrialError] = useState<unknown>(null)
  const [jobError, setJobError] = useState<unknown>(null)
  const [jobBusy, setJobBusy] = useState<string | null>(null)

  /** 试运行的表单值：入口字段可改，必填没填满时按钮禁用；prefilled 记哪些是页面替人预填的 */
  const [entryValues, setEntryValues] = useState<Record<string, string>>({})
  const [prefilled, setPrefilled] = useState<Record<string, boolean>>({})
  const [trialStarting, setTrialStarting] = useState(false)

  /** 引用所在段落的正文和段号：按文档懒取，取过的记在这里 */
  const [chunks, setChunks] = useState<Record<string, { ordinal: number; content: string }>>({})
  const asked = useRef(new Set<string>())
  /** 连续点开两份草稿时，晚到的那次详情不覆盖现在打开的那份 */
  const openSeq = useRef(0)
  /** 这次「回答并移除」的幂等键：勾选或草稿变了才换（K02） */
  const pendingWrite = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })

  /** K06 业务规则：表单里选中的写法（字段名 → 机器值；空串是「保持各自现在的写法」） */
  const [ruleValues, setRuleValues] = useState<Record<string, string>>({})
  /** 这次保存的幂等键：同一批字段操作反复提交用同一个键，选择变了才换 */
  const rulePending = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })
  const [ruleBusy, setRuleBusy] = useState<'save' | 'undo' | null>(null)
  const [ruleError, setRuleError] = useState<unknown>(null)
  /** 写入被拒（基准版本过期）：说清你基于哪一版、服务端现在是哪一版，选择一个字不动 */
  const [ruleStale, setRuleStale] = useState<{ current: number | null; base: number | null } | null>(null)
  /** 这次保存（或撤销）的回执：换成哪一版、绑定同步到哪一步、旧验证怎么失效 */
  const [ruleResult, setRuleResult] = useState<DraftChangeReceipt | null>(null)
  const [ruleReplayed, setRuleReplayed] = useState(false)
  /** 撤销要提交的逆操作（按保存前的值构造）和这次改了几处 */
  const [undoRecord, setUndoRecord] = useState<{ operations: DraftOperation[]; count: number } | null>(null)

  /** K11 增量重导入：这份草稿的导入记录、正在看的那一次、逐格的选择和人工对齐 */
  const [importRuns, setImportRuns] = useState<DraftImportRun[] | null>(null)
  const [importsError, setImportsError] = useState<unknown>(null)
  const [importId, setImportId] = useState<string | null>(null)
  const [importBusy, setImportBusy] = useState<string | null>(null)
  const [importError, setImportError] = useState<unknown>(null)
  /** 逐格的选择：`itemId\0path` → accept / reject（没选的格子不动） */
  const [importPicks, setImportPicks] = useState<Record<string, 'accept' | 'reject'>>({})
  /** 没对齐的工作项要人工指定对哪条指标：item_id → metric_id（空串是取消对齐） */
  const [alignPicks, setAlignPicks] = useState<Record<string, string>>({})
  /** 接受被拒（基准版本过期 / 这次导入比的还是旧的 L）：选好的格子一个不丢，说清你基于哪一版 */
  const [importStale, setImportStale] = useState<{ code: 'stale_revision' | 'import_stale_l'
    current: number | null; base: number | null
    /** 服务端点名的那几格（字段的给人看的说法），import_stale_l 时用来指名道姓 */
    names: string } | null>(null)
  /** 这次接受 / 拒绝的回执 */
  const [importResult, setImportResult] = useState<DraftImportAcceptOut | null>(null)
  const [importAccepted, setImportAccepted] = useState(false)
  const [importRejected, setImportRejected] = useState(false)
  const [importNote, setImportNote] = useState('')
  /** 接受的幂等键：同一批格子的接受反复提交用同一个键，选择变了才换 */
  const importRequest = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })
  /** 重新读取 / 恢复之后：queued、running 时每两秒取一次这一次导入 */
  const [importPollId, setImportPollId] = useState<string | null>(null)

  /** K12 一句话改口径：输入、只读提案、这次落版的结果 */
  const [nlText, setNlText] = useState('')
  const [nlPreview, setNlPreview] = useState<NlChangeProposal | null>(null)
  /** 这份提案是哪一段原文产出来的：输入改过就必须重新预览，确认发的也是这一段 */
  const [nlPreviewOf, setNlPreviewOf] = useState('')
  const [nlNotice, setNlNotice] = useState<'idle' | 'preview' | 'clarify' | 'applied'>('idle')
  const [nlBusy, setNlBusy] = useState<'preview' | 'confirm' | null>(null)
  const [nlError, setNlError] = useState<unknown>(null)
  const [nlResult, setNlResult] = useState<{ revision: number | null; invalidated: boolean; replayed: boolean
    /** 服务端这次落下的操作和预览里的是不是同一批；null 是审计读不回来（判不了） */
    mismatch: boolean | null } | null>(null)
  /** 确认落版用的幂等键：同一句话反复确认是同一件事（服务端认得出，不会多存一版） */
  const nlRequest = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })

  /** K05 的验证记录：记录 + 总情况 + 字段基线状态，走单独那个接口取 */
  const [validations, setValidations] = useState<DraftValidationsOut | null>(null)
  const [validationsError, setValidationsError] = useState<unknown>(null)
  /** 刚做完的审阅：按记录 id 显示服务端交回的决定（谁、覆盖不覆盖），不等列表刷新 */
  const [reviewOut, setReviewOut] = useState<Record<string, DraftValidationRecord['review']>>({})
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({})
  const [reviewBusy, setReviewBusy] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState<unknown>(null)
  /** 独立期望：每条指标一格，逐条给全才能提交 */
  const [expectValues, setExpectValues] = useState<Record<string, string>>({})
  const [expectSource, setExpectSource] = useState<DraftExpectationSource>('human_confirmed')
  /** 验证记录先摆最近几条，其余收起来 */
  const [recordsShown, setRecordsShown] = useState(RECORDS_SHOWN)
  const canReview = useAtLeast('builder')

  const loadList = useCallback(() => {
    setListError(null)
    api.caliberLedger.drafts().then(
      (body) => setDrafts(body.drafts),
      (e) => { setDrafts(null); setListError(e) },
    )
  }, [])
  useEffect(loadList, [loadList])

  /** K05 的验证记录：记录、总情况、字段基线状态。读不到照实写「无法读取」，不当成「还没有记录」 */
  const loadValidations = useCallback((id: string) => {
    const seq = openSeq.current
    api.caliberLedger.draftValidations(id).then(
      (out) => { if (seq === openSeq.current) { setValidations(out); setValidationsError(null) } },
      (e) => { if (seq === openSeq.current) { setValidations(null); setValidationsError(e) } },
    )
  }, [])

  /** 把一次导入的现状并回列表（新的在前）：重新读取 / 恢复 / 对齐 / 接受 / 拒绝之后都用它 */
  const putImport = useCallback((run: DraftImportRun) => {
    setImportRuns((cur) => [run, ...(cur ?? []).filter((row) => row.run_id !== run.run_id)])
  }, [])

  /**
   * K11 的导入记录：新的在前，每条都带工作项和逐字段三方。还有正在跑的那一次就跟着轮询它
   * （重新读取是后台任务，页面只管取它的现状）
   */
  const loadImports = useCallback((id: string) => {
    const seq = openSeq.current
    api.caliberLedger.imports(id).then(
      (out) => {
        if (seq !== openSeq.current) return
        setImportRuns(out.runs)
        setImportsError(null)
        // 默认摆最近那一次的明细：记录列表点哪一次看哪一次
        setImportId((cur) => (cur && out.runs.some((run) => run.run_id === cur) ? cur : (out.runs[0]?.run_id ?? null)))
        const running = out.runs.find((run) => run.status === 'queued' || run.status === 'running')
        if (running) setImportPollId(running.run_id)
      },
      (e) => { if (seq === openSeq.current) { setImportRuns(null); setImportsError(e) } },
    )
  }, [])

  useEffect(() => {
    api.kb.documents().then((rows) => { setDocs(rows); setDocsError(null) },
      (e) => { setDocs([]); setDocsError(e) })
    api.datasources.names().then(
      (all) => {
        const enabled = all.filter((s) => s.enabled !== false)
        setSources(enabled)
        // 数据源必选（取数 SQL 要对照这个库的表和列写），默认选中第一个，别让人对着 422 才知道要填
        setDatasourceId((cur) => cur || (enabled[0]?.id ?? ''))
      },
      () => setSources([]),
    )
  }, [])

  const ensureChunks = useCallback((documentId: string) => {
    if (!documentId || asked.current.has(documentId)) return
    asked.current.add(documentId)
    api.kb.document(documentId).then(({ chunks: rows }) => {
      setChunks((cur) => {
        const next = { ...cur }
        for (const row of rows) next[row.id] = { ordinal: row.ordinal, content: row.content }
        return next
      })
    }, () => { asked.current.delete(documentId) })
  }, [])

  const open = async (id: string) => {
    const seq = ++openSeq.current
    setOpenId(id)
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setPickedQuestions([])
    setAttachOut(null)
    setRunId('')
    setRecentRuns(null)
    setPollingId(null)
    setPollTimedOut(false)
    setTrial(null)
    setTrialPollId(null)
    setTrialTimedOut(false)
    setTrialError(null)
    setJobError(null)
    setRuleValues({})
    setRuleBusy(null)
    setRuleError(null)
    setRuleStale(null)
    setRuleResult(null)
    setRuleReplayed(false)
    setUndoRecord(null)
    rulePending.current = { signature: '', requestId: '' }
    setValidations(null)
    setValidationsError(null)
    setReviewOut({})
    setReviewNotes({})
    setReviewError(null)
    setExpectValues({})
    setExpectSource('human_confirmed')
    setRecordsShown(RECORDS_SHOWN)
    setImportRuns(null)
    setImportsError(null)
    setImportId(null)
    setImportBusy(null)
    setImportError(null)
    setImportPicks({})
    setAlignPicks({})
    setImportStale(null)
    setImportResult(null)
    setImportAccepted(false)
    setImportRejected(false)
    setImportNote('')
    setImportPollId(null)
    importRequest.current = { signature: '', requestId: '' }
    setNlText('')
    setNlPreview(null)
    setNlPreviewOf('')
    setNlNotice('idle')
    setNlBusy(null)
    setNlError(null)
    setNlResult(null)
    nlRequest.current = { signature: '', requestId: '' }
    try {
      const draft = await api.caliberLedger.draft(id)
      if (seq !== openSeq.current) return
      setDetail(draft)
      const marks: Record<string, boolean> = {}
      setEntryValues(entryInitialValues(draft.entry_fields ?? [], marks))
      setPrefilled(marks)
      // 业务规则的初值按这次读到的能力描述定：写法一致的项直接显示，不一致的留空等人选
      setRuleValues(ruleInitialValues(draft.binding))
      if (draft.status === 'drafting') { setPollTimedOut(false); setPollingId(id) }
    } catch (e) {
      if (seq !== openSeq.current) return
      setDetailError(e)
    } finally {
      if (seq === openSeq.current) setDetailLoading(false)
    }
    loadValidations(id)
    loadImports(id)
    api.runs.list({ status: 'succeeded', limit: 20 }).then(
      (runs) => { if (seq === openSeq.current) setRecentRuns(runs) },
      () => { if (seq === openSeq.current) setRecentRuns([]) },
    )
  }

  /**
   * 只刷新详情，不重置页面（不清勾选、不动表单、不换打开的那份）。写入被拒之后用它：拿回服务端现在这一版
   * 当新的基准版本，人确认后再点一次就行（K02）。晚到的响应不覆盖已经切到别的草稿的那一份
   */
  const refreshDetail = useCallback(async (id: string) => {
    const seq = openSeq.current
    try {
      const fresh = await api.caliberLedger.draft(id)
      if (seq === openSeq.current) setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
    } catch {
      // 读不到就先保持现在这一份：人下次动手时还会再取
    }
  }, [])

  useEffect(() => {
    if (!pollingId) return
    const until = Date.now() + DRAFT_POLL_LIMIT_MS
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.draft(pollingId).then((draft) => {
        if (!alive) return
        setDetail((cur) => (cur && cur.id === draft.id ? draft : cur))
        if (draft.status !== 'drafting') {
          setPollingId(null)
          setPollTimedOut(false)
          loadList()
        } else if (Date.now() > until) {
          setPollingId(null)
          setPollTimedOut(true)
          loadList()
        }
      }, (e) => {
        if (!alive) return
        setPollingId(null)
        setDetailError(e)
      })
    }, DRAFT_POLL_MS)
    return () => { alive = false; clearInterval(timer) }
  }, [pollingId, loadList])

  /** 草稿有了试运行就取一次结果；还在 running 就交给下面的轮询 */
  useEffect(() => {
    const id = detail?.id
    const run = detail?.trial?.run_id
    if (!id || !run) return
    let alive = true
    setTrial((cur) => (cur?.run_id === run && detail?.trial?.status !== 'stale' ? cur : {
      run_id: run, status: detail?.trial?.status ?? 'running', metrics: [], error: detail?.trial?.error ?? '',
    }))
    api.caliberLedger.draftTrial(id).then((out) => {
      if (!alive) return
      setTrial(out)
      setTrialError(null)
      setTrialTimedOut(false)
      if (out.status === 'running') setTrialPollId(id)
    }, (e) => {
      if (!alive) return
      setTrialError(e)
      setTrialPollId(null)
    })
    return () => { alive = false }
  }, [detail?.id, detail?.version, detail?.trial?.run_id, detail?.trial?.status])

  /** 试运行还在跑：每两秒取一次，超过一分钟就停；收尾时顺带刷新详情（状态可能已变成待签字发布） */
  useEffect(() => {
    if (!trialPollId) return
    const until = Date.now() + 60_000
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.draftTrial(trialPollId).then((out) => {
        if (!alive) return
        setTrial(out)
        setTrialError(null)
        if (out.status === 'running') {
          if (Date.now() > until) {
            setTrialPollId(null)
            setTrialTimedOut(true)
          }
          return
        }
        setTrialPollId(null)
        setTrialTimedOut(false)
        loadList()
        // 试运行到终态时服务端会把这次跑完的验证记录补记一条：跟着取一次，结论按记录说
        loadValidations(trialPollId)
        const seq = openSeq.current
        api.caliberLedger.draft(trialPollId).then((fresh) => {
          if (seq !== openSeq.current) return
          setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
        }, () => undefined)
      }, (e) => {
        if (!alive) return
        setTrialPollId(null)
        setTrialError(e)
      })
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [trialPollId, loadList, loadValidations])

  /**
   * 正在跑的那一次重导入：每两秒取一次现状（后台在抽候选、算三方），到终态就停。被中断的不会自动续跑，
   * 页面上由人点「恢复」显式排回后台。
   */
  useEffect(() => {
    if (!importPollId) return
    const until = Date.now() + 5 * 60_000
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.importRun(importPollId).then((run) => {
        if (!alive) return
        putImport(run)
        if (run.status === 'queued' || run.status === 'running') {
          if (Date.now() > until) setImportPollId(null)
          return
        }
        setImportPollId(null)
      }, (e) => {
        if (!alive) return
        setImportPollId(null)
        setImportError(e)
      })
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [importPollId, putImport])

  useEffect(() => {
    for (const doc of detail?.documents ?? []) ensureChunks(doc.document_id)
  }, [detail, ensureChunks])

  const shown = useMemo(
    () => (drafts ?? []).filter((d) => !filter || stageOf(d) === filter),
    [drafts, filter],
  )

  const chunkDoc = useMemo(() => {
    const map: Record<string, string> = {}
    for (const doc of detail?.documents ?? []) for (const chunk of doc.chunk_ids) map[chunk] = doc.document_id
    return map
  }, [detail])

  const paragraphOf = (citation: DraftCitation): number | null => {
    const ordinal = chunks[citation.chunk]?.ordinal
    if (typeof ordinal === 'number') return ordinal
    if (typeof citation.ordinal === 'number') return citation.ordinal
    const documentId = citation.document_id || chunkDoc[citation.chunk]
    const doc = detail?.documents?.find((d) => d.document_id === documentId)
    const at = doc?.chunk_ids.indexOf(citation.chunk) ?? -1
    return at >= 0 ? at + 1 : null
  }

  const submit = async () => {
    if (!pickedDocs.length) { toast.warn(D.needDoc); return }
    setSubmitting(true)
    try {
      const out = await api.caliberLedger.extractDraft({
        documents: pickedDocs,
        ...(datasourceId ? { datasource_id: datasourceId } : {}),
        ...(guidance.trim() ? { guidance: guidance.trim() } : {}),
        ...(fixedFilters.trim() ? { fixed_filters: fixedFilters.trim() } : {}),
      })
      toast.ok(D.started)
      setPickedDocs([])
      setGuidance('')
      setFixedFilters('')
      setDatasourceId('')
      setFilter('')
      loadList()
      void open(out.draft_id)
    } catch (e) {
      toast.error(e)
    } finally {
      setSubmitting(false)
    }
  }

  const resolve = async (draft: CaliberDraft) => {
    if (!pickedQuestions.length) { toast.warn(D.resolveNeed); return }
    // 已接管的草稿：改哪张卡要明确（详情接口给的 card_id）；图上有多张可写的卡又认不出唯一目标时不给改，
    // 免得按「第一张」猜（K02）
    if (draft.revision != null && draft.workflow_id && !draft.card_id) {
      toast.warn(draft.cards && draft.cards.length > 1 ? D.cardAmbiguous : D.cardMissing)
      return
    }
    setBusy(`resolve:${draft.id}`)
    // 消问题是按集合做的：去空白、去重、排序之后再发，勾选顺序变一下不该被当成另一次请求（K02 的幂等指纹
    // 服务端也按同一份归一算）
    const questions = [...new Set(pickedQuestions.map((q) => q.trim()).filter(Boolean))].sort()
    // 同一批勾选反复提交（网络失败后重试）用同一个幂等键：服务端认得出是同一件事，不会多存一版
    const signature = `${draft.id}|${questions.join('\u0000')}`
    if (pendingWrite.current.signature !== signature) {
      pendingWrite.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        resolved: questions,
        ...(draft.revision != null ? { expected_revision: draft.revision } : {}),
        ...(draft.card_id ? { target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id } } : {}),
        request_id: pendingWrite.current.requestId,
      })
      setDetail(fresh)
      toast.ok(D.resolved(questions.length))
      setPickedQuestions([])
      // 这一批已经落库：换一个键，别让下一次（重新勾选）落到同一个幂等键上
      pendingWrite.current = { signature: '', requestId: '' }
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        // 有人在别处先改过：这次没写进去，勾选一个不丢、也不拿新版本自动重发；说清现况，人确认后再点一次
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        toast.warn(D.writeStale(Number.isFinite(told) ? told : null, draft.revision ?? null))
        // 只把详情刷到最新（拿新的基准版本），不清空勾选、不重置页面
        void refreshDetail(draft.id)
      } else {
        toast.error(e)
      }
    } finally {
      setBusy(null)
    }
  }

  const attach = async (draft: CaliberDraft) => {
    const id = runId.trim()
    if (!id) { toast.warn(D.attachNeedRun); return }
    setBusy(`attach:${draft.id}`)
    try {
      const out = await api.caliberLedger.attachDraft(draft.id, id)
      setAttachOut(out)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.attached(out.version))
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const dismiss = async (draft: CaliberDraft) => {
    const note = await promptDialog({
      title: D.dismissTitle, label: D.dismissNote, placeholder: D.dismissPlaceholder, confirmLabel: D.dismissConfirm,
    })
    if (note == null) return
    setBusy(`dismiss:${draft.id}`)
    try {
      await api.caliberLedger.dismissDraft(draft.id, note.trim() || undefined)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.dismissed)
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const toggleQuestion = (question: string, checked: boolean) => {
    setPickedQuestions((cur) => (checked ? [...cur, question] : cur.filter((q) => q !== question)))
  }

  const questionList = (questions: string[] | undefined, prefix: string) => {
    const list = (questions ?? []).filter((q) => q.trim())
    if (!list.length) return null
    return (
      <ul className="mt-1 space-y-0.5">
        {list.map((q, i) => (
          <li key={`${prefix}:${i}`}>
            <label className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim">
              <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={pickedQuestions.includes(q)}
                     onChange={(e) => toggleQuestion(q, e.target.checked)} />
              <span className="min-w-0 [overflow-wrap:anywhere]">{q}</span>
            </label>
          </li>
        ))}
      </ul>
    )
  }

  const citationRow = (citation: DraftCitation, key: string, metricName: string) => {
    const chunk = chunks[citation.chunk]
    const ordinal = paragraphOf(citation)
    return (
      <li key={key}>
        <details className="rounded border bg-elev px-2 py-1">
          <summary className="cursor-pointer text-2xs leading-relaxed text-dim">
            <span className="[overflow-wrap:anywhere]">「{citation.quote}」</span>
            {ordinal != null && <span className="ml-1.5 text-faint">· {D.paragraph(ordinal)}</span>}
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-2xs leading-relaxed text-faint">
            {chunk
              ? <QuoteBody content={chunk.content} span={citation.span} quote={citation.quote} />
              : D.citationMissing}
          </p>
          <p className="mt-0.5 text-[10px] text-faint">{metricName}</p>
        </details>
      </li>
    )
  }

  /**
   * 按填好的入口字段再试运行一次：自动试跑缺必填输入时停在 needs_input，人在这里补上。
   * 带 expect 时这一条会落成一条带独立期望的验证记录（对得上就是对得上，对不上照实记）；不带就是只记试跑结果。
   * 入口字段的写法变化只影响这一次请求的参数，不会创建口径定义版本。
   */
  const runTrial = async (draft: CaliberDraft, expect?: { metrics: Record<string, unknown>; source: DraftExpectationSource }) => {
    const fields = draft.entry_fields ?? []
    if (!entryReady(fields, entryValues)) return
    setTrialStarting(true)
    setTrialError(null)
    try {
      const out = await api.caliberLedger.runDraftTrial(draft.id, entryInputs(fields, entryValues), expect)
      setTrial(out)
      setTrialTimedOut(false)
      if (out.status === 'running' && out.run_id) setTrialPollId(draft.id)
      const fresh = await api.caliberLedger.draft(draft.id)
      setDetail((cur) => cur?.id === fresh.id ? fresh : cur)
      loadValidations(draft.id)
    } catch (e) {
      setTrialError(e)
    } finally {
      setTrialStarting(false)
    }
  }

  /**
   * 保存这次业务规则的修改：把**所有**受影响指标的字段操作放在一次请求里（PATCH 的 operations），带上读到的
   * 基准版本、目标卡和幂等键。一次原子变更：全成或全不成；回执（change）说清落成哪一版、绑定同步到哪一步。
   * 被拒（基准版本过期）时什么都不改：这里的写法选择留着，刷新详情拿到新版本后由人显式再点一次。
   */
  const saveRules = async (draft: CaliberDraft, operations: DraftOperation[]) => {
    if (!operations.length) { toast.warn(R.saveNeedChange); return }
    if (!draft.card_id || draft.revision == null) { toast.warn(R.noCard); return }
    setRuleBusy('save')
    setRuleError(null)
    // 同一批操作反复提交（网络断了、手点重试）用同一个幂等键：服务端认得出是同一件事，不会多存一版
    const signature = `${draft.id}|${JSON.stringify(operations)}`
    if (rulePending.current.signature !== signature) {
      rulePending.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations,
        expected_revision: draft.revision,
        request_id: rulePending.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id },
      })
      setDetail(fresh)
      setRuleResult(fresh.change ?? null)
      setRuleReplayed(!!fresh.replayed)
      setRuleStale(null)
      // 撤销按**保存前**那份能力描述里的值构造逆操作，所以用这一版的 binding，不用刚回来的
      setUndoRecord({ operations: undoOperations(draft.binding, operations), count: operations.length })
      setRuleValues(ruleInitialValues(fresh.binding))
      rulePending.current = { signature: '', requestId: '' }
      toast.ok(R.saved(fresh.change?.revision ?? fresh.revision ?? null))
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        // 有人在别处先改过：这次没写进去，选择一个不丢、也不拿新版本自动重发；刷新详情只是为了拿到新的基准版本
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        setRuleStale({ current: Number.isFinite(told) ? told : null, base: draft.revision ?? null })
        void refreshDetail(draft.id)
      } else {
        setRuleError(e)
      }
    } finally {
      setRuleBusy(null)
    }
  }

  /** 撤销这次修改：逆操作 + 当前版本 + 新的请求键，再新增一版；不是整份覆盖，也不改已经存下的版本 */
  const undoRules = async (draft: CaliberDraft, record: { operations: DraftOperation[]; count: number }) => {
    if (draft.revision == null || !record.operations.length) return
    setRuleBusy('undo')
    setRuleError(null)
    // 撤销也按内容认一次：网络断了再点一次是同一件事，服务端认得出，不会多存一版
    const signature = `undo|${draft.id}|${JSON.stringify(record.operations)}`
    if (rulePending.current.signature !== signature) {
      rulePending.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations: record.operations,
        expected_revision: draft.revision,
        request_id: rulePending.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id ?? undefined },
      })
      setDetail(fresh)
      setRuleResult(fresh.change ?? null)
      setRuleReplayed(!!fresh.replayed)
      setRuleStale(null)
      setUndoRecord(null)
      setRuleValues(ruleInitialValues(fresh.binding))
      rulePending.current = { signature: '', requestId: '' }
      toast.ok(R.undone(fresh.change?.revision ?? fresh.revision ?? null))
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        setRuleStale({ current: Number.isFinite(told) ? told : null, base: draft.revision ?? null })
        void refreshDetail(draft.id)
      } else {
        setRuleError(e)
      }
    } finally {
      setRuleBusy(null)
    }
  }

  /**
   * 重新读一遍文档（K11）：在服务端重抽候选 R，和现在的内容 L、上次被接受的导入值 B 逐字段比。
   * **不改 L、不改 B**——真正落到图上的只有下面的「接受选定字段」。带幂等键：同一份草稿 + 同一个键只建一次。
   */
  const createImport = async (draft: CaliberDraft) => {
    if (!draft.workflow_id) { toast.warn(I.readOnly); return }
    setImportBusy('create')
    setImportError(null)
    setImportStale(null)
    setImportResult(null)
    setImportAccepted(false)
    setImportRejected(false)
    const signature = `import|${draft.id}|${draft.revision ?? ''}`
    if (importRequest.current.signature !== signature) {
      importRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const run = await api.caliberLedger.createImport(draft.id, importRequest.current.requestId)
      putImport(run)
      setImportId(run.run_id)
      setImportPicks({})
      setAlignPicks({})
      importRequest.current = { signature: '', requestId: '' }
      if (run.status === 'queued' || run.status === 'running') setImportPollId(run.run_id)
      toast.ok(I.started)
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }

  /**
   * 人工把一条工作项对到图上某条指标（空串是取消对齐）。baseline_unknown 的每一条、还有同名不同范围（引的原文
   * 不是同一段）都要人在这里定：机器不按名字猜「这说的是同一条指标」。对齐只重算三方，不改 L、不改 B。
   */
  const alignImportItem = async (run: DraftImportRun, item: DraftImportItem, metricId: string) => {
    setImportBusy(`align:${item.item_id}`)
    setImportError(null)
    try {
      const fresh = await api.caliberLedger.alignImportItem(run.run_id, item.item_id, metricId)
      putImport(fresh)
      // 换了对哪一条，这条上旧的选择就不适用了：只清这一条的，别的地方不动
      setImportPicks((cur) => {
        const next: Record<string, 'accept' | 'reject'> = {}
        for (const [key, value] of Object.entries(cur)) if (!key.startsWith(`${item.item_id}\u0000`)) next[key] = value
        return next
      })
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }

  /**
   * 重新读一次这一次导入的现状：逐字段的三方（B / L / R）是服务端按**现在**的图现算的，重新读才知道自己看的
   * 还是不是最新的。顺手把不再适用的选择裁掉——不拿旧的选择去提交。
   */
  const reloadImportRun = useCallback(async (runId: string): Promise<DraftImportRun | null> => {
    try {
      const fresh = await api.caliberLedger.importRun(runId)
      putImport(fresh)
      setImportPicks((cur) => prunePicks(cur, fresh))
      return fresh
    } catch (e) {
      setImportError(e)
      return null
    }
  }, [putImport])

  /**
   * 服务端说这次导入比对的还是旧的 L（409 import_stale_l）之后的恢复：按**现在的** L / B 重算这次导入还没决定的
   * 三方（人处置过的决定不动），把现状交回来。不能只把基准版本更新到最新一版就覆盖上去。
   */
  const refreshImportRun = useCallback(async (runId: string): Promise<DraftImportRun | null> => {
    try {
      const fresh = await api.caliberLedger.refreshImport(runId)
      putImport(fresh)
      setImportPicks((cur) => prunePicks(cur, fresh))
      return fresh
    } catch (e) {
      setImportError(e)
      return null
    }
  }, [putImport])

  /**
   * 接受选定的格子：一次提交（带读到的基准版本和幂等键）→ 图新增一版 + 按被接受的格子推进 B。
   * 每条按**工作项身份**（item_id）指认，带上要落的那条指标和**明确列出来的**字段路径；不是这里列出来的格子，
   * 一个都不动。被拒（基准版本过期、或者这次比对的是旧的 L）时选好的格子原样留着，先把这一次导入的现状重新读
   * 回来再让人决定——不拿旧 L 直接覆盖。
   */
  const acceptImportFields = async (draft: CaliberDraft, run: DraftImportRun,
                                    items: { item_id: string; metric_id?: string; fields: string[] }[]) => {
    const picked = items.filter((item) => item.fields.length)
    if (!picked.length) { toast.warn(I.nothingSelected); return }
    if (draft.revision == null) { toast.warn(I.needRevision); return }
    setImportBusy('accept')
    setImportError(null)
    setImportStale(null)
    setImportResult(null)
    setImportRejected(false)
    const signature = `accept|${run.run_id}|${JSON.stringify(picked)}`
    if (importRequest.current.signature !== signature) {
      importRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const out = await api.caliberLedger.acceptImport(run.run_id, {
        items: picked, expected_revision: draft.revision, request_id: importRequest.current.requestId,
      })
      putImport(out)
      setImportResult(out)
      setImportAccepted(true)
      setImportPicks({})
      importRequest.current = { signature: '', requestId: '' }
      toast.ok(I.accepted(out.revision))
      // 图和 B 是同一次提交里推进的：详情、验证状态和列表都跟着刷
      void refreshDetail(draft.id)
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const code = e instanceof ApiError && e.status === 409 ? e.code : null
      if (code === 'stale_revision' || code === 'import_stale_l') {
        const body = e instanceof ApiError ? (e.body as { current_version?: unknown
          fields?: { path?: string }[] } | undefined) : undefined
        const told = Number(body?.current_version)
        // 服务端点名的就是这几格：把字段说法列出来，人一眼看出是哪一格对不上了
        const names = (body?.fields ?? []).map((row) => fieldLabel(String(row.path ?? ''), draft.binding)).join('、')
        setImportStale({
          code: code === 'import_stale_l' ? 'import_stale_l' : 'stale_revision',
          current: Number.isFinite(told) ? told : null, base: draft.revision ?? null, names,
        })
        // 写不进去：先把这一次导入的现状重新读回来，再读一次详情拿新的基准版本。
        // import_stale_l 是「三方算的是旧的 L」：按现在的 L / B **重算三方**（refresh），不是只更新版本号就覆盖。
        if (code === 'import_stale_l') await refreshImportRun(run.run_id)
        else await reloadImportRun(run.run_id)
        void refreshDetail(draft.id)
      } else {
        setImportError(e)
      }
    } finally {
      setImportBusy(null)
    }
  }

  /** 拒绝选定的格子：保持 L 不动，也不推进 B。同样按工作项身份指认，只拒这里明确列出来的格子 */
  const rejectImportFields = async (run: DraftImportRun,
                                    items: { item_id: string; metric_id?: string; seq?: number; fields: string[] }[]) => {
    const picked = items.filter((item) => item.fields.length)
    if (!picked.length) { toast.warn(I.nothingSelected); return }
    setImportBusy('reject')
    setImportError(null)
    setImportResult(null)
    setImportAccepted(false)
    try {
      const fresh = await api.caliberLedger.rejectImport(run.run_id, {
        items: picked, ...(importNote.trim() ? { note: importNote.trim() } : {}),
      })
      putImport(fresh)
      setImportRejected(true)
      setImportPicks({})
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }

  /** 显式恢复被中断的那一次导入：续做还没完成的工作项，已接受 / 拒绝的不动、不重复建版 */
  const resumeImportRun = async (run: DraftImportRun) => {
    setImportBusy('resume')
    setImportError(null)
    try {
      const fresh = await api.caliberLedger.resumeImport(run.run_id)
      putImport(fresh)
      setImportPollId(fresh.run_id)
      toast.ok(I.resumed)
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }

  /**
   * 用一句话看影响（K12）：只调**只读**的提案接口，不落库、不新增版本。认出来的条件落成 K03 同一形状的 operations，
   * 这里只把它翻成「哪条指标、哪一项写法、现在什么 → 保存后什么」；说不清时只有澄清项，一条操作都不产出。
   *
   * 提案和**产出它的那一段原文**一起记下来（nlPreviewOf）：确认时按那一段原文发、服务端再解析一次得到的还是同一批
   * 操作；页面显示的（A）和实际落下的（B）不会是两句不同的话。
   */
  const previewNl = async (draft: CaliberDraft) => {
    const instruction = nlText.trim()
    if (!instruction) { toast.warn(N.needText); return }
    if (!draft.card_id) { toast.warn(draft.cards && draft.cards.length > 1 ? D.cardAmbiguous : D.cardMissing); return }
    setNlBusy('preview')
    setNlError(null)
    setNlResult(null)
    setNlPreview(null)
    try {
      const out = await api.caliberLedger.changeProposal(draft.id, {
        instruction,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id },
      })
      setNlPreview(out.proposal)
      setNlPreviewOf(instruction)
      setNlNotice(out.proposal.ready ? 'preview' : 'clarify')
      // 同一句话反复确认是同一件事：请求键按内容定，重发不会多存一版
      const signature = `nl|${draft.id}|${instruction}`
      if (nlRequest.current.signature !== signature) {
        nlRequest.current = { signature, requestId: newRequestId() }
      }
    } catch (e) {
      setNlNotice('idle')
      setNlError(e)
    } finally {
      setNlBusy(null)
    }
  }

  /**
   * 确认之后才落版（K12）：confirm=true 走服务端的薄适配，内部仍是现有变更服务（版本 CAS、幂等键、绑定同步、
   * 校验一条不少）。
   *
   * 发出去的是**预览时那一段原文**，不是输入框里现在的内容：输入改过就必须重新预览（nlTouched），否则页面显示的
   * 是 A、落下的会是 B。落版之后另比一次服务端落下的操作和预览里的是不是同一批，不一样就照实提示。
   * 失败时页面上什么都不改——输入和预览都留着，由人决定下一步。
   */
  const confirmNl = async (draft: CaliberDraft) => {
    if (!nlPreviewOf || !nlPreview?.ready) { toast.warn(N.needConfirm); return }
    if (nlText.trim() !== nlPreviewOf) { toast.warn(N.needPreviewAgain); return }
    if (draft.revision == null) { toast.warn(N.noRevision); return }
    setNlBusy('confirm')
    setNlError(null)
    const signature = `nl|${draft.id}|${nlPreviewOf}`
    if (nlRequest.current.signature !== signature) {
      nlRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const out = await api.caliberLedger.nlChange(draft.id, {
        instruction: nlPreviewOf,
        confirm: true,
        expected_revision: draft.revision,
        request_id: nlRequest.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id ?? undefined },
      })
      const fresh = 'proposal' in out ? null : out
      const revision = fresh?.change?.revision ?? fresh?.revision ?? null
      // 服务端这次落下的操作和预览里的是不是同一批（同一段原文应当解析出同一批）；判不了就是 null
      const same = sameOperations(nlPreview.operations, fresh?.change?.operations)
      setNlResult({
        revision,
        invalidated: fresh?.change?.validation?.invalidated ?? false,
        replayed: !!fresh?.replayed,
        mismatch: same === null ? null : !same,
      })
      setNlNotice('applied')
      nlRequest.current = { signature: '', requestId: '' }
      toast.ok(N.applied(revision))
      if (fresh) {
        setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
        setRuleValues(ruleInitialValues(fresh.binding))
      }
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      setNlError(e)
    } finally {
      setNlBusy(null)
    }
  }

  /**
   * 对一条验证记录做审阅：只调服务端那个接口（审批人由服务端身份定，界面代写不了），拿回来的决定照原样显示
   * ——谁、什么时候、覆盖不覆盖当前版本。样例对得上不会自动变成批准，这里也不在前端改结论。
   */
  const reviewRecord = async (draft: CaliberDraft, recordId: string, decision: 'confirmed' | 'rejected') => {
    setReviewBusy(recordId)
    setReviewError(null)
    try {
      const note = (reviewNotes[recordId] ?? '').trim()
      const out = await api.caliberLedger.reviewValidation(draft.id, recordId,
        { decision, ...(note ? { note } : {}) })
      setReviewOut((cur) => ({ ...cur, [recordId]: out.record.review ?? null }))
      setReviewNotes((cur) => ({ ...cur, [recordId]: '' }))
      toast.ok(V.reviewDone(decision === 'confirmed' ? V.reviewConfirmed : V.reviewRejected))
      loadValidations(draft.id)
      void refreshDetail(draft.id)
    } catch (e) {
      setReviewError(e)
    } finally {
      setReviewBusy(null)
    }
  }

  const changeDraftJob = async (draft: CaliberDraft, action: 'retry' | 'stop') => {
    const seq = openSeq.current
    setJobBusy(draft.id)
    setJobError(null)
    try {
      if (action === 'retry') await api.caliberLedger.retryDraft(draft.id)
      else await api.caliberLedger.stopDraft(draft.id)
      const fresh = await api.caliberLedger.draft(draft.id)
      if (seq === openSeq.current) {
        setDetail(fresh)
        if (fresh.status === 'drafting') { setPollTimedOut(false); setPollingId(draft.id) }
        else setPollingId(null)
      }
      await loadList()
    } catch (error) {
      if (seq === openSeq.current) setJobError(error)
    } finally {
      setJobBusy((id) => id === draft.id ? null : id)
    }
  }

  /**
   * 业务规则（K06 的工作台）：服务端的能力描述说得出这张卡能按字段改哪几项时才开表单。改完先列「会改到哪几条指标、
   * 现在什么写法 → 保存后什么写法」，再把所有受影响指标的字段操作**一次**提交（原子变更）；回执说清落成哪一版、
   * 取数同步到哪一步、旧验证怎么失效，并按保存前的值给出「撤销这次修改」。这块只写业务说法：取数语句、内部编号、
   * 字段路径和 JSON 都不上来。
   */
  const renderRules = (draft: CaliberDraft) => {
    const binding = draft.binding
    if (!binding) return null
    const fields = binding.editable_fields ?? []
    const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
    const names = new Map(metrics.map((metric) => [metric.id as string, metric.name]))
    const declared = new Set(Object.keys(binding.metric_values ?? {}))
    const plan = rulePlan(binding, ruleValues)
    const changes = plan.changes.map((change) => ({ ...change, name: names.get(change.metricId) ?? change.metricId }))
    const readOnly = !draft.workflow_id ? R.noWorkflow
      : !draft.card_id ? R.noCard
        : draft.revision == null ? R.noRevision
          : declared.size === 0 ? R.unbound : null
    const label = R.status[binding.status] ?? binding.status
    const tone = binding.status === 'aligned' ? 'var(--ok)'
      : binding.status === 'unbound' ? 'var(--text-faint)' : 'var(--warn)'
    const saved = ruleResult?.binding
    const untouched = metrics.length - declared.size
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-rules={binding.status}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{R.title}</h3>
          <span className="text-2xs" data-draft-rules-status={binding.status} style={{ color: tone }}>{label}</span>
          <span className="text-[10px] text-faint">{R.hint}</span>
        </div>
        {binding.status === 'aligned' ? (
          <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-rules-note="">{R.alignedNote}</p>
        ) : (
          <>
            <p className="mt-1 text-2xs leading-relaxed text-[var(--warn)]" data-draft-rules-reason="">
              {R.reason}：{binding.reason}
            </p>
            {binding.fix && binding.fix !== 'none' && (
              <p className="mt-0.5 text-2xs leading-relaxed text-dim" data-draft-rules-todo="">
                {R.fixLabel}：{R.fix[binding.fix] ?? binding.fix}
              </p>
            )}
          </>
        )}

        {readOnly ? (
          <p className="mt-1.5 text-2xs leading-relaxed text-faint" data-draft-rules-readonly="">{R.readOnly}：{readOnly}</p>
        ) : (
          <>
            <ul className="mt-1.5 space-y-1.5">
              {fields.map((field) => {
                const declaring = declaringMetrics(binding, field.field)
                const distinct = [...new Set(declaring.map((id) => binding.metric_values[id][field.field]))]
                const chosen = ruleValues[field.field] ?? ''
                return (
                  <li key={field.field} className="flex flex-wrap items-center gap-x-2 gap-y-1"
                      data-draft-rule-field={field.field}>
                    <span className="text-2xs font-medium text-dim">{field.label}</span>
                    <select className="field h-[26px] text-2xs" value={chosen} aria-label={field.label}
                            disabled={!declaring.length || ruleBusy != null} data-draft-rule-select={field.field}
                            title={distinct.length > 1 ? R.mixedHint : undefined}
                            onChange={(e) => setRuleValues((cur) => ({ ...cur, [field.field]: e.target.value }))}>
                      <option value="">{R.keep}</option>
                      {field.values.map((value) => (
                        <option key={value} value={value}>{choiceLabel(field, value)}</option>
                      ))}
                    </select>
                    <span className="text-[10px] text-faint" data-draft-rule-current={field.field}>
                      {!declaring.length ? R.fieldNone
                        : distinct.length > 1 ? R.mixed
                          : `${R.before}：${choiceLabel(field, distinct[0])}`}
                    </span>
                    <span className="text-[10px] text-faint [overflow-wrap:anywhere]">{field.help}</span>
                  </li>
                )
              })}
            </ul>
            {untouched > 0 && (
              <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-rule-untouched={untouched}>
                {R.unaffected(untouched)}。{R.unaffectedHint}
              </p>
            )}

            <div className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-rule-preview="">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-2xs font-semibold text-dim">{R.previewTitle}</span>
                <span className="tnum text-2xs text-faint" data-draft-rule-affected={changes.length}>
                  {changes.length ? R.affected(changes.length) : R.noChange}
                </span>
              </div>
              {changes.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {changes.map((change) => (
                    <li key={`${change.metricId}:${change.field.field}`} data-draft-rule-change={change.metricId}
                        className="text-2xs leading-relaxed text-dim">
                      <span className="font-medium [overflow-wrap:anywhere]">{change.name}</span>
                      <span className="text-faint"> · {change.field.label}：</span>
                      <span className="text-faint">{choiceLabel(change.field, change.from)}</span>
                      <span className="text-faint"> → </span>
                      <span>{choiceLabel(change.field, change.to)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-xs btn-primary" data-draft-rule-save=""
                      disabled={ruleBusy != null || !plan.operations.length}
                      onClick={() => void saveRules(draft, plan.operations)}>
                {ruleBusy === 'save' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
                {ruleBusy === 'save' ? R.saving : R.save}
              </button>
              {undoRecord && (
                <button type="button" className="btn btn-xs" data-draft-rule-undo="" title={R.undoHint}
                        disabled={ruleBusy != null}
                        onClick={() => void undoRules(draft, undoRecord)}>
                  {ruleBusy === 'undo' ? <Spinner size={11} /> : <Undo2 size={11} aria-hidden />}
                  {ruleBusy === 'undo' ? R.undoing : R.undo(undoRecord.count)}
                </button>
              )}
            </div>
          </>
        )}

        {ruleStale && (
          <div className="mt-1.5" data-draft-rule-stale="">
            <Notice tone="warn">{R.stale(ruleStale.current, ruleStale.base)}</Notice>
          </div>
        )}
        {ruleError != null && <div className="mt-1.5"><ErrorState error={ruleError} compact /></div>}
        {ruleReplayed && !ruleResult && (
          <div className="mt-1.5"><Notice tone="info">{R.replayed}</Notice></div>
        )}

        {ruleResult && (
          <div className="mt-1.5 rounded border bg-panel px-2 py-1.5 text-2xs leading-relaxed"
               data-draft-rule-result={ruleResult.applied ? 'applied' : 'none'}>
            <p className="text-dim" data-draft-rule-result-version="">
              {R.saved(ruleResult.revision)}
              {ruleReplayed && <span className="text-faint"> · {R.replayed}</span>}
            </p>
            {ruleResult.applied ? (
              <>
                <p data-draft-rule-binding={saved?.status ?? ''}>
                  {saved?.synced ? R.savedAligned : R.savedNotSynced}
                  {!saved?.synced && saved?.reason ? `（${saved.reason}）` : ''}
                </p>
                <p className="text-faint" data-draft-rule-invalidated="">{R.invalidated}</p>
                <p className="text-faint">{R.nextStep}</p>
                {!saved?.synced && saved?.fix && saved.fix !== 'none' && (
                  <p className="text-dim" data-draft-rule-fix="">
                    {R.fixLabel}：{R.fix[saved.fix] ?? saved.fix}。{R.fixNote}
                  </p>
                )}
              </>
            ) : (
              <p className="text-faint">{R.savedNoChange}</p>
            )}
          </div>
        )}
      </div>
    )
  }

  /**
   * 文档更新（K11 的增量重导入）：重抽候选 R，和现在的内容 L、上次被接受的导入值 B 逐字段比。R 只是候选——
   * 落到图上只有「接受选定字段」这一次显式动作（一次提交里新增一版图 + 按被接受的格子推进 B）。逐格写清三方
   * 的值、状态和为什么；漏提取不当删除（不能接受，L 上的原值原样留着）；没对齐上的工作项由人指定对到哪条指标。
   * 提交一律按**工作项身份**（item_id）指认、把要落的那几格**明确列出来**（不发 auto）；抽坏了的（failed）工作项
   * 一个接受都点不了，批量也不收它；已接受 / 拒绝过的格子不再被选。被中断的导入由人点「恢复」显式续跑。
   * 写不进去时（基准版本过期 stale_revision / 三方比的是旧的 L、import_stale_l）选好的格子一个不丢：前者重新读
   * 详情拿新版本，后者按现在的口径**重算三方**（refreshImport）再读回来，由人核对后再点一次——不拿旧 L 直接覆盖。
   * 这块只写业务说法，不出现取数语句、节点编号和字段路径。
   */
  const renderImports = (draft: CaliberDraft) => {
    if (!draft.workflow_id) return null
    const runs = importRuns ?? []
    const active = runs.find((run) => run.run_id === importId) ?? null
    const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
    const nameOf = (id: string) => metrics.find((metric) => metric.id === id)?.name ?? I.unknownMetric
    const baseline = validations?.field_baseline ?? draft.field_baseline
    const coverage = baseline?.coverage
    // 老工件没有 coverage 这一段：那时候 put_metrics 收的是整张卡，按 complete 处理（和 fieldbaseline.coverage_of 同一条规矩）；
    // 没有基线（baseline_unknown）时一律是读不出来，不能拿现在的内容当作它
    const coverageStatus = coverage?.status ?? (baseline?.status === 'known' ? 'complete' : 'unknown')
    const missingFields = Object.values(coverage?.metrics ?? {})
      .reduce((total, row) => total + (row.missing?.length ?? 0), 0)
    const uncoveredMetrics = coverage?.uncovered_metric_ids?.length ?? 0
    const coverageText = coverageStatus === 'complete' ? I.coverageComplete
      : coverageStatus === 'partial' ? I.coveragePartial(missingFields, uncoveredMetrics)
        : I.coverageUnknown
    // 这一次要提交的格子：按**现在**的现状现算——格子还在、还没决定过、能接受、工作项没抽坏才收；
    // 每条都带上工作项身份（item_id），同一个 metric_id 上有重复项时也不会指错
    const picking = active ? active.items.map((item) => ({
      item,
      paths: item.fields.filter((cell) => importPicks[pickKey(item.item_id, cell.path)] === 'accept'
        && cell.decided == null && cell.acceptable && itemAccepts(item)).map((cell) => cell.path),
      rejects: item.fields.filter((cell) => importPicks[pickKey(item.item_id, cell.path)] === 'reject'
        && cell.decided == null).map((cell) => cell.path),
    })) : []
    const acceptItems = picking.filter((row) => row.paths.length)
      .map((row) => ({ item_id: row.item.item_id, metric_id: row.item.metric_id, fields: row.paths }))
    const rejectItems = picking.filter((row) => row.rejects.length).map((row) => ({
      item_id: row.item.item_id, fields: row.rejects,
      ...(row.item.metric_id ? { metric_id: row.item.metric_id } : { seq: row.item.seq }),
    }))
    const acceptCount = acceptItems.reduce((total, item) => total + item.fields.length, 0)
    const rejectCount = rejectItems.reduce((total, item) => total + item.fields.length, 0)
    const resumable = active != null && (active.status === 'interrupted' || active.status === 'failed')
    const busy = importBusy != null
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-imports={active?.status ?? 'none'}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{I.title}</h3>
          <span className="text-[10px] text-faint">{I.hint}</span>
        </div>
        <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-coverage={coverageStatus}>
          {I.coverageLabel}：{coverageText}。{I.coverageHint}
        </p>

        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-xs" data-draft-import-create="" disabled={busy}
                  onClick={() => void createImport(draft)}>
            {importBusy === 'create' ? <Spinner size={11} /> : <FileSearch size={11} aria-hidden />}
            {importBusy === 'create' ? I.creating : I.create}
          </button>
          <span className="text-[10px] text-faint">{I.createHint}</span>
        </div>
        {importsError != null && <div className="mt-1"><ErrorState error={importsError} compact /></div>}

        {runs.length === 0 ? (
          <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-runs={0}>
            {I.runs}：{I.runsEmpty}
          </p>
        ) : (
          <div className="mt-2" data-draft-import-runs={runs.length}>
            <h4 className="text-2xs font-semibold text-faint">{I.runs}</h4>
            <ul className="mt-0.5 space-y-0.5">
              {runs.map((run) => (
                <li key={run.run_id}>
                  <button type="button" className={clsx('btn btn-xs w-full justify-start text-left', 'btn-ghost')}
                          aria-pressed={run.run_id === importId} data-draft-import-run={run.run_id}
                          onClick={() => setImportId(run.run_id)}>
                    <span className="[overflow-wrap:anywhere]">
                      {I.runLabel(run.created_at ? formatDateTime(run.created_at) : '—', run.items_decided, run.items_total)}
                    </span>
                    <span className="text-faint"> · {I.status[run.status] ?? run.status}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {active && (
          <div className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-import-detail={active.run_id}>
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
              <span className="font-medium text-dim" data-draft-import-status={active.status}>
                {I.status[active.status] ?? active.status}
              </span>
              {(active.status === 'queued' || active.status === 'running') && <Spinner size={11} />}
              <span className="text-faint">{I.itemsTotal(active.items_total)}</span>
              {active.conflicts.length > 0 && (
                <span className="text-[var(--warn)]" data-draft-import-conflicts={active.conflicts.length}>
                  {I.conflicts(active.conflicts.length)}
                </span>
              )}
              {active.created_at && <span className="text-faint">{I.createdAt(formatDateTime(active.created_at))}</span>}
            </div>
            {active.error && <Notice tone="warn">{active.error}</Notice>}
            {resumable && (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <button type="button" className="btn btn-xs" data-draft-import-resume="" disabled={busy}
                        onClick={() => void resumeImportRun(active)}>
                  {importBusy === 'resume' ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />}
                  {importBusy === 'resume' ? I.resuming : I.resume}
                </button>
                <span className="text-[10px] text-faint">{I.resumeHint}</span>
                <span className="tnum text-[10px] text-faint" data-draft-import-unfinished={unfinishedItems(active)}>
                  {I.unfinished(unfinishedItems(active))}
                </span>
              </div>
            )}

            {active.items.length === 0 ? (
              <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-empty="">{I.noItems}</p>
            ) : (
              <ul className="mt-1.5 space-y-2">
                {active.items.map((item) => {
                  const aligned = !!item.metric_id
                  const alignMode = String(item.align?.mode ?? '')
                  // 没对上要人指定；同名不同范围（身份对得上、引的原文不是同一段）也要人来定，所以 manual 一律开这个口
                  const needsAlign = !aligned || alignMode === 'manual'
                  const clean = cleanPaths(item)
                  return (
                    <li key={item.item_id} className="rounded border bg-elev px-2 py-1.5"
                        data-draft-import-item={item.metric_id || `#${item.seq}`} data-import-item-status={item.status}>
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
                        <span className="font-medium text-dim [overflow-wrap:anywhere]">{item.name || I.unknownMetric}</span>
                        {aligned && <span className="text-faint">· {nameOf(item.metric_id)}</span>}
                        <span className={clsx(item.status === 'accepted' ? 'text-[var(--ok)]'
                          : item.status === 'rejected' || item.status === 'failed' ? 'text-faint' : 'text-[var(--warn)]')}
                              data-draft-import-item-status-label={item.status}>
                          {I.itemStatus[item.status] ?? I.state[item.status] ?? item.status}
                        </span>
                      </div>

                      {needsAlign && (
                        <div className="mt-1 flex flex-wrap items-center gap-2" data-draft-import-align="">
                          <span className="text-2xs text-[var(--warn)]">{I.itemUnaligned}</span>
                          <select className="field h-[26px] text-2xs" data-draft-import-align-select={item.item_id}
                                  aria-label={I.align} disabled={busy}
                                  value={alignPicks[item.item_id] ?? item.metric_id}
                                  onChange={(e) => setAlignPicks((cur) => ({ ...cur, [item.item_id]: e.target.value }))}>
                            <option value="">{I.alignNone}</option>
                            {metrics.map((metric) => (
                              <option key={metric.id} value={metric.id as string}>{metric.name}</option>
                            ))}
                          </select>
                          <button type="button" className="btn btn-xs" data-draft-import-align-submit={item.item_id}
                                  disabled={busy} onClick={() => void alignImportItem(active, item, alignPicks[item.item_id] ?? '')}>
                            {importBusy === `align:${item.item_id}` ? <Spinner size={11} /> : null} {I.alignAction}
                          </button>
                          <span className="text-[10px] text-faint">{I.alignHint}</span>
                        </div>
                      )}

                      {(alignMode === 'manual' || alignMode === 'new') && (
                        <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]"
                           data-draft-import-reason={alignMode}>
                          {I.reasonLabel}：{I.reason[alignMode] ?? alignMode}
                          {item.align?.reason ? ` · ${item.align.reason}` : ''}
                        </p>
                      )}
                      {item.error && <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]">{item.error}</p>}

                      <ul className="mt-1 space-y-1">
                        {item.fields.map((cell) => {
                          const choice = importPicks[pickKey(item.item_id, cell.path)] ?? ''
                          // 抽坏了的（failed）和没对齐上的一律不能接受；接受过的也不用再选
                          const canAccept = itemAccepts(item) && cell.acceptable && cell.decided !== 'accepted'
                          const values: [string, string | null, string][] = [
                            ['b', cellText(cell, 'b', draft.binding), I.b],
                            ['l', cellText(cell, 'l', draft.binding), I.l],
                            ['r', cellText(cell, 'r', draft.binding), I.r],
                          ]
                          return (
                            <li key={cell.path} className="text-2xs leading-relaxed"
                                data-draft-import-field={`${item.item_id}:${cell.path}`} data-import-field-state={cell.state}>
                              <div className="flex flex-wrap items-baseline gap-x-2">
                                <span className="font-medium text-dim">{fieldLabel(cell.path, draft.binding)}</span>
                                <span className="text-faint" data-draft-import-field-state-label={cell.state}>
                                  {I.state[cell.state] ?? cell.state}
                                </span>
                                {cell.decided === 'accepted' && <span className="text-[var(--ok)]">{I.accept}</span>}
                                {cell.decided === 'rejected' && <span className="text-faint">{I.reject}</span>}
                                {cell.state === 'missing' && (
                                  <span className="text-[var(--warn)]" data-draft-import-missing="">
                                    {I.missingNoDelete}
                                  </span>
                                )}
                              </div>
                              <ul className="mt-0.5 space-y-0.5 pl-2">
                                {values.map(([key, text, label]) => text != null && (
                                  <li key={key} className="text-faint" data-draft-import-value={key}>
                                    {label}：<span className="text-dim [overflow-wrap:anywhere]">{text}</span>
                                  </li>
                                ))}
                              </ul>
                              <p className="mt-0.5 text-faint">{I.stateHint[cell.state] ?? ''}</p>
                              {cell.decided == null && (
                                <div className="mt-0.5 flex flex-wrap items-center gap-2">
                                  <label className="flex items-center gap-1 text-dim">
                                    <input type="radio" className="accent-[var(--accent)]"
                                           name={`pick:${item.item_id}:${cell.path}`} disabled={!canAccept || busy}
                                           data-draft-import-pick-accept={`${item.item_id}:${cell.path}`}
                                           checked={choice === 'accept'}
                                           onChange={() => setImportPicks((cur) => ({
                                             ...cur, [pickKey(item.item_id, cell.path)]: 'accept' }))} />
                                    {I.accept}
                                  </label>
                                  <label className="flex items-center gap-1 text-dim">
                                    <input type="radio" className="accent-[var(--accent)]"
                                           name={`pick:${item.item_id}:${cell.path}`} disabled={busy}
                                           data-draft-import-pick-reject={`${item.item_id}:${cell.path}`}
                                           checked={choice === 'reject'}
                                           onChange={() => setImportPicks((cur) => ({
                                             ...cur, [pickKey(item.item_id, cell.path)]: 'reject' }))} />
                                    {I.reject}
                                  </label>
                                </div>
                              )}
                            </li>
                          )
                        })}
                      </ul>

                      {clean.length > 0 && (
                        <div className="mt-1">
                          {/* 一次收掉这条上无冲突的几格：把这几格**明确列出来**发（不发 auto），页面列了几格就只接受几格 */}
                          <button type="button" className="btn btn-xs" data-draft-import-accept-clean={item.item_id}
                                  disabled={busy}
                                  onClick={() => void acceptImportFields(draft, active,
                                    [{ item_id: item.item_id, metric_id: item.metric_id, fields: clean }])}>
                            {I.acceptClean(clean.length)}
                          </button>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}

            {active.items.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button type="button" className="btn btn-xs btn-primary" data-draft-import-accept=""
                        disabled={busy || !acceptCount}
                        onClick={() => void acceptImportFields(draft, active, acceptItems)}>
                  {importBusy === 'accept' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
                  {importBusy === 'accept' ? I.accepting : I.acceptSelected(acceptCount)}
                </button>
                <button type="button" className="btn btn-xs" data-draft-import-reject="" disabled={busy || !rejectCount}
                        onClick={() => void rejectImportFields(active, rejectItems)}>
                  {importBusy === 'reject' ? <Spinner size={11} /> : <X size={11} aria-hidden />}
                  {importBusy === 'reject' ? I.rejecting : I.rejectSelected(rejectCount)}
                </button>
                {!acceptCount && !rejectCount && <span className="text-[10px] text-faint">{I.nothingSelected}</span>}
                <input className="field h-[26px] w-44 text-2xs" data-draft-import-note="" aria-label={I.reject}
                       placeholder={I.notePlaceholder} value={importNote} disabled={busy}
                       onChange={(e) => setImportNote(e.target.value)} />
              </div>
            )}

            {importStale && (
              <div className="mt-1.5" data-draft-import-stale={importStale.code}>
                <Notice tone="warn">
                  {importStale.code === 'import_stale_l'
                    ? I.staleL(importStale.names)
                    : I.stale(importStale.current, importStale.base)}
                </Notice>
              </div>
            )}
            {importError != null && <div className="mt-1"><ErrorState error={importError} compact /></div>}
            {(importAccepted || importRejected) && (
              <div className="mt-1.5 rounded border bg-elev px-2 py-1 text-2xs leading-relaxed"
                   data-draft-import-result={importAccepted ? 'accepted' : 'rejected'}>
                {importAccepted && (
                  <>
                    <p className="text-dim" data-draft-import-result-version={importResult?.revision ?? ''}>
                      {I.accepted(importResult?.revision ?? null)}
                      {importResult?.replayed && <span className="text-faint"> · {I.replayed}</span>}
                    </p>
                    <p className="text-faint">{I.acceptedHint}</p>
                  </>
                )}
                {importRejected && <p className="text-dim">{I.rejected}</p>}
              </div>
            )}
          </div>
        )}
      </div>
    )
  }

  /**
   * 用一句话改口径（K12）：先调只读的提案接口，把 operations 翻成「哪条指标、哪一项写法、现在什么 → 保存后什么」，
   * 不摆 JSON、节点编号和字段路径。含糊的业务条件先显示澄清项，一条操作都不产出——确认（confirm=true）之前，
   * 服务端一个字节都不写；落版失败时这里的输入和预览原样留着。
   */
  const renderNl = (draft: CaliberDraft) => {
    if (!draft.workflow_id) return null
    const proposal = nlPreview
    const changes = proposal?.ready ? nlChanges(draft, proposal.operations) : []
    const clarifications = proposal?.clarifications ?? []
    // 输入改过之后这份提案就不作数了：必须重新预览（确认的是预览时那一段原文，不是输入框里现在的字）
    const touched = !!nlPreviewOf && nlText.trim() !== nlPreviewOf
    const confirmable = !touched && proposal?.ready && changes.length > 0 && draft.revision != null
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-nl={nlNotice} data-draft-nl-touched={touched}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{N.title}</h3>
          <span className="text-[10px] text-faint">{N.hint}</span>
        </div>
        <div className="mt-1.5 flex flex-wrap items-start gap-2">
          <textarea className="field min-h-[40px] flex-1 text-2xs" rows={2} data-draft-nl-input=""
                    placeholder={N.placeholder} value={nlText} disabled={nlBusy != null}
                    onChange={(e) => setNlText(e.target.value)} />
          <button type="button" className="btn btn-xs" data-draft-nl-preview="" disabled={nlBusy != null}
                  onClick={() => void previewNl(draft)}>
            {nlBusy === 'preview' ? <Spinner size={11} /> : <Sparkles size={11} aria-hidden />}
            {nlBusy === 'preview' ? N.previewing : N.preview}
          </button>
        </div>

        {clarifications.length > 0 && (
          <div className="mt-1.5 rounded border bg-panel px-2 py-1.5" data-draft-nl-clarify={clarifications.length}>
            <p className="text-2xs font-medium text-[var(--warn)]">{N.clarifyTitle}</p>
            <ul className="mt-0.5 space-y-0.5 text-2xs leading-relaxed text-dim">
              {clarifications.map((item, index) => (
                <li key={`${item.kind}:${index}`} data-draft-nl-clarify-item={item.kind}>
                  <span className="[overflow-wrap:anywhere]">{item.question}</span>
                  {item.kind === 'no_match' && <span className="text-faint"> · {N.clarifyNoMatch}</span>}
                  {!!item.options?.length && (
                    <span className="text-faint">
                      {' · '}{item.options.map((option) => option.label).join('、')}
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <p className="mt-0.5 text-2xs text-faint">{N.clarifyHint}</p>
          </div>
        )}

        {changes.length > 0 && (
          <div className="mt-1.5 rounded border bg-panel px-2 py-1.5" data-draft-nl-changes={changes.length}>
            <p className="tnum text-2xs font-medium text-dim" data-draft-nl-count={changes.length}>
              {N.changeCount(changes.length)}
            </p>
            {/* 这份影响是按哪一段原文算出来的：输入改过之后标出来，免得以为看到的是现在这句话 */}
            <p className="text-[10px] text-faint" data-draft-nl-of={nlPreviewOf ? 'given' : 'none'}>
              {touched ? N.changedNotice : N.previewedAs(nlPreviewOf)}
            </p>
            <ul className="mt-0.5 space-y-0.5">
              {changes.map((change, index) => (
                <li key={`${change.metricId}:${change.label}:${index}`} className="text-2xs leading-relaxed text-dim"
                    data-draft-nl-change={change.metricId}>
                  <span className="font-medium [overflow-wrap:anywhere]">{change.name}</span>
                  <span className="text-faint"> · {change.label}：</span>
                  <span className="text-faint">{N.now} {change.from}</span>
                  <span className="text-faint"> → </span>
                  <span>{N.after} {change.to}</span>
                </li>
              ))}
            </ul>
            {confirmable ? (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <button type="button" className="btn btn-xs btn-primary" data-draft-nl-confirm=""
                        disabled={nlBusy != null} onClick={() => void confirmNl(draft)}>
                  {nlBusy === 'confirm' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
                  {nlBusy === 'confirm' ? N.confirming : N.confirm}
                </button>
                <span className="text-[10px] text-faint">{N.confirmHint}</span>
              </div>
            ) : touched ? (
              <p className="mt-1 text-2xs text-[var(--warn)]" data-draft-nl-repreview="">{N.needPreviewAgain}</p>
            ) : (
              <p className="mt-1 text-2xs text-faint" data-draft-nl-norevision="">{N.noRevision}</p>
            )}
          </div>
        )}

        {nlError != null && <div className="mt-1"><ErrorState error={nlError} compact /></div>}
        {nlResult && (
          <div className="mt-1.5 rounded border bg-panel px-2 py-1 text-2xs leading-relaxed"
               data-draft-nl-result={nlResult.revision == null ? 'applied' : `v${nlResult.revision}`}>
            <p className="text-dim" data-draft-nl-result-version={nlResult.revision ?? ''}>
              {N.applied(nlResult.revision)}
              {nlResult.replayed && <span className="text-faint"> · {N.replayed}</span>}
            </p>
            {nlResult.invalidated && <p className="text-faint">{N.invalidated}</p>}
            {nlResult.mismatch === true && (
              <p className="text-[var(--warn)]" data-draft-nl-mismatch="">{N.mismatch}</p>
            )}
          </div>
        )}
      </div>
    )
  }

  /**
   * 验证与审阅：总情况（服务端算出的结论）、验证记录（独立期望、实际值、覆盖判断、审阅决定）、字段基线。
   * 分开写清楚：试运行成功、同步、样例对得上都不是验证通过；没有独立期望的照实说只记了试跑结果。
   */
  const renderValidation = (draft: CaliberDraft) => {
    if (!validations && !validationsError) return null
    const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
    const names = new Map(metrics.map((metric) => [metric.id as string, metric.name]))
    const summary = validations?.summary
    const records = validations?.records ?? []
    const baseline = validations?.field_baseline
    // 结论按哪一条记录算：只认服务端点名的那一条（summary.record_id），说清是哪条，别让人把别的记录当成它的结论
    const basisRecord = summary?.record_id ? records.find((record) => record.id === summary.record_id) ?? null : null
    const basisReview = basisRecord ? (reviewOut[basisRecord.id] ?? basisRecord.review) : null
    /**
     * 确认范围够不够：审阅的 scope 要盖住这条记录的**全部** metric_ids，服务端自己列出的
     * 「还没被确认的指标」也要是空的。只确认了一部分（或者范围读不到）就不能写成整卡通过。
     */
    const computedMissed = basisRecord ? uncoveredMetrics(basisRecord, basisReview) : []
    const serverMissed = summary?.unconfirmed
    // 名字优先用服务端列出来的那一份（它就是按确认范围算的），没有就按审阅的 scope 现算
    const missed = serverMissed?.length ? serverMissed : computedMissed
    const confirmedNames = (summary?.confirmed?.length ? summary.confirmed : basisReview?.scope ?? [])
      .filter((id) => (basisRecord?.metric_ids ?? []).includes(id))
    const scopeIncomplete = basisRecord !== null && basisReview?.decision === 'confirmed' && missed.length > 0
    // 服务端说 verified 时再核一遍：它自己列了未确认的指标、或者范围没盖住，就收回成「还不能算验证通过」。
    // 这里只会往保守的方向收回，不会把服务端没说通过的说成通过
    const partial = summary?.verdict === 'verified'
      && (scopeIncomplete || (basisRecord !== null && !scopeCovers(basisRecord, basisReview)))
    const nameOf = (id: string) => names.get(id) ?? id
    const verdict = partial ? 'unverified' : summary?.verdict ?? 'none'
    const tone = verdict === 'verified' ? 'var(--ok)'
      : verdict === 'mismatched' || verdict === 'failed' || verdict === 'rejected' ? 'var(--err)'
        : verdict === 'none' || verdict === 'stale' ? 'var(--text-faint)' : 'var(--warn)'
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-validation={verdict}
           data-validation-partial={partial ? 'recalled' : scopeIncomplete ? 'scope' : 'no'}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{V.title}</h3>
          <span className="text-2xs" data-draft-validation-verdict={verdict} style={{ color: tone }}>
            {V.verdict[verdict] ?? verdict}
          </span>
          {summary?.method?.version != null && <span className="text-[10px] text-faint">{V.method(summary.method.version)}</span>}
        </div>
        {summary && <p className="mt-1 text-2xs leading-relaxed text-dim" data-draft-validation-reason="">{summary.reason}</p>}
        {(partial || scopeIncomplete) && (
          <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]" data-draft-validation-partial="">
            {V.partialVerdict(confirmedNames.map(nameOf).join('、'), missed.map(nameOf).join('、') || '—',
              basisRecord?.metric_ids?.length ?? 0)}
          </p>
        )}
        {summary && (
          <p className="mt-0.5 text-2xs text-faint" data-draft-validation-basis="">
            {basisRecord
              ? V.basis(basisRecord.created_at ? formatDateTime(basisRecord.created_at) : '—',
                V.status[basisRecord.status] ?? basisRecord.status)
              : V.basisNone}
          </p>
        )}
        {summary && (
          <p className="mt-0.5 tnum text-2xs text-faint" data-draft-validation-counts="">
            {V.counts(summary.counts?.total ?? 0, summary.counts?.covering ?? 0)}
          </p>
        )}
        {!!summary?.semantic_gaps?.length && (
          <p className="mt-0.5 text-2xs text-faint" data-draft-validation-gaps="">
            {V.semanticGaps(summary.semantic_gaps.length)}
          </p>
        )}
        {baseline && (
          <p className="mt-0.5 text-2xs leading-relaxed text-faint" data-draft-baseline={baseline.status}>
            {V.baseline}：{baseline.status === 'known'
              ? V.baselineKnown(baseline.accepted_revision ?? null, (baseline.metric_ids ?? []).length,
                baseline.field_count ?? 0)
              : V.baselineUnknown}
            {baseline.status === 'known' ? `。${V.baselineHint}` : ''}
          </p>
        )}
        {validationsError != null && <div className="mt-1"><ErrorState error={validationsError} compact /></div>}
        {reviewError != null && <div className="mt-1"><ErrorState error={reviewError} compact /></div>}

        {records.length === 0 ? (
          <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-validations-empty="">
            {V.empty}：{V.emptyBody}
          </p>
        ) : (
          <>
            <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2">
              <h4 className="text-2xs font-semibold text-faint">{V.records}</h4>
              <span className="text-[10px] text-faint">{V.fieldsHint}</span>
            </div>
            <ul className="mt-1 space-y-1.5">
              {records.slice(0, recordsShown).map((record) => {
                const review = reviewOut[record.id] ?? record.review
                const expected = record.expected
                const ids = record.metric_ids ?? []
                const covering = record.covers_current
                return (
                  <li key={record.id} className="rounded border bg-panel px-2 py-1.5"
                      data-draft-validation-record={record.id} data-record-status={record.status}>
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
                      <span className="font-medium text-dim" data-record-status-label="">
                        {V.status[record.status] ?? record.status}
                      </span>
                      <span className={clsx(covering === true ? 'text-dim' : 'text-faint')}
                            data-record-covers={covering === true ? 'yes' : covering === false ? 'no' : 'unknown'}>
                        {coversText(covering)}
                      </span>
                      {record.created_at && <span className="text-faint">{formatDateTime(record.created_at)}</span>}
                      {record.run_id && (
                        <Link to={`/runs/${record.run_id}`} className="text-accent hover:underline" data-record-run="">
                          {V.openRun}
                        </Link>
                      )}
                    </div>

                    <div className="mt-1 text-2xs leading-relaxed" data-record-expect={expected ? 'given' : 'none'}>
                      <span className="text-faint">{V.expected}</span>
                      {expected ? (
                        <>
                          <span className="text-faint"> · {V.expectedSource}：{V.sources[expected.source] ?? expected.source}</span>
                          <ul className="mt-0.5 space-y-0.5">
                            {ids.map((metricId) => (
                              <li key={metricId} className="flex flex-wrap items-baseline gap-x-2"
                                  data-record-expect-value={metricId}>
                                <span className="text-dim [overflow-wrap:anywhere]">{names.get(metricId) ?? metricId}</span>
                                <span className="tnum">{valueText(expected.metrics?.[metricId])}</span>
                              </li>
                            ))}
                          </ul>
                        </>
                      ) : (
                        <span className="text-[var(--warn)]" data-record-no-expect="">：{V.expectedNone}</span>
                      )}
                    </div>

                    <div className="mt-1 text-2xs leading-relaxed">
                      <span className="text-faint">{V.actual}</span>
                      <ul className="mt-0.5 space-y-0.5">
                        {ids.map((metricId) => {
                          const actual = record.actual?.[metricId] ?? {}
                          const shown = actual.masked ? V.masked : (actual.rendered || valueText(actual.value) || V.noValue)
                          return (
                            <li key={metricId} className="flex flex-wrap items-baseline gap-x-2"
                                data-record-actual={metricId} data-record-actual-ok={String(actual.ok ?? 'unknown')}>
                              <span className="text-dim [overflow-wrap:anywhere]">{names.get(metricId) ?? metricId}</span>
                              <span className="tnum">{shown}</span>
                              {actual.rows != null && <span className="text-faint">{V.rowsHint(actual.rows)}</span>}
                              <span style={{ color: actual.ok === true ? 'var(--ok)'
                                : actual.ok === false ? 'var(--err)' : 'var(--text-faint)' }}>
                                {actual.ok === true ? V.ok : actual.ok === false ? V.notOk : V.unknown}
                              </span>
                            </li>
                          )
                        })}
                      </ul>
                    </div>

                    {!!record.problems?.length && (
                      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-2xs leading-relaxed text-[var(--warn)]"
                          data-record-problems="">
                        {record.problems.map((problem, i) => <li key={`${i}:${problem}`}>{problem}</li>)}
                      </ul>
                    )}

                    <div className="mt-1 border-t pt-1" data-record-review={review?.decision ?? 'none'}>
                      {review ? (
                        <p className="text-2xs leading-relaxed text-dim" data-record-review-line="">
                          {review.decision === 'rejected' ? V.reviewRejected : V.reviewConfirmed}
                          <span className="text-faint"> · {V.reviewBy(review.actor || '—', !!review.verified)}</span>
                          {review.created_at ? <span className="text-faint"> · {formatDateTime(review.created_at)}</span> : ''}
                          {!!review.scope?.length && (
                            <span className="text-faint">
                              {' · '}{V.reviewScope}：{review.scope.map((id) => names.get(id) ?? id).join('、')}
                            </span>
                          )}
                          {review.decision === 'confirmed' && !scopeCovers(record, review) && (
                            <span className="text-[var(--warn)]" data-record-review-partial="">
                              {' · '}{V.reviewPartial}
                              {uncoveredMetrics(record, review).length > 0
                                ? `（没盖住${uncoveredMetrics(record, review).map((id) => names.get(id) ?? id).join('、')}）`
                                : ''}
                            </span>
                          )}
                          {review.covers_current === true ? (
                            <span className="text-faint" data-record-review-covers="yes"> · {V.reviewCovers}</span>
                          ) : review.covers_current === false ? (
                            <span className="text-faint" data-record-review-covers="no"> · {V.reviewNotCovering}</span>
                          ) : (
                            <span className="text-faint" data-record-review-covers="unknown">
                              {' · '}{V.reviewCoversUnknown}
                            </span>
                          )}
                          {review.note && <span className="text-faint"> · {review.note}</span>}
                        </p>
                      ) : <p className="text-2xs text-faint">{V.noReview}</p>}
                      {canReview && (
                        <div className="mt-1 flex flex-wrap items-center gap-2">
                          <input className="field w-44 text-2xs" data-draft-review-note={record.id}
                                 aria-label={V.reviewNote} placeholder={V.reviewNotePlaceholder}
                                 value={reviewNotes[record.id] ?? ''} disabled={reviewBusy === record.id}
                                 onChange={(e) => setReviewNotes((cur) => ({ ...cur, [record.id]: e.target.value }))} />
                          <button type="button" className="btn btn-xs" data-draft-review="confirmed"
                                  disabled={reviewBusy === record.id}
                                  onClick={() => void reviewRecord(draft, record.id, 'confirmed')}>
                            {reviewBusy === record.id ? <Spinner size={11} /> : <Check size={11} aria-hidden />} {V.confirm}
                          </button>
                          <button type="button" className="btn btn-xs btn-ghost" data-draft-review="rejected"
                                  disabled={reviewBusy === record.id}
                                  onClick={() => void reviewRecord(draft, record.id, 'rejected')}>
                            {V.reject}
                          </button>
                          <span className="text-[10px] text-faint">{V.reviewScopeHint}</span>
                        </div>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
            {records.length > recordsShown && (
              <button type="button" className="btn btn-xs btn-ghost mt-1" data-draft-validations-more=""
                      onClick={() => setRecordsShown((cur) => cur + RECORDS_SHOWN)}>
                {V.more(records.length - recordsShown)}
              </button>
            )}
          </>
        )}

        {renderExpectForm(draft)}
      </div>
    )
  }

  /**
   * 带独立期望试运行：指标要逐条给全（服务端按全指标比，少一条会拒），来源只能是人确认、固定夹具或独立计算。
   * 期望必须独立于这次执行——页面把这句话写在旁边，也不替人从这次结果里填一个。
   */
  const renderExpectForm = (draft: CaliberDraft) => {
    const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
    if (!metrics.length) return null
    const fields = draft.entry_fields ?? []
    const filled = metrics.filter((metric) => (expectValues[metric.id as string] ?? '').trim())
    const ready = filled.length === metrics.length
    const wanted: Record<string, unknown> = {}
    for (const metric of metrics) wanted[metric.id as string] = expectValue(expectValues[metric.id as string] ?? '')
    return (
      <details className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-expect="">
        <summary className="cursor-pointer text-2xs font-semibold text-dim">{V.trialWithExpect}</summary>
        <p className="mt-0.5 text-2xs leading-relaxed text-faint">
          {V.trialWithExpectHint}。{V.expectedHint}
        </p>
        <ul className="mt-1 space-y-0.5">
          {metrics.map((metric) => (
            <li key={metric.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs">
              <span className="text-dim [overflow-wrap:anywhere]">{V.expectFor(metric.name)}</span>
              <input className="field w-32 text-2xs" data-draft-expect-input={metric.id}
                     aria-label={V.expectFor(metric.name)} disabled={trialStarting}
                     value={expectValues[metric.id as string] ?? ''}
                     onChange={(e) => setExpectValues((cur) => ({ ...cur, [metric.id as string]: e.target.value }))} />
            </li>
          ))}
        </ul>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-2xs">
          <label className="flex items-center gap-1 text-faint">
            {V.expectSourceLabel}
            <select className="field h-[26px] text-2xs" value={expectSource} data-draft-expect-source=""
                    disabled={trialStarting}
                    onChange={(e) => setExpectSource(e.target.value as DraftExpectationSource)}>
              {(Object.keys(V.sources) as DraftExpectationSource[]).map((key) => (
                <option key={key} value={key}>{V.sources[key]}</option>
              ))}
            </select>
          </label>
          <button type="button" className="btn btn-xs" data-draft-expect-submit=""
                  disabled={trialStarting || !ready || !entryReady(fields, entryValues)}
                  onClick={() => void runTrial(draft, { metrics: wanted, source: expectSource })}>
            {trialStarting ? <Spinner size={11} /> : <Play size={11} aria-hidden />}
            {trialStarting ? V.expectSubmitting : V.expectSubmit}
          </button>
          {!ready && <span className="text-faint" data-draft-expect-needall="">{V.expectNeedAll}</span>}
        </div>
      </details>
    )
  }

  /** 自动试运行：状态、每个指标的值（名字和值成对）、失败原因；没有试运行就整块不画 */
  const renderTrial = (draft: CaliberDraft) => {
    const status = draft.trial?.status === 'stale' ? 'stale' : trial?.status ?? draft.trial?.status
    if (!status) return null
    const rows = trial?.metrics ?? []
    // 质量结论按联合形状读：只有服务端给了认得的 status 才算一份，读不到就是「没有质量结论」
    const quality = qualityReportOf(trial?.validation)
    // 这次试跑对应的验证记录：只有这一条画在试跑块里，别处的旧记录不冒充它
    const brief = trial?.validation
    const own = brief?.record ?? null
    const color = status === 'succeeded' ? (quality?.status === 'pass' ? 'var(--ok)' : 'var(--warn)') : status === 'failed' ? 'var(--err)'
      : status === 'needs_input' || status === 'stale' ? 'var(--warn)' : 'var(--accent)'
    const label = status === 'succeeded' ? D.trialSucceeded : status === 'failed' ? D.trialFailed
      : status === 'needs_input' ? D.trialNeedsInput : status === 'stale' ? D.trialStale : D.trialRunning
    const unitOf = (id: string) => (draft.metrics ?? []).find((m) => m.id === id)?.unit
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-trial={status}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{D.trial}</h3>
          <span className="text-2xs" style={{ color }}>{label}</span>
          {trial?.run_id && (
            <Link to={`/runs/${trial.run_id}`} className="text-2xs text-accent hover:underline" data-draft-trial-run="">
              {D.trialRun}
            </Link>
          )}
          <span className="text-[10px] text-faint">{D.trialHint}</span>
        </div>
        {status === 'running' && (
          <p className="mt-1 flex items-center gap-1.5 text-2xs text-dim">
            <Spinner size={11} /> {D.trialRunning}
          </p>
        )}
        {status === 'running' && trialTimedOut && <div className="mt-1"><Notice tone="warn">{D.trialTimeout}</Notice></div>}
        {status === 'failed' && (
          <div className="mt-1">
            <Notice tone="err">{D.trialFailed}：{trial?.error || draft.trial?.error || D.loadFailed}</Notice>
          </div>
        )}
        {status === 'needs_input' && (
          <p className="mt-1 text-2xs text-dim" data-draft-trial-needs-input="">
            {D.trialNeedsInputHint}：{trial?.error || draft.trial?.error || ''}
          </p>
        )}
        {status === 'stale' && <p className="mt-1 text-2xs text-dim">{D.trialStaleHint}</p>}
        {status === 'succeeded' && (
          <>
            {rows.length > 0 ? (
              <ul className="mt-1 grid gap-x-4 gap-y-0.5 text-2xs sm:grid-cols-[1fr_auto]">
                {rows.map((row) => {
                  const unit = unitOf(row.id)
                  return (
                    <li key={row.id} className="contents" data-draft-trial-metric={row.id}>
                      <span className="text-dim [overflow-wrap:anywhere]">{row.name || row.id}</span>
                      <span className="tnum text-right font-medium">
                        {row.rendered ?? (row.value == null ? '—' : String(row.value))}{unit ? ` ${unit}` : ''}
                      </span>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="mt-1 text-2xs text-faint">{D.trialNoMetrics}</p>
            )}
            <TrialValidation report={quality} />
            <TrialResult queries={trial?.queries} />
          </>
        )}
        {trialError != null && <div className="mt-1"><Notice tone="warn">{D.trialUnavailable}</Notice></div>}

        {/*
          这次试跑的验证记录（K05）：只说本次那一条。还在跑、又还没有本次的记录时写「正在等」，
          不把列表里的旧记录画到这里当成本次的结论；covers_current 只有 === true 才算覆盖。
        */}
        {own ? (
          <p className="mt-1.5 text-2xs leading-relaxed text-dim" data-draft-trial-record={own.status}
             data-trial-record-covers={own.covers_current === true ? 'yes' : own.covers_current === false ? 'no' : 'unknown'}>
            <span className="text-faint">{V.trialRecord}：</span>
            {V.status[own.status] ?? own.status}
            <span className="text-faint"> · {coversText(own.covers_current)}</span>
            <span className="text-faint">
              {' · '}{own.expected
                ? `${V.expected}：${V.sources[own.expected.source] ?? own.expected.source}`
                : V.noExpectation}
            </span>
          </p>
        ) : status === 'running' ? (
          <p className="mt-1.5 flex items-center gap-1.5 text-2xs text-dim" data-draft-trial-waiting="">
            <Spinner size={11} /> {V.trialWaiting}
          </p>
        ) : (
          <p className="mt-1.5 text-2xs leading-relaxed text-faint" data-draft-trial-no-record="">
            {brief?.note || V.trialNoRecord}
          </p>
        )}
      </div>
    )
  }

  /**
   * 取数 SQL：草稿图里写好的模板，入口字段在运行时渲染；只读展示，不出现执行值。
   * 默认折叠，SQL 可能为空（旧草稿、没写出取数），那时整块不画。
   */
  const renderSql = (draft: CaliberDraft) => {
    const groups = (draft.groups ?? []).filter((group) => (group.sql ?? '').trim())
    const sql = (draft.sql ?? '').trim()
    if (!groups.length && !sql) return null
    const names = new Map((draft.metrics ?? []).filter((metric) => metric.id)
      .map((metric) => [metric.id as string, metric.name]))
    return (
      <details className="rounded-md border bg-elev px-3 py-2" data-draft-sql="">
        <summary className="cursor-pointer text-2xs font-semibold text-dim">{D.sqlTitle}</summary>
        <p className="mt-0.5 text-2xs leading-relaxed text-faint">{D.sqlHint}</p>
        {groups.length > 0 ? (
          <div className="mt-1 space-y-2">
            {groups.map((group, index) => (
              <div key={`${index}:${group.sql}`} className="rounded bg-panel px-2 py-1.5"
                   data-draft-sql-group={index}>
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
                  <span className="font-medium text-dim">{D.sqlGroup(index + 1, group.metric_ids?.length ?? 0)}</span>
                  {!!group.metric_ids?.length && (
                    <span className="text-faint [overflow-wrap:anywhere]">
                      {(group.metric_ids ?? []).map((id) => names.get(id) ?? id).join('、')}
                    </span>
                  )}
                </div>
                <div className="mt-1 overflow-x-auto">
                  <code className="mono block whitespace-pre-wrap text-2xs leading-relaxed text-dim">{group.sql}</code>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-1 overflow-x-auto rounded bg-panel px-2 py-1.5">
            <code className="mono block whitespace-pre-wrap text-2xs leading-relaxed text-dim">{sql}</code>
          </div>
        )}
      </details>
    )
  }

  /** 发布前检查：服务端从图和试跑状态算，只读；未通过的不拦人，逐项写原因 */
  const renderChecks = (draft: CaliberDraft) => {
    const checks = draft.checks ?? []
    if (!checks.length) return null
    const passed = checks.filter((check) => check.ok).length
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-checks="">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{D.checks}</h3>
          <span className={clsx('tnum text-2xs', passed === checks.length ? 'text-[var(--ok)]' : 'text-[var(--warn)]')}>
            {D.checksSummary(passed, checks.length)}
          </span>
        </div>
        <ul className="mt-1 space-y-1">
          {checks.map((check) => (
            <li key={check.key} className="flex items-start gap-1.5 text-2xs leading-relaxed" data-draft-check={check.key}>
              {check.ok ? <Check size={11} className="mt-px shrink-0 text-[var(--ok)]" aria-hidden />
                : <AlertTriangle size={11} className="mt-px shrink-0 text-[var(--warn)]" aria-hidden />}
              <span className="min-w-0 flex-1">
                <span className={check.ok ? 'text-dim' : 'text-[var(--warn)]'}>{check.label}</span>
                {!check.ok && <span className="text-faint">（{D.checkFail}）</span>}
                {check.detail && <p className="mt-0.5 text-2xs text-faint [overflow-wrap:anywhere]">{check.detail}</p>}
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-1 text-2xs text-faint">{D.checksHint}</p>
      </div>
    )
  }

  /** 入口字段：发布后按这些字段收参数 */
  const renderEntryFields = (draft: CaliberDraft) => {
    const fields = draft.entry_fields
    const notes = draft.entry_notes ?? []
    if (!fields) return null
    const canTrial = !!draft.workflow_id
    return (
      <div data-draft-entry-fields="">
        <h3 className="mb-1 text-2xs font-semibold text-dim">{D.entryFields}</h3>
        <p className="mb-1 text-2xs text-faint">{D.entryFieldsHint}</p>
        {fields.length > 0 ? (
          <ul className="space-y-0.5 text-2xs leading-relaxed">
            {fields.map((field) => {
              const hasDefault = field.default !== undefined && field.default !== null && field.default !== ''
              return (
                <li key={field.name} className="flex flex-wrap items-center gap-x-2 gap-y-1"
                    data-draft-entry-field={field.name}>
                  <code className="font-medium text-dim">{field.name}</code>
                  <span className="text-faint">{field.type || 'string'}</span>
                  <span className="text-faint">{field.required ? D.required : D.optional}</span>
                  {hasDefault && <span className="text-faint">{D.defaultValue(defaultText(field.default))}</span>}
                  {field.description && <span className="text-dim [overflow-wrap:anywhere]">{field.description}</span>}
                  {canTrial && (field.type === 'boolean' ? (
                    <input type="checkbox" className="accent-[var(--accent)]" disabled={trialStarting}
                           data-draft-entry-input={field.name}
                           checked={(entryValues[field.name] ?? '') === 'true'}
                           onChange={(e) => setEntryValues((cur) => ({
                             ...cur, [field.name]: e.target.checked ? 'true' : 'false',
                           }))} />
                  ) : (
                    <input className="field w-36 text-2xs" value={entryValues[field.name] ?? ''}
                           disabled={trialStarting} data-draft-entry-input={field.name}
                           placeholder={prefilled[field.name] ? D.entryPrefilled : ''}
                           onChange={(e) => setEntryValues((cur) => ({ ...cur, [field.name]: e.target.value }))} />
                  ))}
                </li>
              )
            })}
          </ul>
        ) : <p className="text-2xs text-faint">{D.entryFieldNone}</p>}
        {notes.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-2xs leading-relaxed text-warn" data-draft-entry-notes="">
            {notes.map((note, i) => <li key={`${i}:${note}`}>{note}</li>)}
          </ul>
        )}
        {canTrial && (
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-xs btn-primary"
                    disabled={trialStarting || !entryReady(fields, entryValues)}
                    onClick={() => void runTrial(draft)} data-draft-trial-run-button="">
              {trialStarting ? <Spinner size={11} /> : <Play size={11} aria-hidden />}
              {trialStarting ? D.trialRunStarting : D.trialRunAction}
            </button>
            {Object.keys(prefilled).length > 0 && (
              <span className="text-[10px] text-faint">{D.entryPrefilled}</span>
            )}
          </div>
        )}
      </div>
    )
  }

  /**
   * 一条指标：默认收成一行，名字、单位、值和未决问题数直接看得见；展开才是定义、表达式、引用。
   * 指标多到几十条时这样一屏能扫完，找哪条再展开哪条。
   */
  const renderMetric = (metric: DraftMetric, index: number, values: Map<string, DraftTrialMetric>) => {
    const definition = metric.definition ?? {}
    const citations = definition.citations ?? []
    const questions = metric.open_questions ?? []
    const row = metric.id ? values.get(metric.id) : undefined
    const value = row ? row.rendered ?? (row.value == null ? '' : String(row.value)) : ''
    const rows = [
      [D.unit, metric.unit],
      [D.decimals, metric.decimals != null ? String(metric.decimals) : ''],
      [D.grain, metric.grain],
      [D.timeField, metric.time_field],
      [D.numerator, metric.numerator],
      [D.denominator, metric.denominator],
      [D.scope, definition.scope],
      [D.owner, definition.owner],
      [D.effectiveFrom, definition.effective_from],
      [D.dimensions, metric.dimensions?.join('、')],
      [D.filters, metric.filters?.join('；')],
      [D.exclusions, definition.exclusions?.join('；')],
    ].filter(([, text]) => typeof text === 'string' && text.trim()) as [string, string][]
    // 业务写法（统计时间口径、退款处理）摆在这条指标自己身上：说是哪一条指标按什么口径算的，不看字段路径
    const business = (detail?.binding?.editable_fields ?? [])
      .map((field): [string, string] => {
        const raw = (metric as unknown as Record<string, unknown>)[field.field]
        return [field.label, typeof raw === 'string' ? choiceLabel(field, raw) : '']
      })
      .filter(([, text]) => !!text)
    return (
      <li key={`${metric.id ?? metric.name}:${index}`} data-draft-metric={metric.name}>
        <details className="rounded-md border bg-panel">
          <summary className="flex cursor-pointer flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 py-2"
                   title={D.metricSummaryHint}>
            <span className="text-xs font-medium [overflow-wrap:anywhere]">{metric.name}</span>
            {!!questions.length && <span className="text-2xs text-[var(--warn)]">{D.metricQuestions(questions.length)}</span>}
            {!!value && (
              <span className="tnum ml-auto text-xs font-medium" data-draft-metric-value={metric.id}>
                {value}{metric.unit ? ` ${metric.unit}` : ''}
              </span>
            )}
          </summary>
          <div className="border-t px-3 py-2">
            {!!metric.synonyms?.length && <p className="text-2xs text-faint">{D.synonyms(metric.synonyms.join('、'))}</p>}
            {business.length > 0 && (
              <p className="mb-1 text-2xs text-dim" data-draft-metric-business={metric.id}>
                {business.map(([label, text]) => `${label}${D.businessValue(text)}`).join(' · ')}
              </p>
            )}
            {(rows.length > 0 || business.length > 0) && (
              <dl className="mt-1 grid gap-x-3 gap-y-0.5 text-2xs sm:grid-cols-[auto_1fr]">
                {rows.map(([label, text]) => (
                  <div key={label} className="contents">
                    <dt className="text-faint">{label}</dt>
                    <dd className="text-dim">{text}</dd>
                  </div>
                ))}
              </dl>
            )}
            {metric.expression && (
              <div className="mt-1.5" data-draft-expression="">
                <h4 className="text-2xs font-semibold text-faint">{D.expression}</h4>
                <code className="mt-0.5 block whitespace-pre-wrap rounded bg-elev px-1.5 py-1 text-2xs text-dim [overflow-wrap:anywhere]">
                  {metric.expression}
                </code>
              </div>
            )}
            {definition.text && (
              <div className="mt-1.5">
                <h4 className="text-2xs font-semibold text-faint">{D.definition}</h4>
                <p className="text-2xs leading-relaxed text-dim">{definition.text}</p>
              </div>
            )}
            {citations.length > 0 && (
              <div className="mt-1.5">
                <h4 className="mb-1 text-2xs font-semibold text-faint">{D.citations}</h4>
                <ul className="space-y-1">{citations.map((c, i) => citationRow(c, `${index}:${i}`, metric.name))}</ul>
              </div>
            )}
            {!!questions.length && (
              <div className="mt-1.5" data-draft-questions="">
                <h4 className="text-2xs font-semibold text-faint">{D.questions}</h4>
                {questionList(questions, `${index}`)}
              </div>
            )}
          </div>
        </details>
      </li>
    )
  }

  const renderDetail = (draft: CaliberDraft) => {
    const stage = stageOf(draft)
    const drafting = draft.status === 'drafting'
    const terminal = stage === 'discarded'
    const stageNote = stage === 'review' ? D.stageNote(draft.status) : ''
    const questions = draft.open_questions ?? []
    const dropped = draft.dropped ?? []
    const uncovered = draft.uncovered ?? []
    const metrics = draft.metrics ?? []
    const values = new Map((draft.trial?.status === 'stale' ? [] : trial?.metrics ?? []).map((row) => [row.id, row]))
    // 第一轮还没落库时服务端写的是空对象：没有轮次就不画，别显示「第 undefined 轮」
    const progress = draft.progress && typeof draft.progress.round === 'number' ? draft.progress : null
    return (
      <div className="space-y-3 border-t px-3 py-3" data-draft-detail="">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint">
          <StatusPill stage={stage} status={draft.status} />
          {stageNote && <span>{stageNote}</span>}
          {!!draft.documents?.length && <span>{D.documentsUsed(draft.documents.length)}</span>}
          {typeof draft.blocks === 'number' && draft.blocks > 1 && <span>{D.blocks(draft.blocks)}</span>}
          {draft.created_at && <span title={formatDateTime(draft.created_at)}>{formatTime(draft.created_at)}</span>}
          {draft.created_by && <span>{D.createdBy(draft.created_by)}</span>}
          {draft.model_id && <span title={draft.model_id}>{D.model(draft.model_id)}</span>}
          {draft.workflow_id && stage !== 'discarded' && (
            <Link to={`/studio/${draft.workflow_id}`} className="text-accent hover:underline" data-draft-workflow="">
              {D.openWorkflow}{draft.workflow_name ? `：${draft.workflow_name}` : ''}
              {draft.version != null ? ` · ${D.workflowVersion(draft.version)}` : ''}
            </Link>
          )}
        </div>

        {(drafting || draft.job?.retryable || (draft.status === 'failed' && !draft.workflow_id)) && (
          <div className="flex gap-2">
            {drafting && <button className="btn btn-sm" disabled={jobBusy === draft.id} onClick={() => void changeDraftJob(draft, 'stop')}>停止起草</button>}
            {!drafting && <button className="btn btn-sm" disabled={jobBusy === draft.id} onClick={() => void changeDraftJob(draft, 'retry')}>重试起草</button>}
          </div>
        )}
        {jobError != null && <ErrorState error={jobError} compact />}
        {draft.error && (
          <Notice tone="err">
            {drafting ? D.draftingFailed : D.loadFailed}：{draft.error}
          </Notice>
        )}

        {drafting && pollingId === draft.id && (
          <Notice tone="info">
            <span className="inline-flex items-center gap-1.5"><Spinner size={12} /> {D.drafting}</span>
            {progress && (
              <span className="tnum mt-0.5 block" data-draft-progress="">
                {D.progress(progress.round, progress.covered, progress.uncovered)}
              </span>
            )}
            <span className="mt-0.5 block text-faint">{D.draftingHint}</span>
          </Notice>
        )}
        {drafting && pollTimedOut && (
          <Notice tone="warn">
            {D.draftingTimeout}
            {progress && (
              <span className="tnum mt-0.5 block" data-draft-progress="">
                {D.progress(progress.round, progress.covered, progress.uncovered)}
              </span>
            )}
          </Notice>
        )}
        {(draft.doc_truncated || (draft.truncated && !draft.catalog_truncated)) && (
          <Notice tone="warn">{D.docTruncated}</Notice>
        )}
        {draft.catalog_truncated && <Notice tone="warn">{D.catalogTruncated}</Notice>}

        {renderTrial(draft)}
        {renderRules(draft)}
        {renderValidation(draft)}
        {renderImports(draft)}
        {renderNl(draft)}
        {renderEntryFields(draft)}
        {renderSql(draft)}

        <div>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-2xs font-semibold text-dim">{D.metricsCount(metrics.length)}</h3>
            {pickedQuestions.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-2xs text-faint">{D.resolveHint}</span>
                <button type="button" className="btn btn-xs" disabled={busy === `resolve:${draft.id}`}
                        onClick={() => void resolve(draft)} data-draft-resolve="">
                  {busy === `resolve:${draft.id}` ? <Spinner size={11} /> : null} {D.resolve}
                </button>
              </div>
            )}
          </div>
          {metrics.length ? (
            <ul className="space-y-2">{metrics.map((metric, index) => renderMetric(metric, index, values))}</ul>
          ) : !drafting ? (
            <EmptyState offline={false} icon={<ScrollText size={20} />} title={D.noMetrics} body={D.noMetricsBody} />
          ) : (
            <Skeleton rows={3} height={16} />
          )}
        </div>

        {questions.length > 0 && (
          <div data-draft-questions-all="">
            <h3 className="mb-1 text-2xs font-semibold text-dim">{D.questionsOther}</h3>
            {questionList(questions, 'top')}
          </div>
        )}

        {dropped.length > 0 && (
          <div data-draft-dropped="">
            <h3 className="text-2xs font-semibold text-dim">{D.dropped}</h3>
            <p className="mb-1 text-2xs leading-relaxed text-faint">{D.droppedHint}</p>
            <ul className="space-y-0.5 text-2xs leading-relaxed">
              {dropped.map((item, i) => (
                <li key={`${item.name}:${i}`} className="text-dim">
                  <span className="font-medium">{item.name}</span>
                  <span className="text-faint">：{item.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {uncovered.length > 0 && (
          <div data-draft-uncovered="">
            <h3 className="text-2xs font-semibold text-dim">{D.uncovered(uncovered.length)}</h3>
            <p className="mb-1 text-2xs leading-relaxed text-faint">{D.uncoveredHint}</p>
            <p className="text-2xs leading-relaxed text-dim">{uncovered.join('、')}</p>
          </div>
        )}

        {draft.alignment && <AlignmentBlock alignment={draft.alignment} />}

        {renderChecks(draft)}

        {!drafting && stage === 'review' && draft.workflow_id && (
          <div className="flex flex-wrap items-center gap-2" data-draft-publish="">
            <Link to={`/studio/${draft.workflow_id}`} className="btn btn-primary btn-sm">
              <PenLine size={12} aria-hidden /> {D.publish}
            </Link>
            <span className="text-2xs text-faint">{D.publishHint}</span>
          </div>
        )}

        {!drafting && stage === 'review' && draft.status !== 'attached' && (
          <details className="rounded-md border bg-elev px-3 py-2" data-draft-attach="">
            <summary className="cursor-pointer text-2xs font-semibold text-dim">{D.attach}</summary>
            <p className="mt-0.5 text-2xs leading-relaxed text-faint">{D.attachHint}</p>
            <div className="mt-1.5 flex flex-wrap items-end gap-2">
              <label className="min-w-40 flex-1 text-2xs text-faint">
                <span className="mb-0.5 block">{D.runId}</span>
                <input className="input w-full" value={runId} placeholder={D.runIdPlaceholder}
                       onChange={(e) => setRunId(e.target.value)} aria-label={D.runId} data-draft-run-id="" />
              </label>
              <label className="w-56 text-2xs text-faint">
                <span className="mb-0.5 block">{D.recentRuns}</span>
                <select className="field h-[30px] w-full" value="" aria-label={D.recentRuns}
                        onChange={(e) => { if (e.target.value) setRunId(e.target.value) }}>
                  <option value="">
                    {recentRuns == null ? D.recentRunsPick : recentRuns.length ? D.recentRunsPick : D.recentRunsNone}
                  </option>
                  {(recentRuns ?? []).map((run) => (
                    <option key={run.id} value={run.id}>
                      {run.workflow_name || run.id}{run.created_at ? ` · ${formatTime(run.created_at)}` : ''}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className="btn btn-sm" disabled={busy === `attach:${draft.id}`}
                      onClick={() => void attach(draft)} data-draft-attach-submit="">
                {busy === `attach:${draft.id}` ? <Spinner size={12} /> : null} {busy === `attach:${draft.id}` ? D.attaching : D.attachAction}
              </button>
            </div>
          </details>
        )}

        {attachOut && (
          <div className="rounded-md border bg-elev px-3 py-2 text-2xs leading-relaxed text-dim" data-draft-attached="">
            <p>{D.attached(attachOut.version)}</p>
            <Link to={`/studio/${attachOut.workflow_id}`} className="text-accent hover:underline">
              {D.openWorkflow}
            </Link>
            {attachOut.warnings.length > 0 && (
              <div className="mt-1">
                <span className="text-faint">{D.attachWarnings}</span>
                <ul className="mt-0.5 list-disc pl-4">
                  {attachOut.warnings.map((w) => <li key={w}>{w}</li>)}
                </ul>
              </div>
            )}
          </div>
        )}

        {!terminal && (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-xs btn-ghost" disabled={!!busy}
                    onClick={() => void dismiss(draft)} data-draft-dismiss="">
              {busy === `dismiss:${draft.id}` ? <Spinner size={11} /> : null} {D.dismiss}
            </button>
          </div>
        )}
      </div>
    )
  }

  const pendingDocs = !docs ? <Skeleton rows={4} height={22} />
    : docs.length === 0 ? <p className="py-2 text-2xs leading-relaxed text-faint">{D.noDocuments}：{D.noDocumentsBody}</p>
      : (
        <ul className="max-h-40 space-y-0.5 overflow-y-auto pr-1">
          {docs.map((doc) => {
            const ready = doc.status !== 'processing' && doc.status !== 'failed'
            return (
              <li key={doc.id}>
                <label className={clsx('flex items-start gap-1.5 text-2xs leading-relaxed',
                  ready ? 'text-dim' : 'text-faint')}>
                  <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" disabled={!ready}
                         checked={pickedDocs.includes(doc.id)}
                         onChange={(e) => setPickedDocs((cur) => (e.target.checked
                           ? [...cur, doc.id] : cur.filter((id) => id !== doc.id)))} />
                  <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{doc.title || doc.id}</span>
                  <span className="shrink-0 text-faint">{D.documentMeta(doc.collection, doc.chunk_count)}</span>
                </label>
              </li>
            )
          })}
        </ul>
      )

  return (
    <div className="flex h-full flex-col" data-caliber-drafts="">
      <PageHeader icon={<FileText size={14} />} title={D.title} subtitle={D.subtitle}
                  actions={<>
                    <Link to="/ledger/calibers" className="btn btn-xs btn-ghost">
                      <ChevronLeft size={12} aria-hidden /> {D.back}
                    </Link>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={loadList} title={D.refresh}>
                      <RefreshCw size={12} aria-hidden /> {D.refresh}
                    </button>
                  </>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          <section className="rounded-lg border bg-panel px-3 py-2.5" data-draft-start="">
            <h2 className="text-xs font-semibold">{D.start}</h2>
            <div className="mt-2 grid gap-3 md:grid-cols-2">
              <div>
                <h3 className="text-2xs font-semibold text-dim">{D.documents}</h3>
                <p className="mb-1 text-2xs text-faint">{D.documentsHint}</p>
                {docsError ? <ErrorState error={docsError} compact /> : pendingDocs}
              </div>
              <div className="space-y-2">
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.datasource}</span>
                  <select className="field w-full" value={datasourceId} required
                          onChange={(e) => setDatasourceId(e.target.value)}>
                    {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                  <span className="mt-0.5 block">{D.datasourceHint}</span>
                </label>
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.guidance}</span>
                  <textarea className="field min-h-[64px] w-full text-xs" rows={3} value={guidance}
                            placeholder={D.guidancePlaceholder} onChange={(e) => setGuidance(e.target.value)} />
                </label>
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.fixedFilters}</span>
                  <textarea className="field min-h-[52px] w-full text-xs" rows={2} value={fixedFilters}
                            placeholder={D.fixedFiltersPlaceholder} data-draft-fixed-filters=""
                            onChange={(e) => setFixedFilters(e.target.value)} />
                  <span className="mt-0.5 block">{D.fixedFiltersHint}</span>
                </label>
                <button type="button" className="btn btn-primary btn-sm" disabled={submitting || !datasourceId}
                        onClick={() => void submit()} data-draft-submit="">
                  {submitting ? <Spinner size={12} /> : <FileText size={12} aria-hidden />} {submitting ? D.submitting : D.submit}
                </button>
              </div>
            </div>
          </section>

          <div className="flex flex-wrap items-center gap-1" role="group" aria-label={D.filterLabel}>
            {FILTERS.map((f) => (
              <button key={f || 'all'} type="button"
                      className={clsx('btn btn-xs', filter === f ? 'btn-primary' : 'btn-ghost')}
                      aria-pressed={filter === f} onClick={() => setFilter(f)} data-draft-filter={f || 'all'}>
                {f ? D.filter[f] : D.filter.all}
                {drafts && <span className="tnum ml-1 text-[10px] opacity-70">
                  {f ? drafts.filter((d) => stageOf(d) === f).length : drafts.length}
                </span>}
              </button>
            ))}
          </div>

          {listError ? <ErrorState error={listError} onRetry={loadList} compact />
            : !drafts ? <Skeleton rows={5} height={38} />
            : !shown.length ? (
              <EmptyState offline={false} icon={<FileText size={22} />} title={D.empty}
                          body={filter || drafts.length ? D.emptyFiltered : D.emptyBody} />
            ) : shown.map((draft) => {
              const expanded = openId === draft.id
              return (
                <section key={draft.id} className="overflow-hidden rounded-lg border bg-panel" data-draft={draft.id}>
                  <button type="button" className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover"
                          onClick={() => {
                            if (expanded) {
                              openSeq.current++
                              setOpenId(null)
                              setDetail(null)
                              setPollingId(null)
                              setTrial(null)
                              setTrialPollId(null)
                            } else {
                              void open(draft.id)
                            }
                          }}
                          aria-expanded={expanded} data-draft-toggle="">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="text-sm font-medium [overflow-wrap:anywhere]">{draft.title}</span>
                        <StatusPill stage={stageOf(draft)} status={draft.status} />
                        {draft.error && <span className="text-2xs text-[var(--err)]">{D.draftingFailed}</span>}
                      </div>
                      <div className="mt-0.5 text-2xs text-faint">
                        {D.summary(draft.metric_count, draft.question_count)}
                        {draft.created_at ? ` · ${formatTime(draft.created_at)}` : ''}
                      </div>
                    </div>
                  </button>
                  {expanded && (
                    detailLoading ? <div className="border-t px-3 py-3"><Skeleton rows={4} height={16} /></div>
                      : detailError ? <div className="border-t px-3 py-3"><ErrorState error={detailError} compact
                                                                    onRetry={() => void open(draft.id)} /></div>
                        : detail && detail.id === draft.id ? renderDetail(detail) : null
                  )}
                </section>
              )
            })}
        </div>
      </div>
    </div>
  )
}
