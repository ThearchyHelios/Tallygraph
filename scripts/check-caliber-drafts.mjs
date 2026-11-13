#!/usr/bin/env node
// 口径草稿页上的业务规则工作台（K06 + K11 的文档更新 + K12 的一句话改口径）：/ledger/drafts 里「找到指标 → 改受支持的
// 统计时间口径 / 退款处理 → 看旧→新和影响指标 → 一次提交 → 看新版本、同步状态、验证与审阅 → 能撤销」这一条路走不走得通，
// 以及增量重导入和一句话改口径这两块。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 业务用户改的是业务写法，看不到取数 SQL、节点 id、字段路径和 JSON；能力描述为空、认不出唯一口径卡、没有版本号的
//     资产保持只读，并说清为什么；
//   - 改完先列「会改到哪几条指标、现在什么写法 → 保存后什么写法」，再把**所有**受影响指标的字段操作放在一次 PATCH
//     里（带基准版本、目标卡、幂等键）；入口字段这类查询参数的变化只发试运行，不会发 PATCH、不新增定义版本；
//   - 保存回执说清落成哪一版、取数同步到哪一步、旧验证怎么失效；同步不了的说「定义存下来了、取数一个字没动」+ 技术
//     待办，不写「已同步」；
//   - 撤销按**保存前**的值构造逆操作（原来没声明的用 remove），带上当前版本和**新的**请求键再存一版，不顺一版整份覆盖；
//   - 基准版本过期（409 stale_revision）：写不进去，页面上的写法选择一个字不丢、也不自动重发，详情刷新拿到新版本后由人
//     显式再点一次（同内容仍用同一个请求键）；
//   - 验证那一段按证据说：没有独立期望就是「只记了试运行结果」，样例对得上但没审阅是「还不能算验证通过」，只有独立期望
//     对得上、并且有认证审阅才是验证通过；审阅只调真接口（请求体里没有 actor），拿回来的「谁、覆盖不覆盖」照原样显示；
//   - 增量重导入（K11）：三方的值（上次被接受的导入值 / 现在的内容 / 这次文档里的说法）和逐格状态照实摆出来；**接受是
//     唯一的落图路径**（一次提交里新增一版图 + 按被接受的格子推进 B），没接受过的格子不推进基线；**提交一律按工作项
//     身份（item_id）指认、把要落的几格明确列出来**（同一条指标上有多条候选时不会指错、不发 auto）；抽坏了的（failed）
//     工作项一个接受都点不了、批量也不收它；已接受 / 拒绝过的格子不再被选；漏提取不当删除（不能接受、现在的内容原样
//     留着）；没有可比基线时逐格要人工对齐到某条指标，没对齐之前不能接受；同名不同范围也要人来定；被中断的导入由人点
//     「恢复」显式续跑；写不进去时选好的格子一个不丢，且**不拿旧 L 覆盖**（stale_revision 重读详情，import_stale_l 按
//     现在的口径重算三方 + 指名道姓说清是哪几格）；有基线不等于整张都有，coverage 是 partial 就写清还差多少；
//   - 一句话改口径（K12）：只调只读的提案接口，影响只写「哪条指标、哪一项写法、现在什么 → 保存后什么」，不摆 JSON、
//     节点编号和字段路径；含糊的业务条件先显示澄清项、一条操作都不产出、也不给确认按钮；**确认（confirm=true）之前
//     一个字都不写**；确认发的是**预览时那一段原文**（输入改过就必须重新预览，页面显示的和实际落下的不会是两句话），
//     落版之后另比一次服务端落下的操作和预览里的是不是同一批；落版失败时照实报错，页面上的输入和预览原样留着；
//   - 结论一律按 data 属性（data-draft-validation 等）核：`还不能算验证通过` 这种否定说法里也有「验证通过」四个字，
//     用 includes 会被骗过（页面没通过、检查却绿了）。
//
// 不连着后端写：/api/ 下的请求几乎全部在浏览器层答掉（草稿详情、试运行、验证记录、字段操作、审阅、重导入、提案），
// 其余 GET 放行给开发服务器（外壳要的），不认得的写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-caliber-drafts.mjs
// CHECK_ONLY=撤销,复杂 只跑段名里含这些字的段。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const opened = new Set()
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k))) return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}
const until = async (fn, ms = 6000, step = 100) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) {
    await new Promise((r) => setTimeout(r, step))
    v = await fn()
  }
  return v
}
const clone = (x) => JSON.parse(JSON.stringify(x))

const ADMIN = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }

/** 字段操作那一批要落成的东西：两条指标 × 两项业务写法 */
const OLD = { time_role: 'order_time', refund_rule: 'include_all' }
const NEW = { time_role: 'payment_time', refund_rule: 'exclude_full_refund' }

/** 服务端能力描述的 editable_fields（和 app/ledger/bindings.editable_fields 同一份形状） */
const EDITABLE = [
  { field: 'time_role', label: '统计时间口径', values: ['order_time', 'payment_time'],
    value_labels: { order_time: '按下单时间', payment_time: '按支付时间' },
    help: '这条指标的统计口径按哪个时间点算：下单时间还是支付时间' },
  { field: 'refund_rule', label: '退款处理', values: ['include_all', 'exclude_full_refund'],
    value_labels: { include_all: '全部计入', exclude_full_refund: '排除全额退款' },
    help: '退款订单算不算进来：全额退款排除、部分退款保留' },
]

const ALIGNED_REASON = '定义和取数 SQL 同步（aligned 只说这两边对得上，不等于业务验证通过）'
const UNSUPPORTED_REASON = '这条 SQL 里没有「统计时间口径」的落点（受控模板里没有 [[time_role]]）：'
  + '这条取数不是受控模板写出来的，改不了'

/** 一份已接管的草稿：两条指标都声明着业务写法，图上一张能改的口径卡，客户端要带回 v7 */
function draftOf(over = {}) {
  return {
    id: 'd-orders', title: '订单口径（单据）', status: 'ready', stage: 'review', metric_count: 2, question_count: 0,
    workflow_id: 'wf-orders', workflow_name: '订单口径', created_at: '2026-11-01T02:00:00+00:00', error: null,
    card_id: 'card', cards: [{ id: 'card', title: '订单口径' }], revision: 7, version: 7,
    metrics: [
      { id: 'm1', name: '订单数', unit: '单', expression: "cell(nodes.fetch, 0, 'orders')", grain: '订单行',
        time_role: OLD.time_role, refund_rule: OLD.refund_rule },
      { id: 'm2', name: '订单金额', unit: '元', expression: "cell(nodes.fetch, 0, 'amount')", grain: '订单行',
        time_role: OLD.time_role, refund_rule: OLD.refund_rule },
    ],
    entry_fields: [{ name: 'start', type: 'string', required: true }, { name: 'end', type: 'string', required: true }],
    trial: { run_id: 'run-1', status: 'succeeded', version: 7 },
    binding: bindingOf({ status: 'aligned', reason: ALIGNED_REASON, fix: 'none', synced: false, ...(over.binding ?? {}) }),
    field_baseline: { status: 'known', artifact_id: 'bl-1', version: 1, content_hash: 'bl-1', card_id: 'card',
      workflow_id: 'wf-orders', accepted_revision: 1, accepted_at: '2026-11-01T02:00:00+00:00',
      metric_ids: ['m1', 'm2'], field_count: 4 },
    ...over.draft,
  }
}

function bindingOf(over = {}) {
  const values = over.metric_values ?? { m1: clone(OLD), m2: clone(OLD) }
  return {
    editable_fields: EDITABLE,
    current: over.current ?? clone(OLD),
    status: over.status ?? 'aligned',
    reason: over.reason ?? ALIGNED_REASON,
    fix: over.fix ?? 'none',
    metrics: over.metrics ?? ['m1', 'm2'],
    checked: true,
    metric_values: values,
    ...(over.status === 'unsupported' ? { unsupported_reason: over.reason ?? UNSUPPORTED_REASON } : {}),
    binding_fingerprints: { fetch: 'fp-1' },
    nodes: [{ node_id: 'fetch', status: over.status ?? 'aligned', reason: over.reason ?? ALIGNED_REASON,
      fix: over.fix ?? 'none', declared: clone(OLD), values: clone(OLD), metrics: ['m1', 'm2'],
      binding_fingerprint: 'fp-1' }],
    synced: !!over.synced,
  }
}

/** 一次 PATCH 的变更回执（和 app/ledger/changes.summary 同一份形状） */
function receiptOf({ revision, metrics = ['m1', 'm2'], operations = [], binding, applied = true }) {
  return {
    applied, classification: applied ? 'definition' : 'none', card: 'card', metrics: applied ? metrics : [],
    operations, revision,
    definition_hash: { before: 'dh-1', after: applied ? 'dh-2' : 'dh-1', changed: applied },
    validation: {
      invalidated: applied, scope: 'card', metrics: applied ? metrics : [],
      reason: applied
        ? '口径卡的内容变了（图的执行指纹跟着变）：旧试跑和旧验证不再算覆盖这一版，按卡片保守失效（4.8）'
        : '这次操作没有改动任何内容：图没换版本，原来的验证仍然对着这一版',
      next: applied ? '重新试运行，按新定义取一次独立样例' : '不用重跑',
    },
    binding,
  }
}

/** 验证记录的一条：和 app/ledger/validation.as_dict 同一份形状 */
function recordOf({ id = 'vr-1', status = 'observed', expected = null, ok = null, covers = true, review = null,
  metric_ids = ['m1', 'm2'], actual = null } = {}) {
  return {
    id, draft_id: 'd-orders', workflow_id: 'wf-orders', version: 7, card_id: 'card', metric_ids,
    definition_hash: 'dh-1', binding_fingerprints: { fetch: 'fp-1' }, semantic_hash: 'sh-1', execution_hash: 'eh-1',
    method: { version: 1, note: '原值比较、不做隐式舍入；逐条比较卡上全部指标；声明成标量的指标必须恰好一行' },
    sample: { id: '', version: '' }, inputs: { start: '2026-10-01', end: '2026-11-01' }, inputs_hash: 'ih-1',
    expected,
    actual: actual ?? {
      m1: { value: 2, rendered: '2', rows: 1, masked: false, shape: 'scalar', shape_ok: true, findings: [], ok,
        ...(expected ? { expected: expected.metrics.m1 } : {}) },
      m2: { value: 500, rendered: '500', rows: 1, masked: false, shape: 'scalar', shape_ok: true, findings: [], ok,
        ...(expected ? { expected: expected.metrics.m2 } : {}) },
    },
    status, problems: status === 'observed' ? ['这次试跑没有独立期望：只记 observed，不能算验证通过'] : [],
    run_id: 'run-1', trial_id: 'tr-1', actor: 'alice', origin: 'trial', created_at: '2026-11-01T03:00:00+00:00',
    covers_current: covers, review,
  }
}

function summaryOf({ verdict, status, record = null, covers = true }) {
  return {
    verdict, status, covers_current: covers, record_id: record?.id ?? null, review: record?.review ?? null,
    counts: { total: record ? 1 : 0, covering: covers && record ? 1 : 0, matched: record?.status === 'matched' ? 1 : 0,
      mismatched: 0, observed: record?.status === 'observed' ? 1 : 0, unverified: 0, failed: 0 },
    reason: REASONS[verdict] ?? verdict,
    semantic_gaps: [], method: { version: 1, note: '原值比较、不做隐式舍入' },
  }
}

const REASONS = {
  none: '这份草稿还没有试跑留下的验证记录',
  stale: '现有的验证记录都不是对着当前定义/绑定跑的（定义、取数或绑定改过，或者现在读不到这份取数图）：'
    + '旧记录留着可追，但不能算覆盖这一版，请重新试跑',
  observed: '试跑跑了、也取了数，但没有独立期望：不能算验证通过',
  unverified: '有独立期望且对得上，但还没有认证审阅：样例匹配不等于正式批准',
  verified: '有独立期望的记录覆盖当前版本，并且有认证主体确认过',
  mismatched: '有独立期望，但这一次没对上',
  failed: '这一次试跑没有成功',
  rejected: '有覆盖当前版本的记录，但它被审阅驳回了',
}

const BASELINE = { status: 'known', artifact_id: 'bl-1', version: 1, content_hash: 'bl-1', card_id: 'card',
  workflow_id: 'wf-orders', accepted_revision: 1, accepted_at: '2026-11-01T02:00:00+00:00',
  metric_ids: ['m1', 'm2'], field_count: 4,
  coverage: { status: 'complete', metrics: {}, uncovered_metric_ids: [] } }

// ---------------------------------------------------------------- K11 / K12 的伪造：一次增量重导入、一份提案

const ACCEPT_STATES = ['update', 'conflict', 'new', 'manual']

/** 一格的三方（和 app/api/caliber_import._field_view 同一份形状）：b 是上次被接受的值、l 是现在的内容、r 是这次候选 */
function importField(path, state, over = {}) {
  const b = over.b ?? null
  const l = over.l ?? null
  const r = over.r ?? null
  return {
    path, state,
    b, l, r,
    b_present: over.bPresent ?? b != null, l_present: over.lPresent ?? l != null, r_present: over.rPresent ?? r != null,
    decided: over.decided ?? null,
    acceptable: ACCEPT_STATES.includes(state),
  }
}

/** 一条工作项（一条候选指标） */
function importItem(over = {}) {
  const metricId = over.metric_id ?? 'm1'
  return {
    item_id: over.item_id ?? 'it-1', seq: over.seq ?? 1, metric_id: metricId, candidate_id: over.candidate_id ?? 'c1',
    align_key: over.align_key ?? `k-${over.item_id ?? 'it-1'}`,
    name: over.name ?? '订单数', status: over.status ?? 'update',
    failed: over.failed ?? (over.status === 'failed'),
    align: over.align ?? { metric_id: metricId, mode: 'metric_id', evidence: ['ch-1'],
      reason: '候选带着和图上一致的稳定 metric_id，来源证据也对得上' },
    error: over.error ?? '', accepted: over.accepted ?? [], rejected: over.rejected ?? [],
    applied_revision: over.applied_revision ?? null, fields: over.fields ?? [],
  }
}

/** 一次导入的现状（GET …/imports、GET …/imports/{run_id}）：冲突那一列由逐字段的状态现算（和 _run_view 同一套） */
function importRun(over = {}) {
  const items = over.items ?? []
  const conflicts = over.conflicts ?? items.flatMap((item) => item.fields
    .filter((cell) => ['conflict', 'manual', 'missing'].includes(cell.state))
    .map((cell) => ({ metric_id: item.metric_id, item_id: item.item_id, seq: item.seq, name: item.name,
      path: cell.path, state: cell.state, b: cell.b, l: cell.l, r: cell.r })))
  return {
    run_id: over.run_id ?? 'im-1', draft_id: 'd-orders', workflow_id: 'wf-orders',
    status: over.status ?? 'succeeded', error: over.error ?? '', actor: 'alice', request_id: 'req-1',
    created_at: '2026-11-09T02:00:00+00:00',
    items_total: over.items_total ?? items.length, items_decided: over.items_decided ?? 0,
    source_snapshot: {}, visible_chunk_ids: [], items, conflicts,
  }
}

/** 接受之后的回：一次落到哪一版（和 accept_fields 的返回同一份形状） */
const acceptOut = (run, revision, over = {}) => ({ ...run, ...over, applied: true, replayed: false, revision })

/** 接受 / 拒绝要拦下来的写请求：每次记进 state.calls，检查随后核对请求体 */
const importCalls = (state, method, kind) =>
  state.calls.filter((call) => call.method === method && new RegExp(`/imports/[^/]+${kind}$`).test(call.path))

/** 一份 proposal（和 app/ledger/nlchange.Proposal.as_dict 同一份形状） */
function proposalOf(over = {}) {
  return {
    card: 'card', ready: over.ready ?? true,
    operations: over.operations ?? [
      { op: 'set', metric_id: 'm1', path: '/time_role', value: 'payment_time' },
      { op: 'set', metric_id: 'm2', path: '/time_role', value: 'payment_time' },
    ],
    metrics: over.metrics ?? ['m1', 'm2'],
    clarifications: over.clarifications ?? [],
    preview: over.preview ?? {
      applied: true, classification: 'definition', card: 'card', metrics: ['m1', 'm2'],
      operations: [], revision: null,
      definition_hash: { before: 'dh-1', after: 'dh-2', changed: true },
      validation: { invalidated: true, scope: 'card', metrics: ['m1', 'm2'], reason: '口径卡的内容变了', next: '重新试运行' },
      binding: bindingOf({ status: 'aligned', reason: ALIGNED_REASON, fix: 'none', synced: true }),
      preview: true, persisted: false,
    },
    ignored_claims: [], notes: [],
  }
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页 /ledger/drafts：/api/ 下认得的请求按 state 答，别的 GET 放行给开发服务器，不认得的写请求一律 409 并记进
 * stray（检查结束时必须是空的）。每次写请求都记进 state.calls，检查随后核对请求体。
 */
async function open(state, { me = ADMIN, w = 1280, h = 1100 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } })
  opened.add(ctx)
  // 帧和接口都在本机，但整套检查是并行跑的：给宽一点，机器的抖动不算这一项没过
  ctx.setDefaultTimeout(15_000)
  const page = await ctx.newPage()
  const errors = []
  const dialogs = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { dialogs.push(d.message()); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const p = decodeURIComponent(url.pathname.replace(/^\/api/, ''))
    let body = null
    try { body = req.postDataJSON() } catch { body = null }
    if (p === '/auth/me') return route.fulfill({ json: me })
    const out = await state.reply(req.method(), p, body, url)
    if (out) return route.fulfill({ status: out.status ?? 200, json: out.json ?? {} })
    if (req.method() === 'GET') return route.continue()
    state.stray.push(`${req.method()} ${p}`)
    return route.fulfill({ status: 409, json: { detail: '检查脚本不写库' } })
  })
  await page.goto(`${WEB}/ledger/drafts`, { waitUntil: 'domcontentloaded' })
  return { ctx, page, errors, dialogs }
}

/** 展开那份草稿的详情 */
async function expand(page, draft) {
  const toggle = page.locator(`[data-draft="${draft.id}"] [data-draft-toggle]`)
  await toggle.waitFor({ state: 'visible' })
  await toggle.click()
  await page.locator('[data-draft-detail]').waitFor({ state: 'visible' })
}

/** 页面上的业务块：规则、预览、回执、验证 */
const rulesPanel = (page) => page.locator('[data-draft-rules]')
const validationPanel = (page) => page.locator('[data-draft-validation]')
const textOf = async (locator) => (await locator.innerText().catch(() => '')).replace(/\s+/g, ' ')

/**
 * 这段文字有没有把结论说成「验证通过」。只看短语本身不够：`还不能算验证通过`、`不能算整卡验证通过` 都是**否定**的
 * 说法，用 includes('验证通过') 会被它们骗过（页面明明没通过，检查却绿了）。所以按「前面不是否定词」判；
 * 结论本身一律另按 data-draft-validation 的取值核（verified / unverified / observed…）。
 */
const claimsVerified = (text) => /(?<!(不能算|不算|不是|没有|未))验证通过/.test(text)

function makeState({ draft, validations, writes = {} }) {
  const state = { draft, validations, stray: [], calls: [], trial: null, ...writes }
  state.reply = async (method, p, body) => {
    state.calls.push({ method, path: p, body })
    if (method === 'GET' && p === '/ledger/drafts') return { json: { drafts: [state.draft] } }
    if (method === 'GET' && p === `/ledger/drafts/${state.draft.id}`) return { json: clone(state.draft) }
    if (method === 'GET' && p === `/ledger/drafts/${state.draft.id}/trial`) {
      return state.trial ? { json: state.trial } : { status: 404, json: { detail: '这份草稿还没有试运行' } }
    }
    if (method === 'GET' && p === `/ledger/drafts/${state.draft.id}/validations`) {
      return { json: { draft_id: state.draft.id, field_baseline: state.baseline ?? BASELINE,
        records: state.validations.records,
        summary: state.validations.summary, method: { version: 1, note: '原值比较' } } }
    }
    if (method === 'PATCH' && p === `/ledger/drafts/${state.draft.id}`) return state.patch(body)
    if (method === 'POST' && p === `/ledger/drafts/${state.draft.id}/trial`) return state.trial_(body)
    const review = p.match(new RegExp(`^/ledger/drafts/${state.draft.id}/validations/([^/]+)/review$`))
    if (method === 'POST' && review) return state.review(review[1], body)
    // K11 增量重导入：列表 / 建一次 / 一次导入的现状 / 人工对齐 / 接受 / 拒绝 / 恢复
    if (method === 'GET' && p === `/ledger/drafts/${state.draft.id}/imports`) return { json: { runs: state.imports } }
    if (method === 'POST' && p === `/ledger/drafts/${state.draft.id}/imports`) return state.createImport_(body)
    const importRun = p.match(/^\/ledger\/imports\/([^/]+)$/)
    if (method === 'GET' && importRun) return state.getImport(importRun[1])
    const alignPath = p.match(/^\/ledger\/imports\/([^/]+)\/items\/([^/]+)\/align$/)
    if (method === 'POST' && alignPath) return state.align(alignPath[1], alignPath[2], body)
    const acceptPath = p.match(/^\/ledger\/imports\/([^/]+)\/accept$/)
    if (method === 'POST' && acceptPath) return state.accept(acceptPath[1], body)
    const rejectPath = p.match(/^\/ledger\/imports\/([^/]+)\/reject$/)
    if (method === 'POST' && rejectPath) return state.reject(rejectPath[1], body)
    const resumePath = p.match(/^\/ledger\/imports\/([^/]+)\/resume$/)
    if (method === 'POST' && resumePath) return state.resume(resumePath[1])
    const refreshPath = p.match(/^\/ledger\/imports\/([^/]+)\/refresh$/)
    if (method === 'POST' && refreshPath) return state.refresh(refreshPath[1])
    // K12 一句话改口径：只读提案 / 确认落版
    if (method === 'POST' && p === `/ledger/drafts/${state.draft.id}/change-proposal`) return state.proposal(body)
    if (method === 'POST' && p === `/ledger/drafts/${state.draft.id}/nl-change`) return state.nl(body)
    if (method === 'GET' && p === '/kb/documents') return { json: [] }
    if (method === 'GET' && p === '/datasources/names') return { json: [] }
    if (method === 'GET' && p.startsWith('/runs')) return { json: [] }
    return null
  }
  // 缺省的写答复：不认得就 409（检查里显式给出各段要的答复）
  state.patch = () => ({ status: 409, json: { detail: '这一段没有安排字段操作的答复' } })
  state.trial_ = () => ({ status: 409, json: { detail: '这一段没有安排试运行的答复' } })
  state.review = () => ({ status: 409, json: { detail: '这一段没有安排审阅的答复' } })
  state.imports = []
  state.createImport_ = () => ({ status: 409, json: { detail: '这一段没有安排重新读取的答复' } })
  state.getImport = () => ({ status: 409, json: { detail: '这一段没有安排导入现状的答复' } })
  state.align = () => ({ status: 409, json: { detail: '这一段没有安排人工对齐的答复' } })
  state.accept = () => ({ status: 409, json: { detail: '这一段没有安排接受的答复' } })
  state.reject = () => ({ status: 409, json: { detail: '这一段没有安排拒绝的答复' } })
  state.resume = () => ({ status: 409, json: { detail: '这一段没有安排恢复的答复' } })
  state.refresh = () => ({ status: 409, json: { detail: '这一段没有安排重算三方的答复' } })
  state.proposal = () => ({ status: 409, json: { detail: '这一段没有安排提案的答复' } })
  state.nl = () => ({ status: 409, json: { detail: '这一段没有安排一句话落版的答复' } })
  return state
}

const patchBodies = (state) => state.calls.filter((c) => c.method === 'PATCH').map((c) => c.body)

/** 每个用过的 state（收尾时核对：兜底拦下的写请求必须是空的）和全局的原生对话框 */
const states = []
const dialogs = []
async function pageOn(state, opts) {
  states.push(state)
  const made = await open(state, opts)
  for (const d of made.dialogs) dialogs.push(d)
  return made
}

// ================================================================ 1 正向：改两项、一次提交

await section('正向：业务用户改统计时间口径和退款处理，看旧→新和影响指标，一次提交所有受影响指标', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [recordOf({ status: 'observed' })], summary: summaryOf({ verdict: 'observed', status: 'observed', record: recordOf({ status: 'observed' }) }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [
    { id: 'm1', name: '订单数', value: 3, rendered: '3' }, { id: 'm2', name: '订单金额', value: 900, rendered: '900' }],
    queries: [] }
  state.patch = (body) => {
    state.saved = body
    const live = draftOf()
    live.revision = 8
    live.version = 8
    live.binding = bindingOf({ status: 'aligned', reason: ALIGNED_REASON, fix: 'none', synced: true, metric_values:
      { m1: clone(NEW), m2: clone(NEW) }, current: clone(NEW) })
    live.metrics = live.metrics.map((metric) => ({ ...metric, ...NEW }))
    live.change = receiptOf({ revision: 8, operations: body.operations.map((op) => ({ ...op, changed: true })),
      binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = rulesPanel(page)
  check('业务规则一块画出来了', await panel.count() === 1)
  check('服务端说的同步状态照实写出来', await panel.getAttribute('data-draft-rules') === 'aligned',
    await panel.getAttribute('data-draft-rules'))
  const text = await textOf(panel)
  check('只写业务说法：不出现取数语句、节点编号、字段路径和 JSON',
    !/SELECT|\[\[|nodes\.|time_role|refund_rule|\{|\}/.test(text), text.slice(0, 200))
  check('两项业务写法都在，带给人看的说法', await panel.locator('[data-draft-rule-select="time_role"] option').count() === 3
    && (await textOf(panel.locator('[data-draft-rule-select="time_role"]'))).includes('按下单时间'),
  await textOf(panel.locator('[data-draft-rule-select="time_role"]')))
  check('还没改时预览说「还没有改动」',
    (await textOf(panel.locator('[data-draft-rule-affected]'))).includes('还没有改动'))
  check('保存按钮在没改动时是禁用的', await panel.locator('[data-draft-rule-save]').isDisabled())

  // 指标自己身上也写清按什么口径算：业务用户在这一页找得到、看得懂，不用去翻字段路径
  const metricCard = page.locator('[data-draft-metric="订单数"]')
  await metricCard.locator('summary').click()
  const business = await textOf(metricCard.locator('[data-draft-metric-business]'))
  check('指标自己写清按什么口径算，不出现内部字段名',
    business.includes('统计时间口径：按下单时间') && business.includes('退款处理：全部计入')
    && !/time_role|refund_rule/.test(business), business)

  // 改两项业务写法
  await panel.locator('[data-draft-rule-select="time_role"]').selectOption('payment_time')
  await panel.locator('[data-draft-rule-select="refund_rule"]').selectOption('exclude_full_refund')
  await until(async () => (await panel.locator('[data-draft-rule-change]').count()) === 4)
  check('预览列出会改到哪几条指标（2 条指标 × 2 项写法）',
    await panel.locator('[data-draft-rule-change]').count() === 4,
    String(await panel.locator('[data-draft-rule-change]').count()))
  check('预览写清条数', (await textOf(panel.locator('[data-draft-rule-affected]'))).includes('会改到 4 条指标'),
    await textOf(panel.locator('[data-draft-rule-affected]')))
  const preview = await textOf(panel.locator('[data-draft-rule-preview]'))
  check('预览写清「现在什么写法 → 保存后什么写法」',
    preview.includes('按下单时间') && preview.includes('按支付时间') && preview.includes('全部计入')
    && preview.includes('排除全额退款'), preview)
  check('预览里有两条指标的名字', preview.includes('订单数') && preview.includes('订单金额'), preview)

  await panel.locator('[data-draft-rule-save]').click()
  await page.locator('[data-draft-rule-result]').waitFor({ state: 'visible' })
  const saved = state.saved
  check('一次 PATCH 把两条指标的字段操作都提交了', saved?.operations?.length === 4,
    JSON.stringify(saved?.operations))
  check('全是 set，按指标身份和业务字段定位，值是新写法',
    saved.operations.every((op) => op.op === 'set')
    && new Set(saved.operations.map((op) => `${op.metric_id}${op.path}`)).size === 4
    && saved.operations.filter((op) => op.path === '/time_role').every((op) => op.value === 'payment_time'),
    JSON.stringify(saved.operations))
  check('带上读到的基准版本', saved.expected_revision === 7, String(saved.expected_revision))
  check('指到这张口径卡（不给「第一张」留余地）', saved.target?.card_id === 'card' && saved.target?.workflow_id === 'wf-orders',
    JSON.stringify(saved.target))
  check('带上幂等键', typeof saved.request_id === 'string' && saved.request_id.length > 0, String(saved.request_id))
  check('不走整份换指标那条路（没有 metrics）', saved.metrics === undefined)

  const result = await textOf(page.locator('[data-draft-rule-result]'))
  check('回执写清新版本', result.includes('已保存成 v8'), result)
  check('回执写清定义和取数已经在同一个版本里改过来了', result.includes('定义和取数已经在同一个版本里改过来了'), result)
  check('回执写清旧验证不再覆盖这一版、下一步重新试运行',
    result.includes('旧的试运行和验证不再算覆盖这一版') && result.includes('重新试运行'), result)
  check('回执按服务端的答复标成真写进去了',
    await page.locator('[data-draft-rule-result]').getAttribute('data-draft-rule-result') === 'applied')
  check('保存后表单回到「没有改动」', (await textOf(panel.locator('[data-draft-rule-affected]'))).includes('还没有改动'))
  check('保存后出现「撤销这次修改」', await panel.locator('[data-draft-rule-undo]').count() === 1)
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 2 撤销

await section('撤销：按保存前的值构造逆操作，带当前版本和新请求键再存一版，不顺一版整份覆盖', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  let revision = 7
  const seen = []
  state.patch = (body) => {
    seen.push(body)
    revision += 1
    const live = draftOf()
    live.revision = revision
    live.version = revision
    const values = seen.length === 1 ? { m1: { ...clone(OLD), time_role: 'payment_time' }, m2: { ...clone(OLD), time_role: 'payment_time' } } : { m1: clone(OLD), m2: clone(OLD) }
    live.metrics = live.metrics.map((metric) => ({ ...metric, time_role: values[metric.id].time_role }))
    live.binding = bindingOf({ status: 'aligned', reason: ALIGNED_REASON, fix: 'none', synced: true,
      metric_values: values, current: { time_role: values.m1.time_role, refund_rule: 'include_all' } })
    live.change = receiptOf({ revision, metrics: ['m1', 'm2'],
      operations: body.operations.map((op) => ({ ...op, changed: true })), binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = rulesPanel(page)
  await panel.locator('[data-draft-rule-select="time_role"]').selectOption('payment_time')
  await until(async () => (await panel.locator('[data-draft-rule-change]').count()) === 2)
  await panel.locator('[data-draft-rule-save]').click()
  await until(async () => (await panel.locator('[data-draft-rule-undo]').count()) === 1)
  check('保存后出现撤销按钮，写清撤销几处改动',
    (await textOf(panel.locator('[data-draft-rule-undo]'))).includes('撤销这次修改（回到 2 处改动之前的值）'),
    await textOf(panel.locator('[data-draft-rule-undo]')))
  check('撤销按钮上写明撤销也是新增一版',
    (await panel.locator('[data-draft-rule-undo]').getAttribute('title') ?? '').includes('新增一版'))

  await panel.locator('[data-draft-rule-undo]').click()
  await until(async () => seen.length === 2)
  const undo = seen[1]
  check('撤销用保存前的值构造逆操作（两条指标都设回按下单时间）',
    undo.operations.length === 2 && undo.operations.every((op) => op.op === 'set' && op.path === '/time_role'
      && op.value === 'order_time'), JSON.stringify(undo.operations))
  check('撤销以当前版本为基准（保存后已经是 v8）', undo.expected_revision === 8, String(undo.expected_revision))
  check('撤销用一个**新的**请求键', typeof undo.request_id === 'string' && undo.request_id
    && undo.request_id !== seen[0].request_id, `${seen[0].request_id} / ${undo.request_id}`)
  check('撤销不走整份覆盖（没有 metrics）', undo.metrics === undefined)
  // 收到 PATCH 不等于响应已渲染：等新版本的回执，避免页面较忙时读到上一版。
  await until(async () => (await textOf(page.locator('[data-draft-rule-result]'))).includes('已保存成 v9'))
  const after = await textOf(page.locator('[data-draft-rule-result]'))
  check('撤销后回执写清新版本', after.includes('已保存成 v9'), after)
  check('撤销记在这次会话里（提示已撤销）', await page.locator('[data-draft-rule-undo]').count() === 0)
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 3 冲突：基准版本过期

await section('冲突：基准版本过期时保留写法选择、不自动重发，刷新详情后由人显式再点一次', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const seen = []
  state.patch = (body) => {
    seen.push(body)
    if (seen.length === 1) {
      // 另一个页面刚存过一版：服务端现在已经是 v8，这次写不进去
      state.draft.revision = 8
      state.draft.version = 8
      return { status: 409, json: { detail: '这份内容基于 v7，现在已经是 v8：有人先改过。你刚才的修改还留着，'
        + '没有替你重发，确认后可以基于最新的一版再提交一次', code: 'stale_revision', current_version: 8 } }
    }
    const live = draftOf()
    live.revision = 9
    live.version = 9
    live.binding = bindingOf({ status: 'aligned', reason: ALIGNED_REASON, fix: 'none', synced: true,
      metric_values: { m1: clone(NEW), m2: clone(NEW) }, current: clone(NEW) })
    live.metrics = live.metrics.map((metric) => ({ ...metric, ...NEW }))
    live.change = receiptOf({ revision: 9, operations: body.operations.map((op) => ({ ...op, changed: true })),
      binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = rulesPanel(page)
  await panel.locator('[data-draft-rule-select="time_role"]').selectOption('payment_time')
  await panel.locator('[data-draft-rule-select="refund_rule"]').selectOption('exclude_full_refund')
  await until(async () => (await panel.locator('[data-draft-rule-change]').count()) === 4)
  await page.locator('[data-draft-rule-save]').click()
  await page.locator('[data-draft-rule-stale]').waitFor({ state: 'visible' })
  const stale = await textOf(page.locator('[data-draft-rule-stale]'))
  check('写清你基于哪一版、服务端现在是哪一版',
    stale.includes('你基于 v7') && stale.includes('服务端现在是 v8'), stale)
  check('说清没有替你重发、选择还在', stale.includes('选择还在') || stale.includes('写法还在'), stale)
  check('没有自动重发（只发了那一次 PATCH）', seen.length === 1, `${seen.length} 次`)
  check('详情被重新读了一次（拿到新的基准版本）',
    state.calls.filter((c) => c.method === 'GET' && c.path === '/ledger/drafts/d-orders').length >= 2)
  check('页面上选好的写法一个字没丢',
    await panel.locator('[data-draft-rule-select="time_role"]').inputValue() === 'payment_time'
    && await panel.locator('[data-draft-rule-select="refund_rule"]').inputValue() === 'exclude_full_refund')
  check('预览还在（选择没被清掉）', await panel.locator('[data-draft-rule-change]').count() === 4,
    String(await panel.locator('[data-draft-rule-change]').count()))

  await page.locator('[data-draft-rule-save]').click()
  await until(async () => seen.length === 2)
  check('第二次用的是刷新后的版本（v8）', seen[1].expected_revision === 8, String(seen[1].expected_revision))
  check('同内容重试用同一个请求键（服务端认得出是同一件事）',
    seen[0].request_id === seen[1].request_id, `${seen[0].request_id} / ${seen[1].request_id}`)
  await page.locator('[data-draft-rule-result]').waitFor({ state: 'visible' })
  check('第二次存进去了（回执写清新版本）',
    (await textOf(page.locator('[data-draft-rule-result]'))).includes('已保存成 v9'),
    await textOf(page.locator('[data-draft-rule-result]')))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 4 复杂：同步不了

await section('复杂：同步不了的改动照实说原因和技术待办，不写「已同步」', async () => {
  const state = makeState({
    draft: draftOf({ binding: { status: 'unsupported', reason: UNSUPPORTED_REASON, fix: 'author' } }),
    validations: { records: [], summary: summaryOf({ verdict: 'stale', status: 'observed', covers: false }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.patch = (body) => {
    const live = draftOf({ binding: { status: 'unsupported', reason: UNSUPPORTED_REASON, fix: 'author' } })
    live.revision = 8
    live.version = 8
    live.metrics = live.metrics.map((metric) => ({ ...metric, time_role: 'payment_time' }))
    live.binding = bindingOf({ status: 'unsupported', reason: UNSUPPORTED_REASON, fix: 'author', synced: false,
      metric_values: { m1: { ...clone(OLD), time_role: 'payment_time' }, m2: { ...clone(OLD), time_role: 'payment_time' } } })
    live.change = receiptOf({ revision: 8, metrics: ['m1', 'm2'],
      operations: body.operations.map((op) => ({ ...op, changed: true })), binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = rulesPanel(page)
  check('同步不了的状态照实写在面板上', await panel.getAttribute('data-draft-rules') === 'unsupported',
    await panel.getAttribute('data-draft-rules'))
  const reason = await textOf(panel)
  check('给出具体原因（这条取数没有这个业务字段的落点）', reason.includes('没有「统计时间口径」的落点'), reason.slice(0, 240))
  check('给出技术待办', reason.includes('技术待办') && reason.includes('要作者侧补或改受控绑定'), reason.slice(0, 240))
  await panel.locator('[data-draft-rule-select="time_role"]').selectOption('payment_time')
  await until(async () => (await panel.locator('[data-draft-rule-change]').count()) === 2)
  check('复杂资产照样能提交（定义照常保存，取数不动）',
    !(await panel.locator('[data-draft-rule-save]').isDisabled()))
  await panel.locator('[data-draft-rule-save]').click()
  await page.locator('[data-draft-rule-result]').waitFor({ state: 'visible' })
  const result = await textOf(page.locator('[data-draft-rule-result]'))
  check('回执说「定义已经存下来了、取数一个字没动」', result.includes('定义已经存下来了，取数一个字没动'), result)
  check('回执不说「已同步」', !result.includes('定义和取数已经在同一个版本里改过来了'), result)
  check('回执带上技术待办', await page.locator('[data-draft-rule-fix]').count() === 1,
    await textOf(page.locator('[data-draft-rule-fix]')))
  check('回执里绑定状态照实是 unsupported',
    await page.locator('[data-draft-rule-binding]').getAttribute('data-draft-rule-binding') === 'unsupported',
    await page.locator('[data-draft-rule-binding]').getAttribute('data-draft-rule-binding'))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 5 独立样例、审阅与「不算通过」

await section('验证：独立期望试运行、样例对得上、审阅确认，且没有期望时如实说只记了试跑结果', async () => {
  const observed = recordOf({ status: 'observed' })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [observed], summary: summaryOf({ verdict: 'observed', status: 'observed', record: observed }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [
    { id: 'm1', name: '订单数', value: 3, rendered: '3' }, { id: 'm2', name: '订单金额', value: 900, rendered: '900' }],
    queries: [] }
  let matched = null
  state.trial_ = (body) => {
    state.trialBody = body
    matched = recordOf({ id: 'vr-2', status: 'matched', ok: true,
      expected: { source: 'fixture', metrics: { m1: 2, m2: 500 }, hash: 'xh-1', note: null } })
    state.draft = { ...draftOf(), trial: { run_id: 'run-2', status: 'succeeded', version: 7 } }
    state.validations = { records: [matched], summary: summaryOf({ verdict: 'unverified', status: 'matched', record: matched }) }
    return { json: { run_id: 'run-2', status: 'succeeded', metrics: [], queries: [] } }
  }
  state.review = (recordId, body) => {
    state.reviewBody = { recordId, body }
    const review = { id: 'rv-1', draft_id: 'd-orders', record_id: recordId, workflow_id: 'wf-orders',
      decision: body.decision, actor: 'alice', verified: true, scope: ['m1', 'm2'],
      basis: { version: 7, definition_hash: 'dh-1' }, note: body.note ?? null,
      created_at: '2026-11-01T04:00:00+00:00', covers_current: true }
    matched = { ...matched, review }
    state.validations = { records: [matched],
      summary: { ...summaryOf({ verdict: body.decision === 'confirmed' ? 'verified' : 'rejected', status: 'matched', record: matched }),
        review,
        // 服务端按确认范围算出来的那两份：确认到了全部指标，所以是整卡通过
        confirmed: body.decision === 'confirmed' ? ['m1', 'm2'] : [],
        unconfirmed: body.decision === 'confirmed' ? [] : ['m1', 'm2'] } }
    state.draft = { ...state.draft, validation: state.validations.summary }
    return { json: { ok: true, draft_id: 'd-orders', record: matched } }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = validationPanel(page)
  await panel.waitFor({ state: 'visible' })
  check('验证一块画出来了', await panel.count() === 1)
  check('没有独立期望的记录如实标成「只记了试运行结果」',
    await panel.getAttribute('data-draft-validation') === 'observed',
    await panel.getAttribute('data-draft-validation'))
  const first = page.locator('[data-draft-validation-record="vr-1"]')
  check('那条记录写清没有独立期望、不能算验证通过',
    (await textOf(first)).includes('这条没有独立期望：只记试运行结果，不能算验证通过'), await textOf(first))
  check('那一条不写「对得上」', !(await textOf(first.locator('[data-record-actual]').first())).includes('对得上'),
    await textOf(first.locator('[data-record-actual]').first()))
  check('总结论不是「验证通过」',
    await panel.getAttribute('data-draft-validation') === 'observed'
    && !claimsVerified(await textOf(panel.locator('[data-draft-validation-verdict]'))),
    await panel.getAttribute('data-draft-validation'))
  check('字段基线照实写', (await textOf(panel.locator('[data-draft-baseline]'))).includes('有可还原的导入基线'),
    await textOf(panel.locator('[data-draft-baseline]')))

  // 带独立期望试运行
  await panel.locator('[data-draft-expect] summary').click()
  check('要逐条给全卡指标才让提交（还没填时禁用 + 写明）',
    await panel.locator('[data-draft-expect-submit]').isDisabled()
    && await panel.locator('[data-draft-expect-needall]').count() === 1)
  await panel.locator('[data-draft-expect-input="m1"]').fill('2')
  await panel.locator('[data-draft-expect-input="m2"]').fill('500')
  await panel.locator('[data-draft-expect-source]').selectOption('fixture')
  await until(async () => !(await panel.locator('[data-draft-expect-submit]').isDisabled()))
  check('期望填全了才让提交', !(await panel.locator('[data-draft-expect-submit]').isDisabled()))
  // 入口字段是必填：先填上再试（缺参数的服务端不会发查询）
  await page.locator('[data-draft-entry-input="start"]').fill('2026-10-01')
  await page.locator('[data-draft-entry-input="end"]').fill('2026-11-01')
  await panel.locator('[data-draft-expect-submit]').click()
  await until(async () => !!state.trialBody)
  check('试运行带上独立期望（逐条给全卡指标）',
    state.trialBody?.expect?.metrics?.m1 === 2 && state.trialBody?.expect?.metrics?.m2 === 500
    && state.trialBody?.expect?.source === 'fixture', JSON.stringify(state.trialBody?.expect))
  check('试运行只发试运行请求，入口字段的写法不创建定义版本（没有 PATCH）',
    patchBodies(state).length === 0, patchBodies(state).length + ' 次 PATCH')

  await page.locator('[data-draft-validation-record="vr-2"]').waitFor({ state: 'visible' })
  const second = page.locator('[data-draft-validation-record="vr-2"]')
  check('新记录是「和独立期望对得上」', await second.getAttribute('data-record-status') === 'matched',
    await second.getAttribute('data-record-status'))
  const secondText = await textOf(second)
  check('记录里写清期望来自哪里、期望值和实际值',
    secondText.includes('固定夹具') && secondText.includes('2') && secondText.includes('500'), secondText)
  check('实际值那一条判「对得上」',
    await second.locator('[data-record-actual="m1"]').getAttribute('data-record-actual-ok') === 'true',
    await second.locator('[data-record-actual="m1"]').getAttribute('data-record-actual-ok'))
  check('样例对得上但还没审阅：总结论是「还不能算验证通过」（按 data 属性核，不算通过）',
    await panel.getAttribute('data-draft-validation') === 'unverified'
    && (await textOf(panel.locator('[data-draft-validation-verdict]'))) === '还不能算验证通过',
    await panel.getAttribute('data-draft-validation'))
  check('并写明样例匹配不等于正式批准',
    (await textOf(panel.locator('[data-draft-validation-reason]'))).includes('样例匹配不等于正式批准'),
    await textOf(panel.locator('[data-draft-validation-reason]')))

  // 审阅：只调真接口，请求体里没有 actor
  await panel.locator('[data-draft-review-note="vr-2"]').fill('对过业务口径和样例来源')
  await second.locator('[data-draft-review="confirmed"]').click()
  await until(async () => !!state.reviewBody)
  check('审阅调的是那份记录的审阅接口',
    state.reviewBody?.recordId === 'vr-2' && state.reviewBody?.body?.decision === 'confirmed',
    JSON.stringify(state.reviewBody))
  check('请求体里没有审批人（代写不了）', !('actor' in (state.reviewBody?.body ?? {})),
    JSON.stringify(state.reviewBody?.body))
  await until(async () => (await textOf(second.locator('[data-record-review-line]'))).includes('alice'))
  const reviewLine = await textOf(second.locator('[data-record-review-line]'))
  check('审阅结果照服务端交回的说（谁、认证状态、范围）',
    reviewLine.includes('确认') && reviewLine.includes('由 alice') && reviewLine.includes('订单数'), reviewLine)
  // 结论一律按 data-draft-validation 核：文本里出现「验证通过」四个字不算数（否定说法里也有这四个字）
  await until(async () => (await panel.getAttribute('data-draft-validation')) === 'verified')
  check('有独立期望 + 认证审阅之后结论才是「验证通过」',
    await panel.getAttribute('data-draft-validation') === 'verified'
    && (await textOf(panel.locator('[data-draft-validation-verdict]'))) === '验证通过',
    await panel.getAttribute('data-draft-validation'))
  check('验证一块里也不出现取数语句和节点编号',
    !/SELECT|\[\[|nodes\.|data\.config/.test(await textOf(panel)), (await textOf(panel)).slice(0, 200))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 6 只读资产

await section('只读：能力描述为空、认不出唯一口径卡、没有版本号的资产不开表单', async () => {
  const unbound = draftOf({ binding: { status: 'unbound', reason: '这张卡没有指标声明统计时间口径和退款规则：'
    + '按旧资产处理，不判绑定同步', fix: 'none', metric_values: {}, current: {} } })
  const state = makeState({
    draft: unbound,
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const { page, errors } = await pageOn(state)
  await expand(page, unbound)
  const panel = rulesPanel(page)
  check('旧资产照实标 unbound', await panel.getAttribute('data-draft-rules') === 'unbound',
    await panel.getAttribute('data-draft-rules'))
  check('不开表单（一个下拉都没有）', await panel.locator('[data-draft-rule-select]').count() === 0)
  check('说清为什么不能改',
    (await textOf(panel.locator('[data-draft-rules-readonly]'))).includes('没有声明统计时间口径和退款处理'),
    await textOf(panel.locator('[data-draft-rules-readonly]')))
  check('旧资产不会因为少一个字段就报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('只读：没有取数图的老草稿不画业务规则一块', async () => {
  const bare = { id: 'd-bare', title: '老草稿', status: 'path_pending', stage: 'review', metric_count: 1,
    question_count: 0, workflow_id: null, workflow_name: null, created_at: '2026-10-01T02:00:00+00:00', error: null,
    metrics: [{ id: 'm1', name: '订单数' }], revision: null, card_id: null, cards: [] }
  const state = makeState({
    draft: bare,
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: '', status: 'needs_input', metrics: [], queries: [] }
  const { page, errors } = await pageOn(state)
  await expand(page, bare)
  check('老草稿上没有业务规则一块', await rulesPanel(page).count() === 0)
  check('验证一块照常给出（还没有记录）',
    (await textOf(validationPanel(page))).includes('还没有验证记录'), await textOf(validationPanel(page)))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 7 查询参数

await section('查询参数：入口字段的变化只发试运行，不发 PATCH、不新增定义版本', async () => {
  const state = makeState({
    draft: draftOf(), validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.trial_ = (body) => {
    state.trialBody = body
    return { json: { run_id: 'run-2', status: 'succeeded', metrics: [], queries: [] } }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-entry-input="start"]').fill('2026-10-01')
  await page.locator('[data-draft-entry-input="end"]').fill('2026-11-01')
  await page.locator('[data-draft-trial-run-button]').click()
  await until(async () => !!state.trialBody)
  check('只发试运行，带填好的入口字段',
    state.trialBody?.inputs?.start === '2026-10-01' && state.trialBody?.inputs?.end === '2026-11-01',
    JSON.stringify(state.trialBody?.inputs))
  check('入口字段变化不触发字段操作（没有 PATCH）', patchBodies(state).length === 0,
    JSON.stringify(patchBodies(state)))
  check('试运行请求里没有期望（这一条只记试运行结果）', state.trialBody?.expect === undefined)
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 8 契约防御

await section('契约防御：试跑响应里的 validation 只有记录那一段时，不当成质量通过，覆盖判断未知照实写', async () => {
  const own = recordOf({ id: 'vr-9', status: 'matched', ok: true,
    expected: { source: 'human_confirmed', metrics: { m1: 2, m2: 500 }, hash: 'xh-9' } })
  own.covers_current = null                     // 读数缺这一段：判不了，不许当成覆盖
  const state = makeState({
    draft: draftOf(),
    // 总结论照着这条记录说；覆盖判断也是未知
    validations: { records: [own],
      summary: { ...summaryOf({ verdict: 'unverified', status: 'matched', record: own }), covers_current: null } },
  })
  // 成功分支、但 validation 里只有记录那一段：没有 status、没有 checks
  state.trial = { run_id: 'run-9', status: 'succeeded', metrics: [{ id: 'm1', name: '订单数', value: 2, rendered: '2' }],
    queries: [], validation: { record: own, covers_current: null,
      note: '试跑结果只说明这一次跑成什么样；它还算不算覆盖当前定义/绑定看 covers_current' } }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const trialBlock = page.locator('[data-draft-trial="succeeded"]')
  const trialText = await textOf(trialBlock)
  check('读不到质量状态时不写「质量检查通过」', !trialText.includes('质量检查通过'), trialText)
  check('照实说质量结论还没有', !trialText.includes('质量检查未通过') && trialText.includes('尚未完成质量验证'), trialText)
  await trialBlock.locator('[data-draft-trial-record]').waitFor({ state: 'visible' })
  check('试运行块里只画这一次的那条记录',
    await trialBlock.locator('[data-draft-trial-record="matched"]').count() === 1,
    await trialBlock.locator('[data-draft-trial-record]').count() + ' 条')
  check('覆盖判断未知就写未知，不当成覆盖',
    await trialBlock.locator('[data-draft-trial-record]').getAttribute('data-trial-record-covers') === 'unknown'
    && (await textOf(trialBlock.locator('[data-draft-trial-record]'))).includes('覆盖判断未知'),
    await textOf(trialBlock.locator('[data-draft-trial-record]')))
  check('试运行块里不写「验证通过」', !claimsVerified(trialText), trialText)
  check('那一条写明独立期望来自哪里',
    (await textOf(trialBlock.locator('[data-draft-trial-record]'))).includes('独立期望：人工确认'),
    await textOf(trialBlock.locator('[data-draft-trial-record]')))
  const panel = validationPanel(page)
  check('面板上的覆盖判断也写未知（只有明确说覆盖才算覆盖）',
    await page.locator('[data-record-covers]').first().getAttribute('data-record-covers') === 'unknown')
  check('这条记录不写「覆盖当前版本」（按 data 属性核，不看否定说法里的四个字）',
    await panel.locator('[data-draft-validation-record="vr-9"] [data-record-covers]')
      .getAttribute('data-record-covers') === 'unknown',
    await panel.locator('[data-draft-validation-record="vr-9"] [data-record-covers]').getAttribute('data-record-covers'))
  check('结论说清是按哪一条记录下的',
    (await textOf(panel.locator('[data-draft-validation-basis]'))).includes('结论按')
    && (await textOf(panel.locator('[data-draft-validation-basis]'))).includes('和独立期望对得上'),
  await textOf(panel.locator('[data-draft-validation-basis]')))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('契约防御：试跑还在跑又没有本次记录时只写等本次验证，不拿旧记录充当本次', async () => {
  const old = recordOf({ id: 'vr-old', status: 'observed', covers: false })
  const state = makeState({
    draft: draftOf({ draft: { trial: { run_id: 'run-10', status: 'running', version: 7 } } }),
    validations: { records: [old], summary: summaryOf({ verdict: 'stale', status: 'observed', record: old, covers: false }) },
  })
  state.trial = { run_id: 'run-10', status: 'running', metrics: [], queries: [],
    validation: { record: null, covers_current: false,
      note: '这次试跑还没有留下验证记录：缺证据不能算验证通过' } }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const trialBlock = page.locator('[data-draft-trial="running"]')
  await trialBlock.locator('[data-draft-trial-waiting]').waitFor({ state: 'visible' })
  check('写明正在等本次验证记录',
    (await textOf(trialBlock.locator('[data-draft-trial-waiting]'))).includes('正在等本次的验证记录'),
    await textOf(trialBlock.locator('[data-draft-trial-waiting]')))
  check('试运行块里没有画出任何验证记录（旧记录不充当本次）',
    await trialBlock.locator('[data-draft-trial-record]').count() === 0)
  const trialText = await textOf(trialBlock)
  check('试运行块里不写旧记录的状态、也不写通过',
    !trialText.includes('只记了试运行结果') && !claimsVerified(trialText), trialText)
  const panel = validationPanel(page)
  check('旧记录仍然只摆在自己的位置（面板里，标不覆盖当前版本）',
    await panel.locator('[data-draft-validation-record="vr-old"]').count() === 1
    && await page.locator('[data-record-covers="no"]').count() >= 1)
  check('结论按旧记录说是过期，不写成通过',
    await panel.getAttribute('data-draft-validation') === 'stale'
    && (await textOf(panel.locator('[data-draft-validation-verdict]'))) === '现有记录不覆盖这一版',
    await panel.getAttribute('data-draft-validation'))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('契约防御：部分确认不能写成整卡通过（按范围收回结论）', async () => {
  const partialReview = { id: 'rv-3', draft_id: 'd-orders', record_id: 'vr-3', workflow_id: 'wf-orders',
    decision: 'confirmed', actor: 'alice', verified: true, scope: ['m1'], basis: { version: 7 },
    note: '只确认了订单数这一条', created_at: '2026-11-01T05:00:00+00:00', covers_current: true }
  const record = recordOf({ id: 'vr-3', status: 'matched', ok: true,
    expected: { source: 'fixture', metrics: { m1: 2, m2: 500 }, hash: 'xh-3' }, review: partialReview })
  const state = makeState({
    draft: draftOf(),
    // 服务端（还没改好之前）照样回 verified：页面要按范围把它收回来
    validations: { records: [record], summary: summaryOf({ verdict: 'verified', status: 'matched', record }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = validationPanel(page)
  check('部分确认时面板不标成验证通过', await panel.getAttribute('data-draft-validation') === 'unverified',
    await panel.getAttribute('data-draft-validation'))
  check('按范围收回了结论', await panel.getAttribute('data-validation-partial') === 'recalled',
    await panel.getAttribute('data-validation-partial'))
  const verdict = await textOf(panel.locator('[data-draft-validation-verdict]'))
  check('结论写「还不能算验证通过」（data 属性也不是 verified）',
    verdict === '还不能算验证通过' && await panel.getAttribute('data-draft-validation') === 'unverified', verdict)
  const note = await textOf(panel.locator('[data-draft-validation-partial]'))
  check('写清盖住了哪条、没盖住哪条、一共几条',
    note.includes('订单数') && note.includes('订单金额') && note.includes('2 条指标')
    && note.includes('不能算整卡验证通过'), note)
  const recordText = await textOf(page.locator('[data-draft-validation-record="vr-3"]'))
  check('记录上写明确认范围（订单数）并指出没盖住全部指标',
    recordText.includes('确认范围：订单数') && recordText.includes('没有盖住这条记录的全部指标'), recordText)
  check('审阅自己的覆盖判断照实写',
    await page.locator('[data-record-review-covers]').first().getAttribute('data-record-review-covers') === 'yes')
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))

  // 服务端已经按确认范围收回结论（现在这一版就是这样）：名字用服务端列出来的那份，页面上也不写通过
  const fixedSummary = { ...summaryOf({ verdict: 'unverified', status: 'matched', record }), confirmed: ['m1'],
    unconfirmed: ['m2'] }
  const second = makeState({ draft: draftOf(), validations: { records: [record], summary: fixedSummary } })
  second.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const secondPage = await pageOn(second)
  await expand(secondPage.page, second.draft)
  const fixedPanel = validationPanel(secondPage.page)
  check('服务端按范围收回时页面上也不是验证通过（标成范围没盖住）',
    await fixedPanel.getAttribute('data-validation-partial') === 'scope',
    await fixedPanel.getAttribute('data-validation-partial'))
  check('这一页也不写成整卡通过',
    await fixedPanel.getAttribute('data-draft-validation') === 'unverified'
    && (await textOf(fixedPanel.locator('[data-draft-validation-verdict]'))) === '还不能算验证通过',
    await fixedPanel.getAttribute('data-draft-validation'))
  const fixedNote = await textOf(fixedPanel.locator('[data-draft-validation-partial]'))
  check('按服务端列出的未确认指标说话',
    fixedNote.includes('订单数') && fixedNote.includes('订单金额'), fixedNote)
  check('这一页没有报错', secondPage.errors.length === 0, secondPage.errors.slice(0, 2).join('；'))
})

await section('契约防御：审阅的覆盖判断读不到时不算覆盖', async () => {
  const review = { id: 'rv-4', draft_id: 'd-orders', record_id: 'vr-4', workflow_id: 'wf-orders',
    decision: 'confirmed', actor: 'alice', verified: true, scope: ['m1', 'm2'], basis: { version: 7 },
    note: null, created_at: '2026-11-01T06:00:00+00:00', covers_current: null }
  const record = recordOf({ id: 'vr-4', status: 'matched', ok: true,
    expected: { source: 'fixture', metrics: { m1: 2, m2: 500 }, hash: 'xh-4' }, review })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [record], summary: { ...summaryOf({ verdict: 'unverified', status: 'matched', record }), review } },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  check('审阅的覆盖判断未知就写未知',
    await page.locator('[data-record-review-covers]').first().getAttribute('data-record-review-covers') === 'unknown'
    && (await textOf(page.locator('[data-record-review-covers]').first())).includes('覆盖判断未知'),
    await textOf(page.locator('[data-record-review-covers]').first()))
  check('不写成「这次审阅覆盖当前版本」',
    await page.locator('[data-draft-validation-record="vr-4"] [data-record-review-covers]')
      .getAttribute('data-record-review-covers') !== 'yes',
    await page.locator('[data-draft-validation-record="vr-4"] [data-record-review-covers]').getAttribute('data-record-review-covers'))
  check('全程页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 9 增量重导入：三方、接受、漏项

await section('重导入：三方的值逐格摆出来，接受是无冲突更新里唯一能接受的格子，漏提取不当删除也不推进基线', async () => {
  const item1 = importItem({
    fields: [
      importField('/definition/text', 'update', { b: '按已付款订单统计', l: '按已付款订单统计', r: '按已付款订单统计（退款另计）' }),
      importField('/unit', 'missing', { b: '元', l: '元' }),
    ],
  })
  const run = importRun({ items: [item1] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.baseline = { ...BASELINE, coverage: { status: 'partial', metrics: { m1: { covered: 1, expected: 2, missing: ['/unit'] } }, uncovered_metric_ids: ['m2'] } }
  state.imports = [run]
  state.createImport_ = () => ({ json: importRun({ run_id: 'im-2', status: 'queued', items: [] }) })
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    const done = importItem({
      accepted: ['/definition/text'], status: 'accepted', applied_revision: 8,
      fields: [
        importField('/definition/text', 'unchanged', { b: '按已付款订单统计（退款另计）', l: '按已付款订单统计（退款另计）',
          r: '按已付款订单统计（退款另计）', decided: 'accepted' }),
        importField('/unit', 'missing', { b: '元', l: '元' }),
      ],
    })
    return { json: acceptOut(importRun({ items: [done], items_decided: 1 }), 8) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-draft-imports]')
  await until(async () => (await panel.getAttribute('data-draft-imports')) === 'succeeded')
  check('文档更新一块画出来了，最近那一次导入读完了就摆出明细',
    await panel.getAttribute('data-draft-imports') === 'succeeded', await panel.getAttribute('data-draft-imports'))
  check('没接着后端写：导入现状是按列表接口答的',
    state.calls.filter((c) => c.method === 'GET' && c.path === '/ledger/drafts/d-orders/imports').length === 1)

  // 打开这一条记录看明细
  await page.locator('[data-draft-import-run="im-1"]').click()
  await page.locator('[data-draft-import-detail]').waitFor({ state: 'visible' })
  const field = page.locator('[data-draft-import-field="it-1:/definition/text"]')
  check('逐格写出三方的值（上次被接受的、现在的内容、这次文档里的说法）',
    await field.locator('[data-draft-import-value="b"]').count() === 1
    && await field.locator('[data-draft-import-value="l"]').count() === 1
    && await field.locator('[data-draft-import-value="r"]').count() === 1,
    await textOf(field))
  const fieldText = await textOf(field)
  check('三方各带给人看的说法，B / L / R 不写成缩写',
    fieldText.includes('上次被接受的导入值') && fieldText.includes('现在的内容') && fieldText.includes('这次文档里的说法'),
    fieldText)
  check('无冲突更新写清状态和能不能接受',
    await field.getAttribute('data-import-field-state') === 'update' && fieldText.includes('无冲突更新')
    && fieldText.includes('上次接受之后没人动过'), fieldText)

  // 漏提取的那一格：明确说不当删除，而且接受是禁用的
  const missing = page.locator('[data-draft-import-field="it-1:/unit"]')
  const missingText = await textOf(missing)
  check('漏提取的那一格不当删除：写清这次没提取到、现在的内容原样留着',
    await missing.getAttribute('data-import-field-state') === 'missing'
    && missingText.includes('这次没提取到') && missingText.includes('漏提取不当删除')
    && !missingText.includes('删除这一格'), missingText)
  check('漏提取的格子不能接受（接受是禁用的）',
    await missing.locator('[data-draft-import-pick-accept="it-1:/unit"]').isDisabled())
  check('这一条上只有 1 格能接受，一次收掉的按钮按它算',
    (await textOf(page.locator('[data-draft-import-accept-clean="it-1"]'))).includes('无冲突的 1 格'),
    await textOf(page.locator('[data-draft-import-accept-clean="it-1"]')))

  // 没选格子时接受按钮是禁用的
  check('没选格子时「接受选中的 0 格」是禁用的', await page.locator('[data-draft-import-accept]').isDisabled())

  // 选那一格接受，提交
  await field.locator('[data-draft-import-pick-accept="it-1:/definition/text"]').check()
  await until(async () => !(await page.locator('[data-draft-import-accept]').isDisabled()))
  check('选中之后按钮写清接受几格',
    (await textOf(page.locator('[data-draft-import-accept]'))).includes('接受选中的 1 格'),
    await textOf(page.locator('[data-draft-import-accept]')))
  await page.locator('[data-draft-import-accept]').click()
  await until(async () => accepts.length === 1)
  const acceptBody = accepts[0].body
  check('接受按工作项身份（item_id）指认，只提交勾中的那一格、明确列出字段路径',
    accepts[0].runId === 'im-1' && acceptBody.items?.length === 1
    && acceptBody.items[0].item_id === 'it-1' && acceptBody.items[0].metric_id === 'm1'
    && JSON.stringify(acceptBody.items[0].fields) === JSON.stringify(['/definition/text'])
    && acceptBody.items[0].auto === undefined,
    JSON.stringify(acceptBody.items))
  check('接受带上读到的基准版本（v7）', acceptBody.expected_revision === 7, String(acceptBody.expected_revision))
  check('接受带上幂等键', typeof acceptBody.request_id === 'string' && acceptBody.request_id.length > 0,
    String(acceptBody.request_id))
  const result = page.locator('[data-draft-import-result]')
  await result.waitFor({ state: 'visible' })
  check('接受之后回执写清落到哪一版', (await textOf(result)).includes('落到 v8'), await textOf(result))
  check('回执写清图和基线是同一次提交里推进的、没接受的格子不推进',
    (await textOf(result)).includes('没有接受的格子不会推进基线'), await textOf(result))
  check('接受之后刷新验证状态（字段基线跟着再取一次）',
    state.calls.filter((c) => c.method === 'GET' && c.path === '/ledger/drafts/d-orders/validations').length >= 2)
  check('接受之后详情重新取了一次（拿到新的基准版本）',
    state.calls.filter((c) => c.method === 'GET' && c.path === '/ledger/drafts/d-orders').length >= 2)
  check('接受之后这次导入的现状也刷新了（格子标成已接受）',
    (await textOf(page.locator('[data-draft-import-item="m1"]'))).includes('已接受'))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))

  // 第一次读出来的覆盖：有基线不等于整张都有，partial 要说出来
  const coverage = page.locator('[data-draft-import-coverage]')
  await until(async () => await coverage.getAttribute('data-draft-import-coverage') === 'partial')
  check('覆盖状态照服务端说是 partial', await coverage.getAttribute('data-draft-import-coverage') === 'partial',
    await coverage.getAttribute('data-draft-import-coverage'))
  check('写清还有几格、几条指标没有基线', (await textOf(coverage)).includes('还有 1 格没有基线')
    && (await textOf(coverage)).includes('1 条指标整条没有基线'), await textOf(coverage))
})

// ================================================================ 10 重新读取一次

await section('重新读取：按文档重读是显式动作、带幂等键，抽的过程中跟着轮询这一次的现状', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = []
  const creates = []
  state.createImport_ = (body) => {
    creates.push(body)
    return { status: 201, json: importRun({ run_id: 'im-9', status: 'running', items: [] }) }
  }
  const polls = []
  state.getImport = (runId) => {
    polls.push(runId)
    return { json: importRun({ run_id: runId, status: 'succeeded',
      items: [importItem({ fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })] }) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  check('还没有记录时说清还没有重导入记录',
    (await textOf(page.locator('[data-draft-imports]'))).includes('还没有重导入记录'),
    await textOf(page.locator('[data-draft-imports]')))
  check('没有记录时不摆明细块', await page.locator('[data-draft-import-detail]').count() === 0)
  check('重读之前没有写过任何东西（只有读接口）',
    state.calls.every((call) => call.method === 'GET'), JSON.stringify(state.calls.filter((c) => c.method !== 'GET')))
  await page.locator('[data-draft-import-create]').click()
  await until(async () => creates.length === 1)
  check('重读带幂等键（同一份草稿 + 同一个键只建一次）',
    typeof creates[0].request_id === 'string' && creates[0].request_id.length > 0, JSON.stringify(creates[0]))
  check('重读只发一次（没重复建）', creates.length === 1, `${creates.length} 次`)
  await until(async () => polls.length >= 1, 8000)
  check('抽的过程中跟着轮询这一次导入的现状', polls.length >= 1 && polls[0] === 'im-9', JSON.stringify(polls))
  check('跑到终态时状态照服务端说',
    await until(async () => (await textOf(page.locator('[data-draft-import-status]'))).includes('读完了'), 8000),
    await textOf(page.locator('[data-draft-import-status]')))
  check('新的这一次出现在记录列表里', await page.locator('[data-draft-import-run="im-9"]').count() === 1)
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 11 人工冲突 / 同名不对齐 / 拒绝

await section('冲突：同一格人工改过时按状态说清，接受取文档的、拒绝留现在的；同名不同范围要人来定对齐', async () => {
  const conflict = importItem({
    item_id: 'it-9', name: '订单金额', metric_id: 'm2',
    fields: [
      importField('/unit', 'conflict', { b: '元', l: '万元', r: '元' }),
      importField('/grain', 'update', { b: '订单行', l: '订单行', r: '订单行（明细）' }),
    ],
  })
  const manual = importItem({
    item_id: 'it-8', seq: 2, name: '订单数', metric_id: 'm1',
    align: { metric_id: 'm1', mode: 'manual', evidence: ['ch-2'], live_evidence: ['ch-1'],
      reason: '候选带着和图上一样的 metric_id，但引的原文不是同一段（同名不同范围）：请人工确认' },
    fields: [importField('/unit', 'manual', { l: '单' })],
  })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [conflict, manual] })]
  const rejects = []
  state.reject = (runId, body) => {
    rejects.push({ runId, body })
    const after = importItem({ item_id: 'it-9', name: '订单金额', metric_id: 'm2',
      status: 'partial', rejected: ['/grain'],
      fields: [
        importField('/unit', 'conflict', { b: '元', l: '万元', r: '元' }),
        importField('/grain', 'update', { b: '订单行', l: '订单行', r: '订单行（明细）', decided: 'rejected' }),
      ] })
    return { json: importRun({ items: [after, manual] }) }
  }
  const aligns = []
  state.align = (runId, itemId, body) => {
    aligns.push({ runId, itemId, body })
    const fixed = importItem({ item_id: 'it-8', seq: 2, name: '订单数', metric_id: '',
      align: { metric_id: '', mode: 'new', reason: '没有对得上的指标：请人工确认' },
      fields: [importField('/unit', 'new', { r: '单' })] })
    return { json: importRun({ items: [conflict, fixed] }) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-import-run="im-1"]').click()
  await page.locator('[data-draft-import-detail]').waitFor({ state: 'visible' })

  const conflictCell = page.locator('[data-draft-import-field="it-9:/unit"]')
  const conflictText = await textOf(conflictCell)
  check('人工修改冲突照状态写出来', await conflictCell.getAttribute('data-import-field-state') === 'conflict'
    && conflictText.includes('人工修改冲突'), conflictText)
  check('写清接受取文档的、拒绝留现在的内容',
    conflictText.includes('接受取文档的，拒绝就留你现在的内容'), conflictText)
  check('三方的值都在（元 / 万元 / 元）',
    (await textOf(conflictCell.locator('[data-draft-import-value="l"]'))).includes('万元')
    && (await textOf(conflictCell.locator('[data-draft-import-value="r"]'))).includes('元'), conflictText)
  check('冲突那一格能接受（要人点名，不自动收）',
    !(await conflictCell.locator('[data-draft-import-pick-accept="it-9:/unit"]').isDisabled()))

  // 同名不同范围：身份对得上但引的原文不是同一段 → 要人来定，页面上给得出对齐入口
  const manualItem = page.locator('[data-draft-import-item="m1"]')
  const manualText = await textOf(manualItem)
  check('同名不同范围照实说是要人来定', await page.locator('[data-draft-import-reason="manual"]').count() === 1
    && manualText.includes('同名不同范围'), manualText)
  check('这种情形也给得出人工对齐的入口',
    await manualItem.locator('[data-draft-import-align-select="it-8"]').count() === 1)
  await manualItem.locator('[data-draft-import-align-select="it-8"]').selectOption('m2')
  await manualItem.locator('[data-draft-import-align-submit="it-8"]').click()
  await until(async () => aligns.length === 1)
  check('对齐把人工选的那条指标发给服务端',
    aligns[0].runId === 'im-1' && aligns[0].itemId === 'it-8' && aligns[0].body.metric_id === 'm2',
    JSON.stringify(aligns[0]))

  // 拒绝：只拒勾中的那一格，保持现在的内容不动
  await page.locator('[data-draft-import-pick-reject="it-9:/grain"]').check()
  await until(async () => !(await page.locator('[data-draft-import-reject]').isDisabled()))
  await page.locator('[data-draft-import-reject]').click()
  await until(async () => rejects.length === 1)
  check('拒绝按工作项身份（item_id）指认，只提交勾中的那一格',
    rejects[0].runId === 'im-1' && rejects[0].body.items?.length === 1
    && rejects[0].body.items[0].item_id === 'it-9' && rejects[0].body.items[0].metric_id === 'm2'
    && JSON.stringify(rejects[0].body.items[0].fields) === JSON.stringify(['/grain']),
    JSON.stringify(rejects[0].body.items))
  check('拒绝之后写清现在的内容和基线都没动',
    (await textOf(page.locator('[data-draft-import-result]'))).includes('现在的内容和字段基线都没动'),
    await textOf(page.locator('[data-draft-import-result]')))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 12 没有基线：每格都要人工对齐

await section('没有基线：baseline_unknown 不假装有基线，逐格要人工对齐到现有指标，覆盖照实说读不出来', async () => {
  const item = importItem({
    item_id: 'it-3', metric_id: '', candidate_id: 'c3', name: '客单价', status: 'manual',
    align: { metric_id: '', mode: 'manual', evidence: ['ch-3'],
      reason: '这条指标没有可比的基线：请人工对齐' },
    fields: [importField('/expression', 'manual', { r: 'cell(nodes.fetch, 0, \'avg\')' })],
  })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.baseline = { status: 'baseline_unknown', readable: false,
    reason: '这份草稿没有字段基线：当时被接受的导入值无法还原，不能拿当前内容当作 B',
    coverage: { status: 'unknown', metrics: {}, uncovered_metric_ids: [] } }
  state.imports = [importRun({ items: [item] })]
  const aligns = []
  state.align = (runId, itemId, body) => {
    aligns.push({ runId, itemId, body })
    return { json: importRun({ items: [importItem({
      item_id: 'it-3', metric_id: body.metric_id, name: '客单价', status: 'update',
      fields: [importField('/expression', 'update', { b: "cell(nodes.fetch, 0, 'avg')", l: "cell(nodes.fetch, 0, 'avg')",
        r: "cell(nodes.fetch, 0, 'avg_2')" })],
    })] }) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-import-run="im-1"]').click()
  await page.locator('[data-draft-import-detail]').waitFor({ state: 'visible' })

  const coverage = page.locator('[data-draft-import-coverage]')
  check('没有基线时覆盖照实说读不出来（不是 complete，也不是 partial）',
    await coverage.getAttribute('data-draft-import-coverage') === 'unknown', await coverage.getAttribute('data-draft-import-coverage'))
  check('写清不能拿现在的内容当作基线', (await textOf(coverage)).includes('不能拿现在的内容当作它'), await textOf(coverage))

  const cell = page.locator('[data-draft-import-field="it-3:/expression"]')
  check('没有可比基线的格子标成需要人工对齐',
    await cell.getAttribute('data-import-field-state') === 'manual'
    && (await textOf(cell)).includes('需要人工对齐'), await textOf(cell))
  check('没对齐之前不能接受（先由人指定对到哪条指标）',
    await cell.locator('[data-draft-import-pick-accept="it-3:/expression"]').isDisabled())

  // 逐格人工对齐：这条候选取值没有对上的指标，选一条现有的指标把它对上去
  const alignSelect = page.locator('[data-draft-import-align-select="it-3"]')
  check('没对齐的工作项给得出对齐到哪条指标的入口', await alignSelect.count() === 1)
  check('可选的指标就是这张卡上的指标', await alignSelect.locator('option').count() === 3,
    String(await alignSelect.locator('option').count()))
  await alignSelect.selectOption('m2')
  await page.locator('[data-draft-import-align-submit="it-3"]').click()
  await until(async () => aligns.length === 1)
  check('人工对齐把选中的指标发出去', aligns[0].itemId === 'it-3' && aligns[0].body.metric_id === 'm2',
    JSON.stringify(aligns[0]))
  check('对齐之后这一格变成有基线可比的更新',
    await page.locator('[data-draft-import-field="it-3:/expression"]').getAttribute('data-import-field-state') === 'update',
    await page.locator('[data-draft-import-field="it-3:/expression"]').getAttribute('data-import-field-state'))
  check('对齐之后才谈得上接受',
    !(await page.locator('[data-draft-import-pick-accept="it-3:/expression"]').isDisabled()))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 13 中断与恢复

await section('恢复：被中断的那一次由人点「恢复」显式续跑，已处置的不动、不重复建版', async () => {
  const done = importItem({ item_id: 'it-1', status: 'accepted', accepted: ['/unit'], applied_revision: 8,
    fields: [importField('/unit', 'unchanged', { b: '元', l: '元', r: '元', decided: 'accepted' })] })
  const pending = importItem({ item_id: 'it-2', seq: 2, name: '订单金额', metric_id: 'm2', status: 'pending',
    fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ status: 'interrupted', error: '服务中断，已保留导入进度，可恢复',
    items: [done, pending], items_decided: 1 })]
  const resumes = []
  state.resume = (runId) => {
    resumes.push(runId)
    return { json: importRun({ status: 'queued', items: [done, pending], items_decided: 1 }) }
  }
  state.getImport = (runId) => ({ json: importRun({ run_id: runId, status: 'succeeded',
    items: [done, pending], items_decided: 1 }) })
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-draft-imports]')
  check('被中断的那一次照实说是中断了、可以恢复',
    (await textOf(page.locator('[data-draft-import-status]'))).includes('可以恢复'),
    await textOf(page.locator('[data-draft-import-status]')))
  check('说清还有几条没做完', await page.locator('[data-draft-import-unfinished]').count() === 1
    && (await textOf(page.locator('[data-draft-import-unfinished]'))).includes('还有 1 条没做完'),
    await textOf(page.locator('[data-draft-import-unfinished]')))
  check('已经接受的格子标成已接受，未完成的照旧是没处置',
    (await textOf(page.locator('[data-draft-import-item="m1"]'))).includes('已接受')
    && (await textOf(page.locator('[data-draft-import-item="m2"]'))).includes('还没处置'))
  await page.locator('[data-draft-import-resume]').click()
  await until(async () => resumes.length === 1)
  check('恢复只发一次恢复请求（不重复建版、也不替人接受）',
    resumes.length === 1 && resumes[0] === 'im-1'
    && importCalls(state, 'POST', '/accept').length === 0, JSON.stringify(resumes))
  await until(async () => await panel.getAttribute('data-draft-imports') === 'succeeded')
  check('恢复之后跟着轮询现状，直到它离开运行中', (await textOf(page.locator('[data-draft-import-status]'))).includes('读完了'),
    await textOf(page.locator('[data-draft-import-status]')))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 14 基准版本过期：选择保留

await section('冲突：接受时基准版本过期，选好的格子一个不丢、不自动重发，刷新详情后由人再点一次', async () => {
  const item = importItem({ fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [item] })]
  // stale 之后页面会重新读这一次导入的现状（逐字段三方按服务端现在的内容重算）
  state.getImport = () => ({ json: importRun({ items: [item] }) })
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    if (accepts.length === 1) {
      state.draft.revision = 8
      state.draft.version = 8
      return { status: 409, json: { detail: '口径已经改到 v8，你的版本是 v7：请刷新后重新核对再接受',
        code: 'stale_revision', current_version: 8 } }
    }
    return { json: acceptOut(importRun({ items: [importItem({
      fields: [importField('/unit', 'unchanged', { b: '万元', l: '万元', r: '万元', decided: 'accepted' })],
      status: 'accepted', accepted: ['/unit'], applied_revision: 9 })], items_decided: 1 }), 9) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-import-run="im-1"]').click()
  await page.locator('[data-draft-import-detail]').waitFor({ state: 'visible' })
  await page.locator('[data-draft-import-pick-accept="it-1:/unit"]').check()
  await page.locator('[data-draft-import-accept]').click()
  await page.locator('[data-draft-import-stale]').waitFor({ state: 'visible' })
  const stale = await textOf(page.locator('[data-draft-import-stale]'))
  check('写清你基于哪一版、服务端现在是哪一版',
    stale.includes('你基于 v7') && stale.includes('服务端现在是 v8'), stale)
  check('说清选好的格子还在、没有替你重发', stale.includes('你选好的格子还在'), stale)
  check('没有自动重发（只发了那一次接受）', accepts.length === 1, `${accepts.length} 次`)
  check('详情被重新读了一次（拿到新的基准版本）',
    state.calls.filter((c) => c.method === 'GET' && c.path === '/ledger/drafts/d-orders').length >= 2)
  check('选好的那一格一个字没丢',
    await page.locator('[data-draft-import-pick-accept="it-1:/unit"]').isChecked())
  check('按钮还在、还写着接受 1 格',
    !(await page.locator('[data-draft-import-accept]').isDisabled())
    && (await textOf(page.locator('[data-draft-import-accept]'))).includes('接受选中的 1 格'))

  await page.locator('[data-draft-import-accept]').click()
  await until(async () => accepts.length === 2)
  check('第二次用的是刷新后的版本（v8）', accepts[1].body.expected_revision === 8,
    String(accepts[1].body.expected_revision))
  check('第二次还是按同一条工作项身份提交',
    accepts[1].body.items?.[0]?.item_id === 'it-1', JSON.stringify(accepts[1].body.items))
  check('同内容重试用同一个请求键（服务端认得出是同一件事）',
    accepts[0].body.request_id === accepts[1].body.request_id,
    `${accepts[0].body.request_id} / ${accepts[1].body.request_id}`)
  await until(async () => (await page.locator('[data-draft-import-result]').count()) === 1)
  check('第二次接受落到了 v9',
    (await textOf(page.locator('[data-draft-import-result]'))).includes('落到 v9'),
    await textOf(page.locator('[data-draft-import-result]')))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 15 一句话改口径：预览 / 澄清 / 确认

await section('一句话：先看人读的影响（不摆 JSON 和节点编号），含糊先澄清，确认之后才落一版', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const proposals = []
  state.proposal = (body) => {
    proposals.push(body)
    return { json: { draft_id: 'd-orders', workflow_id: 'wf-orders', revision: 7, card: 'card',
      proposal: proposalOf(), applied: false, persisted: false } }
  }
  const writes = []
  state.nl = (body) => {
    writes.push(body)
    const live = draftOf()
    live.revision = 8
    live.version = 8
    live.metrics = live.metrics.map((metric) => ({ ...metric, time_role: 'payment_time' }))
    live.change = receiptOf({ revision: 8, operations: [], binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-draft-nl]')
  check('一句话改口径一块画出来了', await panel.count() === 1)
  check('没预览之前没有「确认并应用」（没有确认就不写）',
    await page.locator('[data-draft-nl-confirm]').count() === 0)
  check('面板上写清预览不写任何东西',
    (await textOf(panel)).includes('预览本身不写任何东西'), await textOf(panel))

  await page.locator('[data-draft-nl-input]').fill('订单金额按支付时间算')
  await page.locator('[data-draft-nl-preview]').click()
  await until(async () => proposals.length === 1)
  check('预览只调只读的提案接口，带上目标卡',
    proposals[0].instruction === '订单金额按支付时间算' && proposals[0].target?.card_id === 'card'
    && proposals[0].target?.workflow_id === 'wf-orders', JSON.stringify(proposals[0]))
  await page.locator('[data-draft-nl-changes]').waitFor({ state: 'visible' })
  const changes = await textOf(page.locator('[data-draft-nl-changes]'))
  check('影响按人读的说法写：哪条指标、哪一项写法、现在什么 → 保存后什么',
    changes.includes('订单数') && changes.includes('订单金额') && changes.includes('统计时间口径')
    && changes.includes('现在 按下单时间') && changes.includes('保存后 按支付时间'), changes)
  check('不摆 JSON、节点编号和字段路径',
    !/[\{\}\[\]"]|time_role|nodes\.|metric_id|\/definition/.test(changes), changes)
  check('写清会改到几处', (await textOf(page.locator('[data-draft-nl-count]'))).includes('会改到 2 处'),
    await textOf(page.locator('[data-draft-nl-count]')))
  check('预览不落版（没有发过一句话写请求）', writes.length === 0 && patchBodies(state).length === 0)

  // 确认之后才写一版
  await page.locator('[data-draft-nl-confirm]').click()
  await until(async () => writes.length === 1)
  check('确认之后才调 nl-change，带 confirm=true',
    writes[0].confirm === true && writes[0].instruction === '订单金额按支付时间算', JSON.stringify(writes[0]))
  check('确认带上读到的基准版本', writes[0].expected_revision === 7, String(writes[0].expected_revision))
  check('确认带上幂等键', typeof writes[0].request_id === 'string' && writes[0].request_id.length > 0,
    String(writes[0].request_id))
  await page.locator('[data-draft-nl-result]').waitFor({ state: 'visible' })
  check('落版之后写清落到哪一版',
    (await textOf(page.locator('[data-draft-nl-result]'))).includes('落到 v8'),
    await textOf(page.locator('[data-draft-nl-result]')))
  check('落版之后写清旧验证不再覆盖这一版',
    (await textOf(page.locator('[data-draft-nl-result]'))).includes('旧的试运行和验证不再覆盖这一版'),
    await textOf(page.locator('[data-draft-nl-result]')))
  check('一句话只走这一条写路径（没有 PATCH）', patchBodies(state).length === 0)
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 16 一句话：先澄清、失败不擅自改

await section('一句话：含糊的业务条件先显示澄清，一条操作都不产出、也不给确认按钮；落版失败时什么都不改', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const writes = []
  state.nl = (body) => {
    writes.push(body)
    return { status: 409, json: { detail: '这句话提到「退款处理」，但没说清取哪个值：请指定全部计入、排除全额退款',
      code: 'change_proposal_ambiguous' } }
  }
  state.proposal = (body) => ({ json: { draft_id: 'd-orders', workflow_id: 'wf-orders', revision: 7, card: 'card',
    applied: false, persisted: false,
    proposal: proposalOf({ ready: false, operations: [], clarifications: [{ kind: 'ambiguous', field: 'refund_rule',
      question: '这句话提到「退款处理」，但没说清取哪个值：请指定全部计入、排除全额退款',
      options: [{ value: 'include_all', label: '全部计入' }, { value: 'exclude_full_refund', label: '排除全额退款' }] }] }) } })
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-nl-input]').fill('排除退款')
  await page.locator('[data-draft-nl-preview]').click()
  await page.locator('[data-draft-nl-clarify]').waitFor({ state: 'visible' })
  const clarify = await textOf(page.locator('[data-draft-nl-clarify]'))
  check('含糊的条件照服务端的问题写出来', clarify.includes('没说清取哪个值'), clarify)
  check('把允许的取值原样列出来（不替人猜）',
    clarify.includes('全部计入') && clarify.includes('排除全额退款'), clarify)
  check('澄清时不产出任何修改（没有影响列表）', await page.locator('[data-draft-nl-changes]').count() === 0)
  check('澄清时没有确认按钮（先问清）', await page.locator('[data-draft-nl-confirm]').count() === 0)
  check('澄清之前也没写过任何东西', writes.length === 0)

  // 就算硬发一次确认（服务端仍然说不清）：失败照实说，页面上什么都不改
  const state2 = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state2.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state2.proposal = () => ({ json: { draft_id: 'd-orders', workflow_id: 'wf-orders', revision: 7, card: 'card',
    applied: false, persisted: false, proposal: proposalOf() } })
  const failures = []
  state2.nl = (body) => {
    failures.push(body)
    return { status: 409, json: { detail: '口径已经改到 v8，你的版本是 v7：请刷新后重新核对再改',
      code: 'stale_revision', current_version: 8 } }
  }
  const second = await pageOn(state2)
  await expand(second.page, state2.draft)
  await second.page.locator('[data-draft-nl-input]').fill('订单金额按支付时间算')
  await second.page.locator('[data-draft-nl-preview]').click()
  await second.page.locator('[data-draft-nl-changes]').waitFor({ state: 'visible' })
  await second.page.locator('[data-draft-nl-confirm]').click()
  await until(async () => failures.length === 1)
  const failure = await textOf(second.page.locator('[data-draft-nl] [role="alert"]'))
  check('落版失败时照实报错（服务端说不清就一个字都不改）',
    failures[0].confirm === true && failure.includes('口径已经改到 v8'), failure)
  check('失败之后输入和预览原样留着（人再决定）',
    await second.page.locator('[data-draft-nl-input]').inputValue() === '订单金额按支付时间算'
    && await second.page.locator('[data-draft-nl-changes]').count() === 1)
  check('失败时没有落成任何一版（没有成功回执）',
    await second.page.locator('[data-draft-nl-result]').count() === 0)
  check('这一页没有报错', second.errors.length === 0, second.errors.slice(0, 2).join('；'))
  check('这一页没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 17 拒绝之后批量、重复工作项、抽坏了的项

await section('批量：拒绝过的格子不再被收进来，一次收掉的按钮把剩下的几格明确列出来（不发 auto）', async () => {
  const clean = () => importItem({ item_id: 'it-1', fields: [
    importField('/unit', 'update', { b: '元', l: '元', r: '万元' }),
    importField('/grain', 'update', { b: '订单行', l: '订单行', r: '订单行（明细）' }),
    importField('/definition/text', 'update', { b: '按已付款订单统计', l: '按已付款订单统计', r: '按已付款订单统计（退款另计）' }),
  ] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [clean()] })]
  state.getImport = () => ({ json: importRun({ items: [state.imports[0].items[0]] }) })
  const rejects = []
  state.reject = (runId, body) => {
    rejects.push({ runId, body })
    const after = importItem({ item_id: 'it-1', status: 'partial', rejected: ['/unit'], fields: [
      importField('/unit', 'update', { b: '元', l: '元', r: '万元', decided: 'rejected' }),
      importField('/grain', 'update', { b: '订单行', l: '订单行', r: '订单行（明细）' }),
      importField('/definition/text', 'update', { b: '按已付款订单统计', l: '按已付款订单统计', r: '按已付款订单统计（退款另计）' }),
    ] })
    return { json: importRun({ items: [after] }) }
  }
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    return { json: acceptOut(importRun({ items: [importItem({ item_id: 'it-1', status: 'accepted',
      accepted: ['/grain', '/definition/text'], applied_revision: 8, fields: [
        importField('/grain', 'unchanged', { b: '订单行（明细）', l: '订单行（明细）', r: '订单行（明细）', decided: 'accepted' }),
        importField('/definition/text', 'unchanged', { b: '按已付款订单统计（退款另计）', l: '按已付款订单统计（退款另计）',
          r: '按已付款订单统计（退款另计）', decided: 'accepted' }),
      ] })], items_decided: 1 }), 8) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  check('三格都能收时，按钮写清 3 格',
    (await textOf(page.locator('[data-draft-import-accept-clean="it-1"]'))).includes('无冲突的 3 格'),
    await textOf(page.locator('[data-draft-import-accept-clean="it-1"]')))

  // 先拒掉一格：那一格不该再被批量收进去
  await page.locator('[data-draft-import-pick-reject="it-1:/unit"]').check()
  await page.locator('[data-draft-import-reject]').click()
  await until(async () => rejects.length === 1)
  check('拒绝把勾中的那一格指成工作项身份 + 字段路径',
    rejects[0].body.items?.[0]?.item_id === 'it-1'
    && JSON.stringify(rejects[0].body.items[0].fields) === JSON.stringify(['/unit']),
    JSON.stringify(rejects[0].body.items))
  const rejectedCell = page.locator('[data-draft-import-field="it-1:/unit"]')
  check('拒绝过的那一格不再摆出选择（已经决定过了）',
    await rejectedCell.locator('[data-draft-import-pick-accept="it-1:/unit"]').count() === 0
    && await rejectedCell.locator('[data-draft-import-pick-reject="it-1:/unit"]').count() === 0)
  check('批量按钮只剩 2 格（拒绝过的不再被收）',
    (await textOf(page.locator('[data-draft-import-accept-clean="it-1"]'))).includes('无冲突的 2 格'),
    await textOf(page.locator('[data-draft-import-accept-clean="it-1"]')))

  await page.locator('[data-draft-import-accept-clean="it-1"]').click()
  await until(async () => accepts.length === 1)
  const body = accepts[0].body
  check('一次收掉发的是**明确列出来的**剩下那几格（不是 auto）',
    body.items?.length === 1 && body.items[0].auto === undefined
    && JSON.stringify(body.items[0].fields) === JSON.stringify(['/grain', '/definition/text']),
    JSON.stringify(body.items))
  check('这条也带着工作项身份和落点指标',
    body.items[0].item_id === 'it-1' && body.items[0].metric_id === 'm1', JSON.stringify(body.items[0]))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('重复工作项：同一条指标上有两条候选时，接受 / 拒绝按工作项身份指认，不指错', async () => {
  const first = importItem({ item_id: 'it-a', seq: 1, candidate_id: 'ca', name: '订单数',
    fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })
  const second = importItem({ item_id: 'it-b', seq: 2, candidate_id: 'cb', name: '订单数（另一种说法）',
    fields: [importField('/grain', 'update', { b: '订单行', l: '订单行', r: '订单行（明细）' })] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [first, second] })]
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    return { json: acceptOut(importRun({ items: [first, second], items_decided: 1 }), 8) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await until(async () => await page.locator('[data-draft-import-item="m1"]').count() === 2)
  check('两条候选都摆出来了（同一条指标）', await page.locator('[data-draft-import-item="m1"]').count() === 2,
    String(await page.locator('[data-draft-import-item="m1"]').count()))
  // 只对第二条候选的那一格点接受
  await page.locator('[data-draft-import-pick-accept="it-b:/grain"]').check()
  await page.locator('[data-draft-import-accept]').click()
  await until(async () => accepts.length === 1)
  check('按工作项身份指认，没有指到同一条指标上的另一条候选',
    accepts[0].body.items?.length === 1 && accepts[0].body.items[0].item_id === 'it-b'
    && JSON.stringify(accepts[0].body.items[0].fields) === JSON.stringify(['/grain']),
    JSON.stringify(accepts[0].body.items))
  check('批量按钮也按各自的工作项身份发',
    (await textOf(page.locator('[data-draft-import-accept-clean="it-a"]'))).includes('无冲突的 1 格'))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('抽坏了的工作项：所有接受都禁用，批量里也不收它', async () => {
  const broken = importItem({ item_id: 'it-x', seq: 1, name: '客单价', metric_id: 'm2', status: 'failed',
    error: '候选的引用指向的段落没有提供给模型：不能当出处，请重新抽取或人工补依据',
    fields: [importField('/expression', 'update', { b: "cell(nodes.fetch, 0, 'avg')", l: "cell(nodes.fetch, 0, 'avg')",
      r: "cell(nodes.fetch, 0, 'avg_2')" })] })
  const good = importItem({ item_id: 'it-y', seq: 2, name: '订单数', metric_id: 'm1', status: 'update',
    fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [broken, good] })]
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    return { json: acceptOut(importRun({ items: [broken, good], items_decided: 1 }), 8) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const brokenCell = page.locator('[data-draft-import-field="it-x:/expression"]')
  check('抽坏了的工作项标成这条没抽好', (await textOf(page.locator('[data-draft-import-item="m2"]')))
    .includes('这条没抽好'), await textOf(page.locator('[data-draft-import-item="m2"]')))
  check('抽坏了的那条：接受是禁用的', await brokenCell.locator('[data-draft-import-pick-accept="it-x:/expression"]').isDisabled())
  check('抽坏了的那条不摆「一次收掉」的按钮',
    await page.locator('[data-draft-import-accept-clean="it-x"]').count() === 0)
  check('抽坏了的那条仍然可以拒绝（不是口径的就拒掉）',
    !(await brokenCell.locator('[data-draft-import-pick-reject="it-x:/expression"]').isDisabled()))
  check('批量只算能接受的那一条（1 格）',
    (await textOf(page.locator('[data-draft-import-accept]'))).includes('接受选中的 0 格')
    && await page.locator('[data-draft-import-accept]').isDisabled())
  await page.locator('[data-draft-import-pick-accept="it-y:/unit"]').check()
  await until(async () => !(await page.locator('[data-draft-import-accept]').isDisabled()))
  await page.locator('[data-draft-import-accept]').click()
  await until(async () => accepts.length === 1)
  check('提交里只有能接受的那一条（抽坏的不混进去）',
    accepts[0].body.items?.length === 1 && accepts[0].body.items[0].item_id === 'it-y',
    JSON.stringify(accepts[0].body.items))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 18 这次导入比的还是旧的 L

await section('冲突：服务端说这次导入比的还是旧的 L（import_stale_l），重新读回现状、选择留着、不拿旧 L 覆盖', async () => {
  const before = importItem({ item_id: 'it-1', fields: [importField('/unit', 'update', { b: '元', l: '元', r: '万元' })] })
  // 别处把这张卡改过了：重新读回来时 L 已经不是「元」，这一格成了人工冲突
  const after = importItem({ item_id: 'it-1', status: 'conflict',
    fields: [importField('/unit', 'conflict', { b: '元', l: '万元', r: '元' })] })
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.imports = [importRun({ items: [before] })]
  // 服务端说「三方算的是旧的 L」之后，页面要按现在的 L / B 重算一次（POST …/refresh），把新三方读回来
  let refreshes = 0
  state.refresh = (runId) => {
    refreshes += 1
    return { json: importRun({ run_id: runId, items: [after] }) }
  }
  const state2Reads = () => refreshes
  const accepts = []
  state.accept = (runId, body) => {
    accepts.push({ runId, body })
    if (accepts.length === 1) {
      return { status: 409, json: { detail: '这一格的当前内容和这次导入算候选时的那一版不一样了：请刷新这次导入的三方、'
        + '按现在的口径重新确认后再接受', code: 'import_stale_l',
        fields: [{ item_id: 'it-1', metric_id: 'm1', path: '/unit' }] } }
    }
    return { json: acceptOut(importRun({ items: [importItem({ item_id: 'it-1', status: 'accepted',
      accepted: ['/unit'], applied_revision: 9,
      fields: [importField('/unit', 'unchanged', { b: '元', l: '元', r: '元', decided: 'accepted' })] })], items_decided: 1 }), 9) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-import-pick-accept="it-1:/unit"]').check()
  await page.locator('[data-draft-import-accept]').click()
  await page.locator('[data-draft-import-stale]').waitFor({ state: 'visible' })
  check('照服务端的码标出来（不是笼统的过期）',
    await page.locator('[data-draft-import-stale]').getAttribute('data-draft-import-stale') === 'import_stale_l',
    await page.locator('[data-draft-import-stale]').getAttribute('data-draft-import-stale'))
  const stale = await textOf(page.locator('[data-draft-import-stale]'))
  check('指名道姓说清是哪一格对不上了、已经按现在的口径重算了三方',
    stale.includes('单位') && stale.includes('已经按现在的口径把这次导入的三方重算了一遍'), stale)
  check('没有自动重发（只发了那一次接受）', accepts.length === 1, `${accepts.length} 次`)
  check('走的是「按现在的 L / B 重算三方」那条路（POST …/refresh），不是只更新版本号',
    state2Reads() >= 1, String(state2Reads()))
  const cell = page.locator('[data-draft-import-field="it-1:/unit"]')
  await until(async () => (await cell.getAttribute('data-import-field-state')) === 'conflict')
  check('三方按**重新读回来**的内容写：现在的内容变成 万元，状态是人工修改冲突',
    await cell.getAttribute('data-import-field-state') === 'conflict'
    && (await textOf(cell.locator('[data-draft-import-value="l"]'))).includes('万元'),
    await textOf(cell))
  check('选好的那一格还留着（没有拿旧 L 直接覆盖，也没清空选择）',
    await page.locator('[data-draft-import-pick-accept="it-1:/unit"]').isChecked())
  check('详情也重新读了一次（拿到新的基准版本）',
    state.calls.filter((call) => call.method === 'GET' && call.path === '/ledger/drafts/d-orders').length >= 2)
  await page.locator('[data-draft-import-accept]').click()
  await until(async () => accepts.length === 2)
  check('第二次按重新读回来的现状提交（同一格、同一身份）',
    accepts[1].body.items?.[0]?.item_id === 'it-1'
    && JSON.stringify(accepts[1].body.items[0].fields) === JSON.stringify(['/unit']),
    JSON.stringify(accepts[1].body.items))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 19 一句话：输入改过必须先重新预览、落下的要对得上

await section('一句话：输入改过之后必须重新预览，确认发的就是预览时那一段原文', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const proposals = []
  state.proposal = (body) => {
    proposals.push(body)
    return { json: { draft_id: 'd-orders', workflow_id: 'wf-orders', revision: 7, card: 'card',
      applied: false, persisted: false,
      proposal: proposalOf({ operations: [{ op: 'set', metric_id: 'm2', path: '/refund_rule', value: 'exclude_full_refund' }],
        metrics: ['m2'] }) } }
  }
  const writes = []
  state.nl = (body) => {
    writes.push(body)
    const live = draftOf()
    live.revision = 8
    live.version = 8
    live.change = receiptOf({ revision: 8, operations: [{ op: 'set', metric_id: 'm2', path: '/refund_rule', changed: true }],
      binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-nl-input]').fill('订单金额排除全额退款')
  await page.locator('[data-draft-nl-preview]').click()
  await page.locator('[data-draft-nl-changes]').waitFor({ state: 'visible' })
  check('预览写明这份影响是按哪一段原文算的',
    (await textOf(page.locator('[data-draft-nl-of]'))).includes('这份影响是按这一段原文算的：「订单金额排除全额退款」'),
    await textOf(page.locator('[data-draft-nl-of]')))
  check('没改输入时有确认按钮', await page.locator('[data-draft-nl-confirm]').count() === 1)

  // 改了输入：这份提案就不作数了，确认按钮收起来、也不许写
  await page.locator('[data-draft-nl-input]').fill('订单数量按支付时间算')
  await until(async () => (await page.locator('[data-draft-nl]').getAttribute('data-draft-nl-touched')) === 'true')
  check('输入改过就标成「这句话改过了」，确认按钮收起来（必须重新预览）',
    await page.locator('[data-draft-nl-confirm]').count() === 0
    && (await textOf(page.locator('[data-draft-nl-repreview]'))).includes('要先重新预览'),
    await textOf(page.locator('[data-draft-nl-repreview]')))
  check('改过之后一个字都没写', writes.length === 0)
  check('影响列表还标着是按上一次那段原文算的（显示的是 A 就说 A）',
    (await textOf(page.locator('[data-draft-nl-of]'))).includes('输入改过了')
    && (await textOf(page.locator('[data-draft-nl-of]'))).includes('请重新预览'),
    await textOf(page.locator('[data-draft-nl-of]')))

  // 重新预览之后才谈得上确认，且确认发的是**重新预览那一段**
  await page.locator('[data-draft-nl-preview]').click()
  await until(async () => proposals.length === 2)
  check('重新预览用的是输入框现在的内容', proposals[1].instruction === '订单数量按支付时间算',
    JSON.stringify(proposals[1]))
  await page.locator('[data-draft-nl-confirm]').waitFor({ state: 'visible' })
  await page.locator('[data-draft-nl-confirm]').click()
  await until(async () => writes.length === 1)
  check('确认发的是预览那一段原文（不是别的）', writes[0].instruction === '订单数量按支付时间算',
    JSON.stringify(writes[0]))
  check('落下的操作和预览一致时不提示不一致',
    await page.locator('[data-draft-nl-mismatch]').count() === 0)
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('一句话：服务端落下的操作和预览里的不是同一批时，照实提示，不让显示 A 实际落 B', async () => {
  const state = makeState({
    draft: draftOf(),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: 'none' }) },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  state.proposal = () => ({ json: { draft_id: 'd-orders', workflow_id: 'wf-orders', revision: 7, card: 'card',
    applied: false, persisted: false, proposal: proposalOf() } })
  // 服务端这次落下的只有 m1 那一条（预览里是 m1、m2 两条）
  state.nl = () => {
    const live = draftOf()
    live.revision = 8
    live.version = 8
    live.change = receiptOf({ revision: 8,
      operations: [{ op: 'set', metric_id: 'm1', path: '/time_role', changed: true }], binding: live.binding })
    return { json: live }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.locator('[data-draft-nl-input]').fill('订单按支付时间算')
  await page.locator('[data-draft-nl-preview]').click()
  await page.locator('[data-draft-nl-changes]').waitFor({ state: 'visible' })
  await page.locator('[data-draft-nl-confirm]').click()
  await page.locator('[data-draft-nl-mismatch]').waitFor({ state: 'visible' })
  check('写清落下的和预览里的不是同一批、要人到画布上核对',
    (await textOf(page.locator('[data-draft-nl-mismatch]'))).includes('和预览里的不是同一批'),
    await textOf(page.locator('[data-draft-nl-mismatch]')))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

// ================================================================ 20 结论一律按 data 属性核（否定说法反例）

await section('验证反例：页面写的是「还不能算验证通过」，按 data 属性核，不被这四个字骗过', async () => {
  const review = { id: 'rv-5', draft_id: 'd-orders', record_id: 'vr-5', workflow_id: 'wf-orders',
    decision: 'confirmed', actor: 'alice', verified: true, scope: ['m1'], basis: { version: 7 },
    note: null, created_at: '2026-11-01T07:00:00+00:00', covers_current: true }
  const record = recordOf({ id: 'vr-5', status: 'matched', ok: true,
    expected: { source: 'fixture', metrics: { m1: 2, m2: 500 }, hash: 'xh-5' }, review })
  const state = makeState({
    draft: draftOf(),
    // 服务端自己已经按范围收回成 unverified：页面上的说法里带「验证通过」四个字，但不是通过
    validations: { records: [record], summary: { ...summaryOf({ verdict: 'unverified', status: 'matched', record }),
      confirmed: ['m1'], unconfirmed: ['m2'] } },
  })
  state.trial = { run_id: 'run-1', status: 'succeeded', metrics: [], queries: [] }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = validationPanel(page)
  const verdict = await textOf(panel.locator('[data-draft-validation-verdict]'))
  check('页面写的确实是否定说法「还不能算验证通过」', verdict === '还不能算验证通过', verdict)
  check('那句里确实有「验证通过」四个字（只看这四个字就会被骗）', verdict.includes('验证通过'), verdict)
  check('短语判定认得这是否定说法（naive includes 会在这里误报成通过）', claimsVerified(verdict) === false, verdict)
  check('结论按 data 属性核：不是 verified', await panel.getAttribute('data-draft-validation') === 'unverified',
    await panel.getAttribute('data-draft-validation'))
  check('页面没有报错', errors.length === 0, errors.slice(0, 2).join('；'))
})

await section('语义问数：常用叫法可输入多个，对象日期绑定按版本保存', async () => {
  const state = makeState({ draft: draftOf({ draft: {
    metrics: [{ id: 'm1', name: '日 OEE', grain: '工厂、日单值' }], metric_count: 1,
    entry_fields: ['start', 'end', 'plant'].map(name => ({ name, type: 'string', required: true })),
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  state.patch = body => {
    state.draft = { ...state.draft, revision: 8, metrics: [{ ...state.draft.metrics[0], semantic: body.operations[0].value }] }
    return { json: clone(state.draft) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-query-semantics]')
  await panel.locator('summary').click()
  await panel.getByLabel('常用叫法（逗号分隔）').fill('OEE，设备综合效率')
  await panel.getByLabel('汇总与缺失规则').fill('按实际开班时长加权，缺失不写零')
  await panel.getByLabel('按日期查询').check()
  await panel.getByLabel('开始（含）').selectOption('start')
  await panel.getByLabel('结束（不含）').selectOption('end')
  await panel.getByLabel('日期规则说明').fill('按已确认的生产日归属')
  await panel.getByRole('button', { name: '添加对象类型', exact: true }).click()
  await panel.getByLabel('对象类型', { exact: true }).fill('工厂')
  await panel.getByLabel('绑定参数').selectOption('plant')
  await panel.getByRole('button', { name: '添加对象', exact: true }).click()
  await panel.getByLabel('工厂 value', { exact: true }).fill('P100')
  await panel.getByLabel('工厂 label', { exact: true }).fill('工厂甲')
  await panel.getByLabel('已确认上述定义与取数实现一致，发布后启用直接问数').check()
  // 普通 HTTP 部署中没有 randomUUID，仍要能保存；这不是生成安全凭据。
  await page.evaluate(() => Object.defineProperty(window.crypto, 'randomUUID', { value: undefined, configurable: true }))
  await panel.getByRole('button', { name: '保存问数定义' }).click()
  await until(() => patchBodies(state).length)
  const body = patchBodies(state)[0]
  check('单次字段变更带基准版本和目标卡', body?.expected_revision === 7 && body?.target?.card_id === 'card' && !!body?.request_id)
  check('多个常用叫法均保留', JSON.stringify(body?.operations[0]?.value?.aliases) === JSON.stringify(['OEE', '设备综合效率']))
  check('实体编号绑定到工厂参数', body?.operations[0]?.value?.entities[0]?.field === 'plant'
    && body.operations[0].value.entities[0].members[0].value === 'P100')
  check('默认只允许单日，未暗中启用月汇总', JSON.stringify(body?.operations[0]?.value?.time?.periods) === '["day"]')
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义编辑恢复：保存其他指标保留未保存内容并使用新版本', async () => {
  const state = makeState({ draft: draftOf({ draft: {
    metrics: [{ id: 'm1', name: '指标甲' }, { id: 'm2', name: '指标乙' }], metric_count: 2,
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  state.patch = body => {
    const op = body.operations[0]
    state.draft = { ...state.draft, revision: state.draft.revision + 1,
      metrics: state.draft.metrics.map(metric => metric.id === op.metric_id ? { ...metric, semantic: op.value } : metric) }
    return { json: clone(state.draft) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const first = page.locator('[data-query-semantics]').nth(0)
  const second = page.locator('[data-query-semantics]').nth(1)
  await first.locator('summary').click()
  await second.locator('summary').click()
  await first.getByLabel('汇总与缺失规则').fill('甲的汇总规则')
  await second.getByLabel('汇总与缺失规则').fill('乙尚未保存的规则')
  await first.getByRole('button', { name: '保存问数定义' }).click()
  await until(() => state.draft.revision === 8)
  await until(async () => !await first.getByText('有未保存的修改；保存其他指标不会覆盖这里的内容。').count())
  check('保存甲后乙仍展开且保留输入', await second.getByLabel('汇总与缺失规则').isVisible()
    && await second.getByLabel('汇总与缺失规则').inputValue() === '乙尚未保存的规则')
  await second.getByRole('button', { name: '保存问数定义' }).click()
  await until(() => patchBodies(state).length === 2)
  check('乙按新版本保存原编辑内容', patchBodies(state)[1]?.expected_revision === 8
    && patchBodies(state)[1]?.operations[0]?.value?.aggregation === '乙尚未保存的规则')
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义编辑恢复：同指标外部更新阻止覆盖并可载入最新版', async () => {
  const state = makeState({ draft: draftOf({ draft: {
    metrics: [{ id: 'm1', name: '指标甲' }, { id: 'm2', name: '指标乙' }], metric_count: 2,
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  state.patch = body => {
    state.draft = { ...state.draft, revision: 9, metrics: [
      { ...state.draft.metrics[0], semantic: body.operations[0].value },
      { ...state.draft.metrics[1], semantic: { version: 1, enabled: false, aliases: [], grain: '',
        aggregation: '其他人更新的规则', time: null, entities: [] } },
    ] }
    return { json: clone(state.draft) }
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const first = page.locator('[data-query-semantics]').nth(0)
  const second = page.locator('[data-query-semantics]').nth(1)
  await first.locator('summary').click()
  await second.locator('summary').click()
  await second.getByLabel('汇总与缺失规则').fill('乙本地未保存的规则')
  await first.getByRole('button', { name: '保存问数定义' }).click()
  const reload = second.getByRole('button', { name: '载入最新配置（放弃本地修改）' })
  await reload.waitFor({ state: 'visible' })
  check('同指标发生冲突时保留输入并禁止保存', await second.getByRole('button', { name: '保存问数定义' }).isDisabled()
    && await second.getByLabel('汇总与缺失规则').inputValue() === '乙本地未保存的规则')
  await reload.click()
  check('明确放弃后载入服务端配置并可继续保存', await second.getByLabel('汇总与缺失规则').inputValue() === '其他人更新的规则'
    && await second.getByRole('button', { name: '保存问数定义' }).isEnabled())
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义编辑恢复：关闭重开后旧保存响应不覆盖新详情', async () => {
  const state = makeState({ draft: draftOf({ draft: {
    metrics: [{ id: 'm1', name: '指标甲' }], metric_count: 1,
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  let release
  state.patch = body => new Promise(resolve => { release = () => resolve({ json: {
    ...clone(state.draft), revision: 8,
    metrics: [{ ...state.draft.metrics[0], semantic: body.operations[0].value }],
  } }) })
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-query-semantics]')
  await panel.locator('summary').click()
  await panel.getByLabel('汇总与缺失规则').fill('旧保存内容')
  await panel.getByRole('button', { name: '保存问数定义' }).click()
  await until(() => release)
  await page.locator(`[data-draft="${state.draft.id}"] [data-draft-toggle]`).click()
  state.draft = { ...state.draft, revision: 9, metrics: [{ id: 'm1', name: '新版本指标' }] }
  await expand(page, state.draft)
  release()
  await page.waitForTimeout(200)
  check('旧响应没有覆盖重开的新版本', (await panel.innerText()).includes('新版本指标'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义编辑恢复：慢网络起草轮询不并发堆积', async () => {
  const state = makeState({ draft: draftOf({ draft: { status: 'drafting' } }),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  const original = state.reply
  let requests = 0, active = 0, peak = 0
  state.reply = async (method, path, body, url) => {
    if (method === 'GET' && path === `/ledger/drafts/${state.draft.id}` && ++requests > 1) {
      active++; peak = Math.max(peak, active)
      await new Promise(resolve => setTimeout(resolve, 3000))
      active--
    }
    return original(method, path, body, url)
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  await page.waitForTimeout(8500)
  check('两轮以上慢请求仍最多一个在途', requests >= 3 && peak === 1, `requests=${requests}, peak=${peak}`)
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义编辑恢复：补充缺失指标候选可合并回原稿', async () => {
  const item = id => ({ id, name: id, kind: 'metric', status: 'definition_only', fields: {},
    dependencies: [], source: { document_id: 'doc', line: 1, quote: id } })
  const originalDraft = draftOf({ draft: { status: 'questions_open',
    metrics: [{ id: 'm1', name: '已有指标', source_definition_id: 'def1' }], metric_count: 1,
    selected_definition_ids: ['def1', 'def2'],
    semantic_inventory: { version: 1, items: [item('def1'), item('def2')], rules: [], warnings: [] },
  } })
  const state = makeState({ draft: clone(originalDraft),
    validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  const originalReply = state.reply
  let continued, merged, mergeAttempts = 0
  state.reply = async (method, path, body, url) => {
    if (method === 'POST' && path === `/ledger/drafts/${originalDraft.id}/continue`) {
      continued = body
      state.draft = { ...clone(originalDraft), id: 'd-candidate', title: '补充指标候选', revision: 3,
        metrics: [{ id: 'm2', name: '补充指标', source_definition_id: 'def2' }],
        origin: { continuation_from: { draft_id: originalDraft.id, revision: 7, merge_required: true } } }
      return { json: { draft_id: state.draft.id, status: state.draft.status, original_draft_id: originalDraft.id, merge_required: true } }
    }
    if (method === 'GET' && path === `/ledger/drafts/${originalDraft.id}` && state.draft.id !== originalDraft.id)
      return { json: { ...clone(originalDraft), revision: 8 } }
    if (method === 'POST' && path === '/ledger/drafts/d-candidate/merge') {
      mergeAttempts++
      if (mergeAttempts === 1) return { status: 409, json: { detail: '原稿已变化，请核对后重试' } }
      merged = body
      state.draft = { ...clone(originalDraft), revision: 9, metrics: [...originalDraft.metrics, ...state.draft.metrics] }
      return { json: { ...clone(state.draft), merged_candidate_id: 'd-candidate' } }
    }
    return originalReply(method, path, body, url)
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  let panel = page.locator('[data-draft-continuation]')
  await panel.getByLabel('补充说明').fill('使用已核实的产量字段')
  await panel.getByRole('button', { name: '继续处理未完成指标' }).click()
  await until(() => continued)
  check('继续请求包含基准版本和补充说明', continued?.expected_revision === 7 && continued?.guidance === '使用已核实的产量字段')
  await panel.getByRole('button', { name: '合并补充指标到原稿' }).waitFor({ state: 'visible' })
  await panel.getByRole('button', { name: '合并补充指标到原稿' }).click()
  await panel.getByText('原稿已变化，请核对后重试', { exact: false }).first().waitFor({ state: 'visible' })
  check('版本冲突保留候选和重试入口', state.draft.id === 'd-candidate'
    && await panel.getByRole('button', { name: '合并补充指标到原稿' }).isEnabled())
  await panel.getByRole('button', { name: '合并补充指标到原稿' }).click()
  await until(() => merged)
  check('合并采用原稿最新版本及候选版本', merged?.expected_revision === 8 && merged?.candidate_revision === 3)
  await until(async () => await page.locator('[data-query-semantics]').count() === 2)
  check('合并后回到保留已有指标的原稿', await page.locator('[data-query-semantics]').count() === 2
    && state.draft.metrics[0].name === '已有指标')
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('语义问数：定义清单保留未实现项，无数据源指标不能被选择', async () => {
  const item = (id, name, status) => ({ id, name, kind: 'metric', status, section: 'OEE 系列', formula: '原文公式',
    policy: '缺失不写零', fields: { 指标: name, 公式: '原文公式' }, dependencies: [],
    source: { document_id: 'doc', line: 8, quote: '| 原文公式 |' } })
  const state = makeState({ draft: draftOf({ draft: {
    status: 'path_pending', workflow_id: null, revision: null, card_id: null, metrics: [], entry_fields: [],
    semantic_inventory: { version: 1, note: '尚未绑定数据', rules: [], items: [
      item('day', '日 OEE', 'definition_only'), item('missing', '人均效率', 'unavailable'),
    ] },
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  const original = state.reply
  let implementation
  state.reply = async (method, p, body, url) => {
    if (p === '/datasources/names') return { json: [{ id: 'source', name: '合成测试库', enabled: true }] }
    if (method === 'POST' && p.endsWith('/implement')) {
      implementation = body
      return { status: 202, json: { draft_id: state.draft.id, status: 'drafting' } }
    }
    return original(method, p, body, url)
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-semantic-inventory]')
  check('默认不要求逐项勾选', await panel.locator('input[type="checkbox"]').count() === 0)
  await panel.getByRole('button', { name: '仅处理部分指标', exact: true }).click()
  check('无数据源定义保留但禁选', await panel.getByLabel('选择 人均效率', { exact: true }).isDisabled())
  await panel.getByLabel('选择 日 OEE', { exact: true }).check()
  await panel.getByLabel('用于实现的数据源').selectOption('source')
  await panel.getByRole('button', { name: '开始处理选中的 1 项' }).click()
  await until(() => implementation)
  check('只提交选中的定义和数据源', JSON.stringify(implementation) === JSON.stringify({ selected: ['day'], datasource_id: 'source' }))
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('自动处理：默认全部、无需手动分批，持续展示进度和缺口', async () => {
  const items = Array.from({ length: 13 }, (_, i) => ({ id: `metric-${i}`, name: `指标${i}`, kind: 'metric',
    status: 'definition_only', fields: {}, dependencies: [], source: { document_id: 'doc', line: i + 1, quote: '原文' } }))
  items.push({ ...items[0], id: 'missing', name: '无来源指标', status: 'unavailable' })
  const state = makeState({ draft: draftOf({ draft: {
    status: 'path_pending', datasource_id: null, workflow_id: null, revision: null, card_id: null,
    metrics: [], entry_fields: [], semantic_inventory: { version: 1, rules: [], items },
  } }), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  const original = state.reply
  let implementation
  state.reply = async (method, p, body, url) => {
    if (p === '/datasources/names') return { json: [{ id: 'source', name: '唯一已授权测试库', enabled: true }] }
    if (method === 'POST' && p.endsWith('/implement')) {
      implementation = body
      state.draft = { ...state.draft, status: 'drafting', selected_definition_ids: items.slice(0, 13).map(i => i.id),
        implementation_mode: 'all', implementation_progress: { total: 13, completed: 6, pending: 7, blocked: 0, active_names: ['指标6'] },
        definition_issues: [{ definition_id: 'missing', name: '无来源指标', reason: '原文注明无数据源' }] }
      return { status: 202, json: { draft_id: state.draft.id, status: 'drafting' } }
    }
    return original(method, p, body, url)
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-semantic-inventory]')
  check('默认清单没有勾选框', await panel.locator('input[type="checkbox"]').count() === 0)
  check('预告全部可实现项，排除无来源项', (await panel.innerText()).includes('将自动处理 13 项'))
  await panel.getByRole('button', { name: '仅处理部分指标', exact: true }).click()
  for (let i = 0; i < 7; i++) await panel.getByLabel(`选择 指标${i}`, { exact: true }).check()
  check('可选范围不受内部六项批次限制', await panel.getByRole('button', { name: '开始处理选中的 7 项' }).isEnabled())
  await panel.getByRole('button', { name: '恢复处理全部指标', exact: true }).click()
  await panel.getByRole('button', { name: '自动处理全部可实现指标', exact: true }).click()
  await until(() => implementation)
  check('默认提交全部模式，没有偷偷沿用手选范围', JSON.stringify(implementation) === '{"datasource_id":"source"}')
  await panel.locator('[data-implementation-progress]').waitFor()
  const text = await panel.innerText()
  check('持续进度说明已生成和待处理数', text.includes('已生成 6 项，待处理 7 项') && text.includes('正在处理：指标6'))
  check('单独说明待补充来源的指标', text.includes('另有 1 项'))
  check('不用用户再添加下一批', !text.includes('下一批') && !text.includes('最多选择 6'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('旧口径迁移：显示粒度缺口，按检查版本准备独立草稿', async () => {
  const state = makeState({ draft: draftOf(), validations: { records: [], summary: summaryOf({ verdict: 'none', status: null }) } })
  const original = state.reply
  let migration
  state.reply = async (method, p, body, url) => {
    if (p.endsWith('/migration') && method === 'GET') return { json: {
      revision: 7, metrics_total: 1, metrics_needing_work: 1, can_rebuild: true,
      note: '这是静态迁移检查，不代表查询结果已验证',
      metrics: [{ card_id: 'card', metric_id: 'm1', name: '日 OEE', query_count: 1,
        findings: [{ code: 'scalar_unproven', message: '最终取数未收敛为单行' }], guidance: '比率不能简单平均' }],
    } }
    if (p.endsWith('/migration') && method === 'POST') {
      migration = body
      return { status: 201, json: { draft_id: state.draft.id, status: 'path_pending' } }
    }
    return original(method, p, body, url)
  }
  const { page, errors } = await pageOn(state)
  await expand(page, state.draft)
  const panel = page.locator('[data-migration-panel]')
  await panel.getByRole('button', { name: '检查问数与取数粒度' }).click()
  await panel.getByText('共 1 项，1 项需处理').waitFor()
  await panel.locator('details summary').click()
  check('逐项显示粒度问题', await panel.getByText('最终取数未收敛为单行', { exact: true }).isVisible())
  check('说明静态检查不等于验证通过', (await panel.innerText()).includes('不代表查询结果已验证'))
  await panel.getByRole('button', { name: '从原文准备独立迁移草稿' }).click()
  await until(() => migration)
  check('迁移基于刚检查的版本，未整份覆盖旧图', JSON.stringify(migration) === '{"expected_revision":7}')
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

check('整个检查没有弹出原生对话框', dialogs.length === 0, dialogs.slice(0, 2).join('；'))
check('检查没有写库（兜底拦下的写请求）', states.every((s) => s.stray.length === 0),
  states.flatMap((s) => s.stray).slice(0, 4).join('、'))

await browser.close()
console.log(`\n${failed ? `✗ ${failed} 项没过` : '✓ 全部通过'}`)
process.exit(failed ? 1 : 0)
