/**
 * 口径账本的接口约定（docs/design/next-round/02-口径账本.md；服务端 app/ledger/view.py、issue.py、reconcile.py、certify.py）。
 *
 * 界面层不放逻辑：每个数的标签、整题判断、能做的动作（能不能出具、能不能自动、不能的原因、要不要先核对参数）都由服务端
 * 给，界面照着画。比对在封存之后做（不在封存范围内），GET /api/runs/{id}/ledger 在比对还没出结果时答 state=pending，
 * 页面按退避再取（store/ledger.ts），不靠实时流：ledger.matched 在事件总线关闭之后才追加，打开着的页面收不到。
 */
import type { FormSchema } from './typesApp'

/** 绑定的一个参数：值是这次运行查到过的遮罩值时不给值（masked），也不能拿它代填出具 */
export interface LedgerBinding {
  field: string
  value?: unknown
  masked?: boolean
}

/** 差在哪的一条（explain_sql_diff 的条目，SQL 片段已按遮罩换过）。text 是一句中文说明 */
export interface LedgerDiffItem {
  aspect?: string
  a?: unknown
  b?: unknown
  text?: string
}

export interface LedgerDiff {
  items: LedgerDiffItem[]
  /** 兜底说明：两条 SQL 对不上结构，没能归纳成具体差异 */
  fallback: boolean
  text_diff: string[]
}

/** 一份相关口径（同一个数据源、查了同样的表，但不等价） */
export interface LedgerRelated {
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  node_id?: string
  comparable?: boolean
  reason?: string
  via?: string
  binding?: LedgerBinding[]
  diff?: LedgerDiff | null
}

/**
 * 一个数的标签（四种）：
 * - caliber：来自口径「X」vN（how：independent 等价已证明 / composed 助手选用）
 * - related：相关口径（附差在哪），数本身是这次的推理
 * - reasoning：这次的推理，没有相关的已发布口径
 * - unknown：没比对（reason 写为什么）
 */
export interface LedgerLabel {
  kind: 'caliber' | 'external' | 'derived' | 'assessed' | 'related' | 'reasoning' | 'unknown'
  how?: 'independent' | 'composed'
  source?: string
  as_of?: string
  stale?: boolean
  rule?: { inputs?: string[]; conditions?: number; then?: number; else?: number }
  assessment?: {
    score?: number | null
    level?: string | null
    dimensions?: { name?: string; weight?: number }[]
    levels?: { name?: string; min?: number }[]
  }
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  binding?: LedgerBinding[]
  related?: LedgerRelated[]
  reason?: string
  /** 出自哪条查询（查询快照工件）；指标是 queries */
  query?: string
  queries?: string[]
  node_id?: string
  column?: string | null
  card?: string
  metric?: string
}

/** 一条查询的比对：proven / composed / related / none / skipped */
export interface LedgerQuery {
  artifact: string
  node_id?: string
  source?: string
  tables?: string[]
  status: 'proven' | 'composed' | 'related' | 'none' | 'skipped' | string
  reason?: string
  note?: string
  candidates: (LedgerRelated & { status?: string; how?: string })[]
  /**
   * 逐格比过的（服务端 ledger/match._cells，algo 4）：被引用的每一列各自的结论和候选。整条（按全部引用列投影）没证明、
   * 报告引用了这条查询好几列时才有；每个数的标签已经按它那一格标好，界面照标签画，不拿它重算
   */
  cells?: Record<string, { status: string; reason?: string; candidates: (LedgerRelated & { status?: string; how?: string })[] }>
}

/** 整题判断（主报告）：所有带标签的数都来自同一口径的同一版、参数一致时 equivalent */
export interface LedgerWhole {
  equivalent: boolean
  reason?: string
  counts?: Partial<Record<LedgerLabel['kind'], number>>
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  how?: 'independent' | 'composed'
  binding?: LedgerBinding[]
  missing?: string[]
  missing_required?: string[]
}

/** 按口径正式出具这一项：能不能（available / reason）、能不能自动（auto / auto_reason）、已经发起过的那一次（issued_run） */
export interface LedgerIssueAction {
  kind: 'issue'
  workflow_id: string
  version: number
  caliber: string
  workflow_name: string
  how?: 'independent' | 'composed' | null
  level?: string | null
  level_text: string
  available: boolean
  auto: boolean
  reason?: string
  auto_reason?: string
  issued_run?: string
}

/** 入口字段（比对工件里记的那一份）：核对参数的表单照它画 */
export interface LedgerField {
  name: string
  required: boolean
  has_default: boolean
}

/**
 * 看差多少（口径对账）这一项。
 *
 * `available` 是**这一项当前这个人能不能真的发起**（服务端按同一份资产授权判：应用授权给到「使用」、比对等价、
 * 绑定没被遮；builder / admin / 关登录放行）。只有 available 为真的那一项才带着口径身份（workflow_id / version）——
 * 服务端不把身份给出去的那些只是**说明**，不是操作凭据，界面也就不该拿它去发请求（真发出去入口也会 403）。
 * 人读的部分（哪条查询、哪份口径、等级、差在哪、入口字段）照旧原样给。
 */
export interface LedgerReconcileAction {
  kind: 'reconcile'
  query: string
  caliber: string
  /** 能不能真的发起对账；false 时下面两样一定没有 */
  available: boolean
  /** 要对账的那一版口径：只有 available 为真时才有 */
  workflow_id?: string
  version?: number
  node_id?: string
  workflow_name?: string | null
  level_text: string
  /** 参数是推断的、不全的、带遮罩值的：要人先核对（check_reason 是原因） */
  needs_check: boolean
  check_reason?: string
  binding: LedgerBinding[]
  /** 两条 SQL 对不上结构：多半要人选对照的指标 */
  fallback: boolean
  fields?: LedgerField[]
}

export type LedgerAction = LedgerIssueAction | LedgerReconcileAction

/** 按口径出具过的一次正式运行。tier_text 是「完整出具 / 降档出具 / 不予出具」，运行没结束时为空 */
export interface LedgerIssued {
  run_id: string
  status: string
  end_reason?: string | null
  workflow_id: string
  version: number
  tier?: string | null
  tier_text?: string | null
  auto: boolean
  created_at?: string | null
}

/** 对账卡上的一组对照：这次 / 口径 / 差值 / 相对差各是哪个指标 */
export interface LedgerReconcileGroup {
  label: string
  ours: string
  caliber: string
  diff: string
  rel: string
}

/** 对过的一次账 */
export interface LedgerReconciled {
  run_id: string
  status: string
  query?: string
  caliber?: string
  workflow_id?: string
  version?: number
  /** binding：按比对时的参数；checked：人核对过的参数 */
  inputs_from?: 'binding' | 'checked' | string
  rows?: number[] | null
  card?: string
  groups?: LedgerReconcileGroup[]
  created_at?: string | null
  /** 「说明有用 / 看不懂」（服务端 ledger/feedback.feedback_of）：看的这个人最后一次给的、各几个人 */
  feedback?: LedgerFeedback
}

export type LedgerFeedbackVerdict = 'useful' | 'unclear'

/** 对账结果上的反馈：mine 是看的这个人最后一次给的（没给过为空），useful / unclear 是各几个人（每人只算最后一次） */
export interface LedgerFeedback {
  mine?: LedgerFeedbackVerdict | null
  useful: number
  unclear: number
}

/** POST /api/ledger/answers/{run}/reconcile/{对账运行}/feedback 的答复 */
export interface LedgerFeedbackOut {
  run_id: string
  feedback: LedgerFeedback
}

/**
 * 答案身上的召回标记（GET /runs/{id}/ledger 的 recall，服务端 ledger/recall.answer_marks；细案做法 7「依据有变」「已按 vN
 * 核对，不影响」）。几种 state 都可能带：没比对过、不比对的答案照样可能被标依据有变。不放数、表名、取值
 */
export interface LedgerRecallMarks {
  /** 口径升版后召回重算、数没变：在这个答案上记的「已按 vN 核对，不影响」 */
  checked?: {
    workflow_id?: string | null
    workflow_name?: string | null
    from_version?: number | null
    to_version?: number | null
    recall_run?: string | null
    changed?: boolean
    at?: string | null
    /** 行序不定、没能核对的那几项（服务端 ledger/recall.settle）：不算进「不影响」 */
    row_order_metrics?: string[]
  }[]
  /** 依据有变：kind 是 catalog（目录里影响数字的项被驳回或修改）/ structure（表结构）/ values（新码值）/ catalog_edit */
  basis?: { kind?: string | null; items?: number; new_errors?: number; at?: string | null }[]
  /** 看的这个人手上、说的是这个答案的 recall 待办（还没处理的） */
  tasks?: { id: string; reason?: string; to_version?: number | null }[]
}

export interface LedgerCaliber {
  caliber: string
  workflow_id: string
  version: number
  level?: string
  graph_hash?: string | null
  workflow_name?: string | null
  level_text: string
}

/**
 * GET /api/runs/{id}/ledger。state：matched 比对过；pending 正在比对（或刚跑完、封存完就比）；ineligible 这次运行不比对
 * （reason 写为什么）；unreadable 比对记录读不出
 */
export interface RunLedger {
  run_id: string
  state: 'matched' | 'pending' | 'ineligible' | 'unreadable'
  reason?: string
  /** 「封存后比对，不在封存范围内」 */
  note?: string
  artifact?: string
  algo?: string
  trigger?: string
  retro?: boolean
  matched_at?: string | null
  post_seal?: boolean
  /** 事后补比的：按现在的口径回看 / 按之后发布的口径回看 */
  retro_note?: string
  calibers?: LedgerCaliber[]
  queries?: LedgerQuery[]
  reports?: Record<string, { labels: Record<string, LedgerLabel> }>
  primary?: string | null
  whole?: LedgerWhole
  actions?: LedgerAction[]
  issued?: LedgerIssued[]
  reconciled?: LedgerReconciled[]
  recall?: LedgerRecallMarks
}

/** 证据片段接口（GET /runs/{id}/evidence/segments/{seg}）的 ledger 键：这个片段的标签和比对的来历 */
export interface SegmentLedger {
  label: LedgerLabel
  artifact?: string
  algo?: string
  trigger?: string
  retro?: boolean
  matched_at?: string | null
  post_seal?: boolean
  note?: string
  retro_note?: string
}

/** POST /api/ledger/answers/{run}/issue 的答复：created 为假是同一次已经发起过、交回那一次 */
export interface LedgerIssueOut {
  run_id: string
  created: boolean
  issued: LedgerIssued
}

/** POST /api/ledger/answers/{run}/reconcile 的请求体 */
export interface LedgerReconcileIn {
  query: string
  workflow_id: string
  version: number
  inputs?: Record<string, unknown>
  confirm?: boolean
  pairs?: { ours: string; metric: string }[]
}

export interface LedgerReconcileOut {
  run_id: string
  reconciled: LedgerReconciled
}

/** 认不出列怎么配时 409（ledger_reconcile_needs_pairs）另带的选项 */
export interface LedgerPairOptions {
  columns: string[]
  metrics: { id: string; name: string; unit?: string }[]
  /**
   * 预选的对照（服务端 ledger/reconcile._suggested）：这一列单独比和口径取数的某一列等价（逐格证明了），口径卡上只读那一格的
   * 指标。界面照它预选、人可以改；老服务端不给
   */
  suggested?: { ours: string; metric: string }[]
}

// ---------------------------------------------------------------- 认证队列（可沉淀的问题）

export interface CertifyTurn {
  run_id: string | null
  conversation_id?: string | null
  turn_id?: string | null
  question: string
}

export interface CertifyFrozen {
  workflow_id: string
  name: string
  status?: string
  published_version?: number | null
}

/** 三种来源：确定性重复（SQL 形状相同）、问题聚类、被反复用的答案 */
export interface CertifyItem {
  id: string
  kind: 'shape' | 'cluster' | 'reused'
  question: string
  turns: CertifyTurn[]
  frozen_into?: CertifyFrozen[]
  // shape
  source?: string
  shape?: string
  runs?: number
  conversations?: number
  params?: { index: number; distinct: number }[]
  constants?: number
  related?: { caliber?: string; workflow_id?: string; version?: number; level?: string }[]
  representative?: { run_id: string; query?: string }
  peers?: string[]
  // cluster
  count?: number
  // reused
  uses?: { exports: number; app: boolean; extracted: boolean; canvas: boolean }
}

export interface CertifyQueue {
  items: CertifyItem[]
  considered: { runs: number; matched: number; questions: number }
  threshold: { runs: number; conversations: number; exports: number }
  embedder?: unknown
}

/** 固化成口径草稿（POST /copilot/from-run，freeze: true）的答复 */
export interface FreezeOut {
  workflow_id: string
  name: string
  nodes: number
  edges: number
  params: string[]
  metrics: string[]
  sources: CertifyTurn[]
}

// ---------------------------------------------------------------- 口径目录 / 试跑

export interface CaliberMetric {
  id: string
  name?: string
  unit?: string
  decimals?: number
  format?: string
  /** 公式（formula）、外部常量（external）、单元格条件规则（rule）、评估（assessment） */
  kind?: string
  grain?: string
  time_field?: string
  numerator?: string
  denominator?: string
  dimensions?: string[]
  filters?: string[]
  contract?: Record<string, unknown>
  /** 定义卡：文字定义、范围、排除项、负责人、生效日期、引用 */
  definition?: { text?: string; scope?: string; exclusions?: string[]; owner?: string; effective_from?: string; citations?: unknown[] }
  params?: { name: string; type: string; default?: unknown; min?: number; max?: number }[]
}

/** GET /api/ledger/calibers：一份已发布口径卡的可调用描述（不返回 SQL、不返回参数值） */
export interface CaliberDescriptor {
  workflow_id: string
  workflow_name: string
  version: number
  level: string
  graph_hash?: string | null
  semantic_hash?: string | null
  card: { node_id: string; title: string; caliber?: string | null; caliber_version?: string | null }
  metrics: CaliberMetric[]
  entry_schema: FormSchema
  composable: boolean
  compose_reason?: string | null
  dependencies: {
    sources: string[]
    tables: string[]
    catalog_versions?: Record<string, unknown> | null
    schema_versions?: Record<string, unknown> | null
  }
  issuance: {
    level: string
    auto_issue: boolean
    requires_independent_proof: boolean
    side_effects?: string | null
    reasons: string[]
  }
}

export interface ValidationCheck {
  code: string
  status: 'pass' | 'fail' | 'unknown'
  detail: string
  metric_id?: string
  name?: string
}

export interface QualityReport {
  status: 'pass' | 'fail' | 'unknown'
  checks: ValidationCheck[]
}

export interface CaliberTrialMetric extends CaliberMetric {
  quality?: QualityReport
  masked?: boolean
  value?: unknown
  rendered?: string
}

/** POST /api/ledger/calibers/{wf}/{version}/{node}/trial：按固定版本试跑一次 */
export interface CaliberTrialOut {
  validation?: QualityReport
  run_id: string
  status: string
  output?: Record<string, unknown> | null
  error?: string | null
  metrics: CaliberTrialMetric[]
  /** 这次试运行各取数节点读到的表；旧服务端没有 */
  queries?: DraftTrialQuery[]
}

// ---------------------------------------------------------------- 从业务文档起草口径

/**
 * 草稿的细状态（app/ledger/docdraft.STATUSES）：起草中 → 待确认问题 / 待接数据路径 / 待签字发布 → 已接上；
 * 另有已驳回。界面不再逐个画这些状态，按 CaliberDraftStage 折成四种阶段；细状态留在标签的悬停提示里。
 * ready 表示图已经生成、没有未决问题，可以试运行、可以签字发布；questions_open 也有图和数，
 * 只是还没清完问题，不阻断试运行。path_pending 是旧路径：图还没生成，取数只能来自一次真实运行。
 */
export type CaliberDraftStatus = 'drafting' | 'questions_open' | 'path_pending' | 'ready' | 'attached' | 'rejected' | 'failed'

/**
 * 界面上的阶段（服务端 stage）：起草中 / 待审 / 已丢弃 / 失败。ready、questions_open、path_pending、attached
 * 都归「待审」；细状态仍照实给，需要说清楚时用 status。
 */
export type CaliberDraftStage = 'drafting' | 'review' | 'discarded' | 'failed'

/** 一条引用：逐字原文、原文所在段落（chunk）、段号、span 是原文里的 [start, end) */
export interface DraftCitation {
  chunk: string
  document_id?: string
  /** 原文所在段落的段号（1 起） */
  ordinal?: number
  quote: string
  span?: [number, number]
}

/** 一条指标的定义（docdraft.DEFINITION_FIELDS） */
export interface DraftDefinition {
  text?: string
  scope?: string
  exclusions?: string[]
  owner?: string
  effective_from?: string
  citations?: DraftCitation[]
}

/** 草稿里的一条指标：业务定义 + 取数表达式；取数 SQL 在草稿图上（CaliberDraft.sql），这里不返回。 */
export interface DraftMetric {
  id?: string
  name: string
  synonyms?: string[]
  unit?: string
  /** 小数位 */
  decimals?: number
  /** 指标表达式，形如 cell(nodes.fetch_1, 0, '列')；一组取数时节点 id 是 fetch */
  expression?: string
  contract?: Record<string, unknown>
  /** 粒度，如「日」「订单行」 */
  grain?: string
  /** 业务时间字段的业务叫法 */
  time_field?: string
  filters?: string[]
  numerator?: string
  denominator?: string
  dimensions?: string[]
  definition?: DraftDefinition
  open_questions?: string[]
  /**
   * K04 的业务字段：统计时间口径、退款处理。指标声明了它们，取数 SQL 上的受控绑定才跟着走；
   * 界面按 binding.editable_fields 的 value_labels 把机器值翻成给人看的说法，不直接显示这两个键名
   */
  time_role?: string
  refund_rule?: string
}

/** 草稿图的入口字段：发布后按这些字段收参数 */
export interface DraftEntryField {
  name: string
  type: string
  required: boolean
  default?: unknown
  description?: string
}

/**
 * 试运行的状态：还会缺必填入口字段（needs_input），这时不跑，等人填了再发起；needs_input 没有 run_id。
 */
export type DraftTrialStatus = 'running' | 'succeeded' | 'failed' | 'needs_input' | 'stale'

/** 起草后自动发起的那次只读试运行（draft.payload.trial） */
export interface DraftTrial {
  run_id: string
  status: DraftTrialStatus
  error?: string
  version?: number
  graph_hash?: string
  execution_hash?: string
  inputs_hash?: string
}

/** GET /api/ledger/drafts/{id}/trial 的一条：name 和 rendered 一一对应 */
export interface DraftTrialMetric {
  quality?: QualityReport
  masked?: boolean
  id: string
  name: string
  value?: unknown
  rendered?: string
}

/**
 * 一次试运行里某个取数节点读到的一张表：列头、前几行、总行数；截断时只带回前若干行，
 * 遮罩过的格子在 rows 里已经是「已遮罩」这样的字样，页面照原样显示。
 */
export interface DraftTrialQuery {
  node_id: string
  /** 这张表是查还是别的工具拿到的（旧服务端可能没有） */
  tool?: string
  /** 取数用的数据源 */
  source?: string
  columns: string[]
  rows: unknown[][]
  total_rows: number
  /** 只带回了前若干行 */
  truncated?: boolean
  source_truncated?: boolean
  preview_truncated?: boolean
  /** 遮罩过的列名；格子本身已经是遮罩后的字样 */
  masked?: string[]
}

/** GET /api/ledger/drafts/{id}/trial：这次自动试运行的状态和每个指标的值；POST 同一个路径按填好的入口字段再发起一次 */
export interface DraftTrialOut {
  validation?: DraftTrialValidation
  run_id: string
  status: DraftTrialStatus
  metrics: DraftTrialMetric[]
  /** 这次试运行各取数节点读到的表；旧服务端和旧草稿没有 */
  queries?: DraftTrialQuery[]
  error?: string | null
}

/**
 * 试跑响应里的 validation。它不是一份固定的质量报告：服务端按分支给不同的东西——成功时是质量结论
 * （status + checks），另并进这次试跑对应的**验证记录**一段（K05 的 record / covers_current / note）；
 * stale、needs_input、running 这些分支可能只有其中一段，甚至只有 note（老服务端也可能什么都不给）。
 *
 * 所以按**联合形状**读：status 缺席就是「没有质量结论」，不能当成通过（也不写成未知通过）；covers_current
 * 可能是 null（调用方没有当前依据），只有 === true 才算覆盖。质量结论自己走 QualityReport 的类型，
 * 由页面按 status 认全之后才交给质量报告那块画。
 */
export interface DraftTrialValidation {
  /** 这一段自己的格式版本（服务端新加的；认不得就照实当没有） */
  schema_version?: number
  /** 这一次执行是怎么结束的（succeeded / failed / running / needs_input / stale） */
  execution?: string
  status?: 'pass' | 'fail' | 'unknown'
  checks?: ValidationCheck[]
  /** 这次试运行对应的验证记录；没有就是 null（缺证据不能算验证通过） */
  record?: DraftValidationRecord | null
  /** 这条记录还覆不覆盖当前定义/绑定：只有 === true 才算覆盖，null 是判不了 */
  covers_current?: boolean | null
  /** 服务端就这一段的一句说明（例如「这次试运行还没有留下可对应的验证记录」） */
  note?: string
}

/** 带独立期望试运行的那一份：指标要逐条给全卡指标，来源是三种之一 */
export interface DraftExpectIn {
  metrics: Record<string, unknown>
  source?: DraftExpectationSource
  sample_id?: string
  sample_version?: string
  note?: string
}

/** 被丢弃的条目和原因（含 SQL 痕迹、引用核对不上） */
export interface DraftDropped {
  name: string
  reason: string
}

/** 草稿和已发布口径的对齐结果：新的、已有出处的、和现有口径定义不同的 */
export interface DraftAlignment {
  new: string[]
  duplicate: { name: string; workflow_id?: string; version?: number; node_id?: string }[]
  conflict: { name: string; workflow_id?: string; version?: number; reason?: string }[]
}

/** 起草时用了哪份文档的哪些段落 */
export interface DraftDocument {
  document_id: string
  title: string
  chunk_ids: string[]
}

/** GET /api/ledger/drafts 的一条：只有计数和状态，正文用 GET /api/ledger/drafts/{id} 看 */
export interface CaliberDraftSummary {
  id: string
  title: string
  status: CaliberDraftStatus
  /** 服务端算的界面阶段；旧服务端没有，页面按 status 推 */
  stage?: CaliberDraftStage
  metric_count: number
  question_count: number
  workflow_id: string | null
  created_at: string | null
  error: string | null
}

/** 起草轮次和覆盖情况：多轮续跑时每轮更新，covered 是已经起草出的指标名数，uncovered 是还没覆盖的 */
export interface DraftProgress {
  round: number
  covered: number
  uncovered: number
}

/** 发布前检查的一项：服务端从图和试跑状态算出来，只读，不拦人 */
export interface DraftCheck {
  key: string
  label: string
  ok: boolean
  detail?: string
}

/** 草稿图里一组取数：一条 SQL 出哪几个指标（metric_ids 对应 DraftMetric.id） */
export interface DraftGroup {
  sql: string
  entry_fields?: DraftEntryField[]
  metric_ids?: string[]
}

/** 草稿图上能写的口径卡：多张时 PATCH 要指明改哪一张（card_id） */
export interface DraftCardRef {
  id: string
  title: string
}

/** 按字段改一处（K03）：op 只有 set / remove，path 是白名单里的字段路径，位置由 metric_id 定，不用数组下标 */
export interface DraftOperation {
  op: 'set' | 'remove'
  metric_id: string
  path: string
  /** 只有 set 才有：写 '' 是「声明成空」、数组整份设置；remove 不接受 value */
  value?: unknown
}

// ---------------------------------------------------------------- K04 业务绑定能力（服务端能力描述）

/** 一个能按字段改的业务定义：机器值 + 给人看的说法 */
export interface DraftEditableField {
  field: string
  label: string
  values: string[]
  /** 机器值 → 界面上写的说法 */
  value_labels: Record<string, string>
  help: string
}

/** 一个取数节点上「定义 vs 取数实现」的判定结果（aligned / needs_update / unsupported / not_declared） */
export interface DraftBindingNode {
  node_id: string
  status: string
  reason: string
  /** 该做什么：none / sync / author / manual */
  fix: string
  declared: Record<string, string>
  values: Record<string, string>
  metrics: string[]
  binding_fingerprint?: string
}

/**
 * GET /api/ledger/drafts/{id} 的 binding（K04）：能按字段改哪些业务定义、允许值、现在的值、定义和取数实现的
 * 同步状态、不支持时的原因和该做什么。**不给模板和 SQL**；aligned 只说定义和实现同步，不等于验证通过。
 */
export interface DraftBinding {
  editable_fields: DraftEditableField[]
  /** 现在的业务值：字段 → 值（各取数节点里最先出现的那一个） */
  current: Record<string, string>
  status: 'aligned' | 'needs_update' | 'unsupported' | 'unbound' | string
  reason: string
  fix: string
  /** 这次判定涉及哪些指标（metric_id） */
  metrics: string[]
  checked: boolean
  /** 每条指标声明的业务值：metric_id → {字段: 值}（工作台按它算「改这一项影响哪几条指标」） */
  metric_values: Record<string, Record<string, string>>
  unsupported_reason?: string
  binding_fingerprints?: Record<string, string>
  nodes?: DraftBindingNode[]
  /** 只有变更回执里的那一段有：这一次在同一事务里把取数 SQL 也改过来了 */
  synced?: boolean
}

// ---------------------------------------------------------------- K05 / K06 验证与审阅

/**
 * 一条验证记录的状态。**没有 pass / verified 这种状态**：matched 只是「这一次和这份期望对得上」，
 * observed 是「跑了、取了数，但没有独立期望」，都不能说成验证通过（结论看 DraftValidationSummary.verdict）。
 */
export type DraftValidationStatus = 'matched' | 'mismatched' | 'observed' | 'unverified' | 'failed'

/** 期望来自哪里：人工确认 / 固定夹具 / 独立计算。不能拿当前 SQL 的结果抄一份当标准答案 */
export type DraftExpectationSource = 'human_confirmed' | 'fixture' | 'independent_computation'

/** 验证总情况里的结论（服务端现算）：verified 要「有独立期望对得上」且「有覆盖它的认证审阅」 */
export type DraftValidationVerdict = 'none' | 'stale' | 'verified' | 'rejected' | 'mismatched' | 'failed' | 'observed' | 'unverified'

/** 这条记录上最新的一次审阅决定（认证主体、确认范围、所审依据） */
export interface DraftValidationReview {
  id: string
  draft_id: string
  record_id: string
  workflow_id?: string | null
  decision: 'confirmed' | 'rejected' | string
  actor?: string | null
  /** 主体是不是认证过的（关登录时自报的署名是 false） */
  verified: boolean
  /** 确认范围：这份记录覆盖的指标，可以只确认其中一个子集 */
  scope: string[]
  basis?: Record<string, unknown>
  note?: string | null
  created_at?: string | null
  /** 这条决定还算不算覆盖当前定义/绑定 */
  covers_current?: boolean | null
}

/** 这次运行交回的某一个指标的实际值：值、形状、行数、遮罩，以及和期望比出来的结果 */
export interface DraftValidationActual {
  value?: unknown
  status?: string
  rendered?: string
  rows?: number | null
  masked?: boolean
  truncated?: boolean
  shape?: string | null
  shape_ok?: boolean | null
  /** 引擎记过「业务描述和取数条件的关系还没被证明」：待人工确认的缺口，不改这次比较的结论 */
  semantic_unverified?: boolean
  findings?: string[]
  /** 期望给的、对比的那个原值 */
  expected?: unknown
  /** 对得上 / 对不上 / 判不了（null） */
  ok?: boolean | null
}

/** 这一次试跑对着比的独立期望 */
export interface DraftValidationExpected {
  source: DraftExpectationSource | string
  metrics: Record<string, unknown>
  note?: string | null
  hash?: string
}

/** GET /api/ledger/drafts/{id}/validations 的一条记录（只追加，不改写旧的） */
export interface DraftValidationRecord {
  id: string
  draft_id: string
  workflow_id?: string | null
  version: number
  card_id: string
  metric_ids: string[]
  definition_hash?: string
  binding_fingerprints?: Record<string, string>
  semantic_hash?: string
  execution_hash?: string
  method: { version: number; note: string }
  sample: { id: string; version: string }
  inputs: Record<string, unknown>
  inputs_hash?: string
  /** 没有独立期望就是 null：只记 observed，不能算验证通过 */
  expected: DraftValidationExpected | null
  actual: Record<string, DraftValidationActual>
  status: DraftValidationStatus
  problems: string[]
  run_id?: string
  trial_id?: string
  actor?: string | null
  /** trial：试跑留下的；late：到终态时补记的 */
  origin?: string
  created_at?: string | null
  /** 还覆不覆盖**当前**定义/绑定（定义、取数或绑定改过就对不上了） */
  covers_current?: boolean | null
  review?: DraftValidationReview | null
}

/** 一份草稿的验证总情况；verdict 才是结论 */
export interface DraftValidationSummary {
  verdict: DraftValidationVerdict
  /** 拿来下结论的那条记录的状态；一条记录都没有时是 none。老服务端可能不给这一段 */
  status?: DraftValidationStatus | 'none'
  /** 只有 === true 才算覆盖当前定义/绑定；false 是不覆盖，缺字段是判不了 */
  covers_current?: boolean | null
  record_id?: string | null
  review?: DraftValidationReview | null
  /** 这次被确认到的指标（确认范围）：确认范围要盖住这条记录的全部指标才算整张卡通过 */
  confirmed?: string[]
  /** 这条记录覆盖、但还没被确认的指标：非空就不算整张卡通过 */
  unconfirmed?: string[]
  /** 逐状态的条数；读数缺字段时按 0 显示，不猜 */
  counts?: { total?: number; covering?: number } & Record<string, number | undefined>
  reason: string
  /** 记录里哪些指标的语义说明仍待人工确认 */
  semantic_gaps?: string[]
  method?: { version: number; note: string }
}

/**
 * 字段基线 B 覆盖到哪一步（K11 的按字段推进）：complete 是整张卡每一格都有基线，partial 是还有没基线的格
 * 或整条没基线的指标，unknown 是读不出来。「有没有基线」看 status，**「整张是不是都有」看这里**，两件事不能混。
 */
export interface DraftFieldCoverage {
  status: 'complete' | 'partial' | 'unknown' | string
  /** 逐条指标覆盖了几格、缺哪些格 */
  metrics?: Record<string, { covered: number; expected: number; missing: string[] }>
  /** 整条都没有基线的指标 */
  uncovered_metric_ids?: string[]
}

/** 字段基线 B 的状态：known 有可还原的接受导入值 / baseline_unknown 没有（不能拿当前内容冒充 B） */
export interface DraftFieldBaseline {
  status: 'known' | 'baseline_unknown' | string
  reason?: string
  artifact_id?: string
  version?: number
  content_hash?: string
  card_id?: string
  workflow_id?: string
  accepted_revision?: number
  accepted_at?: string
  metric_ids?: string[]
  field_count?: number
  /** known 不等于完整：按字段推进出来的基线常常只覆盖卡上的一部分 */
  coverage?: DraftFieldCoverage
  source_snapshot_artifact_id?: string
}

/** GET /api/ledger/drafts/{id}/validations：记录（新的在前）+ 总情况 + 字段基线状态 */
export interface DraftValidationsOut {
  draft_id: string
  /** 老服务端可能不给这一段：显示时照实说读不到，不当成「有基线」 */
  field_baseline?: DraftFieldBaseline
  records: DraftValidationRecord[]
  summary: DraftValidationSummary
  method?: { version: number; note: string }
}

/** PATCH 草稿（operations 那条路）回来的变更回执：改了什么、影响哪些指标、绑定同步到哪一步、旧验证怎么失效 */
export interface DraftChangeReceipt {
  applied: boolean
  classification: 'definition' | 'display' | 'none' | string
  card: string
  metrics: string[]
  operations: { op: string; metric_id: string; path: string; changed: boolean }[]
  revision: number | null
  definition_hash: { before: string; after: string; changed: boolean }
  validation: { invalidated: boolean; scope: string; metrics: string[]; reason: string; next: string }
  binding: DraftBinding
}

/** GET /api/ledger/drafts/{id}：payload 摊平在上层（documents / metrics / dropped / open_questions / alignment） */
export interface CaliberDraft extends CaliberDraftSummary {
  job?: { status: string; attempt: number; error?: string | null; retryable: boolean }

  /**
   * 这份草稿的权威口径卡（K02）。已接管的草稿（有取数图的）才有：图上一张可写的卡时就是它，多张时是下游契约
   * 声明的那一张；认不出来（多张又没有唯一目标）时是 null，改的时候要显式给 target.card_id
   */
  card_id?: string | null
  /** 图上能写的口径卡，一张一条；页面上要人挑的时候用它 */
  cards?: DraftCardRef[]
  /** 客户端要带回来的基准版本（Workflow.version）：PATCH 时当 expected_revision。老草稿没有图时为 null */
  revision?: number | null
  /**
   * K04 的业务绑定能力描述：能按字段改哪些业务定义、允许值、现在的值、定义和取数实现的同步状态。
   * 已接管的草稿才有；工作台照它决定展示哪些业务修改（逐资产启用）
   */
  binding?: DraftBinding
  /** K05 的字段基线 B 状态：有可还原的接受导入值 / 无法还原（老草稿） */
  field_baseline?: DraftFieldBaseline
  /** K05 的验证总情况：结论、覆盖判断、最新一条、审阅；整条记录走 /validations */
  validation?: DraftValidationSummary
  /** 这次 PATCH 走 operations 时的变更回执（改了什么、影响指标、绑定同步到哪一步） */
  change?: DraftChangeReceipt
  /** 这一次 PATCH 和上次是同一个 request_id（已经写进去了，没有新增版本） */
  replayed?: boolean

  documents?: DraftDocument[]
  metrics?: DraftMetric[]
  /** 草稿图的入口字段 */
  entry_fields?: DraftEntryField[]
  /** 入口字段在各组里声明不一致的地方（必填取并集、类型取更宽），合并时记下的说明 */
  entry_notes?: string[]
  /** 草稿图里的取数 SQL 模板：入口字段在运行时渲染；页面只读展示，不出现执行值 */
  sql?: string | null
  dropped?: DraftDropped[]
  open_questions?: string[]
  /** 还没有起草出来的指标名称：续跑跑到轮数上限或没有新指标后剩下的，可以再起一份 */
  uncovered?: string[]
  /** 这张草稿图有几个取数节点（fetch_1、fetch_2…）：多组指标共用一张口径卡 */
  blocks?: number
  /** 逐组取数：每条 SQL 出哪几个指标；旧草稿没有，页面回退到拼接的 sql 字段 */
  groups?: DraftGroup[]
  /** 多轮续跑的进度；旧草稿没有 */
  progress?: DraftProgress
  /** 模型回复被截断，指标是从完整片段里抢救出来的 */
  salvaged?: boolean
  alignment?: DraftAlignment
  /** 发布前检查；旧草稿没有图时不给 */
  checks?: DraftCheck[]
  /** 起草后自动发起的那次只读试运行；旧草稿可能没有 */
  trial?: DraftTrial | null
  /** 草稿图对应的版本号 */
  version?: number | null
  /** 文档超过一次能发给模型的字数，后面的段落没有参与起草 */
  doc_truncated?: boolean
  /** 数据目录太大，只展开了文档点到的几张表 */
  catalog_truncated?: boolean
  /** 旧服务端只给这一个：上面两种截断之一，界面按较窄的那条说 */
  truncated?: boolean
  model_id?: string | null
  collection?: string | null
  datasource_id?: string | null
  /** 起草时的文档 id 和附加要求 */
  origin?: { documents?: string[]; guidance?: string }
  created_by?: string | null
  updated_at?: string | null
  workflow_name?: string | null
}

/** POST /api/ledger/drafts/{id}/attach：接上一次真实运行后建出的工作流版本 */
export interface CaliberDraftAttachOut {
  workflow_id: string
  version: number
  /** 并进了定义的指标（口径卡上的 metric） */
  metrics: unknown[]
  /** 对不上已有指标名的那些，由人决定新建还是忽略 */
  warnings: string[]
}

// ---------------------------------------------------------------- K11 增量重导入（B / L / R 三方）

/**
 * 一格的三方状态（app/ledger/reimport.FIELD_STATES）。**能不能接受由状态定**：
 * update（无冲突更新）/ conflict（人工改过）/ new（新增候选）/ manual（没有可比基线，要人工对齐）能接受；
 * missing（这次没提取到）**不能**——漏提取不当删除，L 上的原值原样留着；unchanged 不用接受。
 */
export type DraftImportFieldState = 'update' | 'conflict' | 'new' | 'missing' | 'manual' | 'unchanged'

/** 一条候选指标（工作项）的状态：逐字段三方算出来的 + 人处置之后的 */
export type DraftImportItemStatus = 'pending' | 'update' | 'conflict' | 'new' | 'manual' | 'unchanged'
  | 'partial' | 'accepted' | 'rejected' | 'failed'

/** 一格的三方：b 是上次被接受的导入值、l 是现在图上人工维护的值、r 是这次抽出来的候选 */
export interface DraftImportField {
  path: string
  state: DraftImportFieldState | string
  b?: unknown
  l?: unknown
  r?: unknown
  b_present: boolean
  l_present: boolean
  r_present: boolean
  /** 人处置过没有：accepted / rejected / null */
  decided?: string | null
  /** 这一格现在能不能接受（和状态一一对应，界面按它禁不禁用按钮） */
  acceptable: boolean
}

/** 这条候选对上了哪条指标、凭什么（名字像不像不参与，只认稳定身份和来源证据） */
export interface DraftImportAlign {
  metric_id?: string
  /** metric_id（候选带着稳定身份）/ evidence（按来源证据）/ new（没对上）/ manual（要人指定） */
  mode?: string
  reason?: string
  evidence?: string[]
  live_evidence?: string[]
  /** 证据比对取自哪个：baseline（B 的引用）还是 live（当前图的引用） */
  basis?: string
  /** mode 是 manual 时，对上的候选有哪几条 */
  candidates?: string[]
  manual?: boolean
  /** 引用指向的段落没进这次提示：不能当出处，记下来 */
  invisible_citations?: unknown[]
}

/** 一条工作项（一条候选指标）：逐字段三方 + 人的处置 */
export interface DraftImportItem {
  item_id: string
  seq: number
  /** 对齐到的图上指标；没对齐上时是空串 */
  metric_id: string
  /** 候选自己的身份（和图上无关） */
  candidate_id: string
  /** 持久对齐键（稳定身份 + 规范化的来源证据）：重新抽之后还按它认同一条工作项 */
  align_key?: string
  /** 服务端标的「这条抽坏了」（引用一段没有给过模型的原文）：它上面一律不能接受 */
  failed?: boolean
  name: string
  status: DraftImportItemStatus | string
  align: DraftImportAlign
  error: string
  accepted: string[]
  rejected: string[]
  applied_revision?: number | null
  fields: DraftImportField[]
}

/** 一次导入里值得单独列出来的格子（冲突 / 要人工对齐 / 漏提取） */
export interface DraftImportConflict {
  metric_id: string
  item_id: string
  seq: number
  name: string
  path: string
  state: string
  b?: unknown
  l?: unknown
  r?: unknown
}

/**
 * 一次增量重导入（GET /api/ledger/drafts/{id}/imports、GET /api/ledger/imports/{run_id}）。
 * **R 只是候选，不改 L**：工作项和冲突是后台抽出来的，真正落到图上的只有「接受选定字段」。
 */
export interface DraftImportRun {
  run_id: string
  draft_id: string
  workflow_id: string
  /** queued / running / succeeded / partial / failed / interrupted / cancelled */
  status: string
  error: string
  actor: string
  request_id: string
  created_at: string
  items_total: number
  items_decided: number
  source_snapshot?: Record<string, unknown>
  visible_chunk_ids?: string[]
  prompt_version?: number
  parse_version?: number
  model_id?: string
  items: DraftImportItem[]
  conflicts: DraftImportConflict[]
}

/** POST /api/ledger/imports/{run_id}/accept：这一次落到哪一版、是不是重放（B 和 L 在同一次提交里推进） */
export interface DraftImportAcceptOut extends DraftImportRun {
  applied: boolean
  replayed: boolean
  revision: number | null
}

// ---------------------------------------------------------------- K12 受限的自然语言修改（只产出提案）

/** 必须先问清的澄清项：含糊业务条件（要人指定取值）或没匹配到已声明的能力 */
export interface NlChangeClarification {
  kind: 'ambiguous' | 'no_match' | 'missing' | string
  field?: string | null
  question: string
  options: { value: string; label: string }[]
}

/**
 * 一句话产出的受限修改提案：K03 同一形状的 operations + 影响摘要（preview）+ 澄清项。
 * **只读**：不落库、不新增版本；ready 为真才谈得上应用。
 */
export interface NlChangeProposal {
  card: string
  ready: boolean
  operations: DraftOperation[]
  metrics: string[]
  clarifications: NlChangeClarification[]
  /** changes.summary 的同一份形状（preview 为 true、persisted 为 false）；服务端没给就是 null */
  preview?: (DraftChangeReceipt & { preview?: boolean; persisted?: boolean }) | null
  /** 模型自报但不作数的键（批准、审阅这类），只是照实记下来 */
  ignored_claims?: string[]
  notes?: string[]
}

/**
 * POST /api/ledger/drafts/{id}/change-proposal（只预览）的回：提案 + 这次没落库（applied / persisted 都是 false）。
 * POST …/nl-change 的 confirm=true 交回的是**草稿详情**（见 NlChangeOut），不是这个形状。
 */
export interface NlChangePreviewOut {
  draft_id: string
  workflow_id: string
  revision: number
  card: string
  proposal: NlChangeProposal
  applied?: boolean
  persisted?: boolean
}

/** POST …/nl-change 的回：confirm=false 是 NlChangePreviewOut；confirm=true 是草稿详情（带这一次的 change 回执） */
export type NlChangeOut = NlChangePreviewOut | (CaliberDraft & { replayed?: boolean })
