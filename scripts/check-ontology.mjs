#!/usr/bin/env node
// AI 业务本体与问数跨源指标穿透（W3-b.4）的前端界面检查。
//
// 守的是五处核心交互面：
//   1. 数据目录「标准业务实体」视图（/data/catalog/:sourceId?view=entities）：顶部工具栏进入与返回不破坏
//      [data-catalog-view-tab] 固有的三标签（list / review / changes）；待确认（proposed）表列绑定可单项审阅确认；
//      新建标准业务实体时把跨源表列绑定与「数据源名=编码」成员映射原样提交给 POST /api/ontology/entities；
//   2. 口径草稿「业务本体与跨源关系」折叠面板（/ledger/drafts → OntologyPanel）：默认收起且展开前不请求
//      GET /api/ontology/workflows/:id；展开后渲染一跳本体拓扑 SVG 与反向表血缘；待确认关系可一键确认；新增
//      跨源上游驱动关系时向 PATCH /api/ledger/drafts/:id 提交 /links 操作并带上 expected_revision；
//   3. 问数异常一键跨源排查与 [[h:H1]] 数据源健康证据面板（/chat/:conversationId）：正文中 [[h:H1]] 片段点开后
//      在证据面板渲染 [data-ev-health]、判定徽标、核查表与异常明细；轮次底部渲染 [data-remedy-escalate]，点击后
//      向 POST /api/chat/turns 发送 { conversation_id, retry_of, mode: 'escalate' }；
//   4. 设置页「问数异常跨源排查」偏好（/settings/prefs）：[data-setting-auto-escalate] 从 prompt 切到 auto 后保存，
//      向 PUT /api/settings 提交 investigation.auto_escalate_on_anomaly = 'auto'；
//   5. 评测页本体穿透期望展示与「本体穿透贡献对照」预设（/evals/suites/:id）：题目行展示源健康核查、最少本体跳数与
//      诊断阶段期望；发起弹窗选 [data-eval-start-mode="ontology_tools"] 时收起单臂本体下拉，估算与发起请求体一致带上
//      preset: 'ontology_tools'。
//
// 所有 /api/ 请求均在浏览器路由层拦截伪造，不写任何真实数据库；未识别的写请求一律 409 并记入 stray，结束时断言为空。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 220)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
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
    for (const c of opened) {
      await c.unrouteAll?.({ behavior: 'ignoreErrors' }).catch(() => {})
      await c.close().catch(() => {})
    }
    opened.clear()
  }
}

const until = async (fn, ms = 7000, step = 100) => {
  const end = Date.now() + ms
  let v = await Promise.resolve().then(fn).catch(() => null)
  while (!v && Date.now() < end) {
    await new Promise((r) => setTimeout(r, step))
    v = await Promise.resolve().then(fn).catch(() => null)
  }
  return v
}
const clone = (x) => JSON.parse(JSON.stringify(x))
const textOf = async (loc) => ((await loc.textContent().catch(() => '')) ?? '').replace(/\s+/g, ' ').trim()

const ADMIN = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }
const NOW = new Date('2026-11-11T08:00:00Z').toISOString()

const SOURCES = [
  {
    id: 'ds-mes', name: 'mes_db', kind: 'sqlite', origin: 'manual', enabled: true,
    database: 'mes.db', options: {}, schema_cache: {}, table_count: 2, tools: ['db_query__mes_db'],
    created_at: NOW, updated_at: NOW,
  },
  {
    id: 'ds-erp', name: 'erp_db', kind: 'sqlite', origin: 'manual', enabled: true,
    database: 'erp.db', options: {}, schema_cache: {}, table_count: 1, tools: ['db_query__erp_db'],
    created_at: NOW, updated_at: NOW,
  },
]

const SOURCE_NAMES = SOURCES.map((s) => ({ id: s.id, name: s.name, kind: s.kind, enabled: true }))

const DEFAULT_SETTINGS = {
  run: {
    default_memory_scope: 'default',
    default_collection: 'default',
    confirm_dangerous_tools: true,
    tool_gate_provider: null,
    tool_gate_model: null,
    agent_max_steps: 100,
    agent_budget_tokens: null,
    agent_budget_usd: null,
  },
  investigation: { auto_escalate_on_anomaly: 'prompt' },
  apps: { on_demand_daily_limit: 20, user_personal_apps: true, share_links: true },
}

const browser = await chromium.launch({ executablePath: CHROME })
const allStray = []
const allDialogs = []
const allPageErrors = []

async function openPage(path, reply, { w = 1440, h = 920 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(12_000)
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem('agentlab.theme', 'dark')
      localStorage.setItem('agentlab_actor', '检查员甲')
    } catch { /* noop */ }
  })
  const page = await ctx.newPage()
  const calls = []
  const stray = []
  page.on('pageerror', (e) => allPageErrors.push(`${path}: ${e.message}`))
  page.on('dialog', (d) => { allDialogs.push(`${path}: ${d.message()}`); void d.dismiss() })

  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const p = decodeURIComponent(url.pathname.replace(/^\/api/, ''))
    let body = null
    try { body = req.postDataJSON() } catch { body = null }
    let rawBody = null
    try { rawBody = req.postDataBuffer()?.toString('utf8') ?? req.postData() ?? null } catch { rawBody = null }
    calls.push({ method: req.method(), path: p, body, rawBody, search: url.search })

    if (p === '/auth/me') return route.fulfill({ json: ADMIN })
    if (p === '/health') return route.fulfill({ json: { status: 'ok' } })

    const custom = await reply(req.method(), p, body, url, rawBody)
    if (custom) {
      if (typeof custom.body === 'string') {
        return route.fulfill({
          status: custom.status ?? 200,
          contentType: custom.contentType ?? 'text/plain; charset=utf-8',
          headers: custom.headers,
          body: custom.body,
        })
      }
      return route.fulfill({ status: custom.status ?? 200, json: custom.json ?? {} })
    }

    // 应用外壳启动所需的只读目录兜底，单起 Vite 时也不阻塞外壳渲染
    if (req.method() === 'GET') {
      if (p === '/providers') return route.fulfill({ json: [{ id: 'p-mock', name: '演示模型', kind: 'mock', enabled: true, default_model: 'mock-gpt', models: [{ id: 'mock-gpt', label: 'mock-gpt' }] }] })
      if (p === '/tools') return route.fulfill({ json: [] })
      if (p === '/skills') return route.fulfill({ json: [] })
      if (p === '/kb/collections') return route.fulfill({ json: [{ collection: 'default', documents: 1, chunks: 2 }] })
      if (p === '/kb/documents') return route.fulfill({ json: [] })
      if (p === '/memory/scopes') return route.fulfill({ json: [{ scope: 'default', count: 0 }] })
      if (p === '/workflows') return route.fulfill({ json: [] })
      if (p === '/datasources') return route.fulfill({ json: SOURCES })
      if (p === '/datasources/names') return route.fulfill({ json: SOURCE_NAMES })
      if (p === '/approvals' || p.startsWith('/runs/approvals')) return route.fulfill({ json: [] })
      if (p === '/inbox/counts') return route.fulfill({ json: { total: 0, approvals: 0, signoffs: 0, verifications: 0, recalls: 0 } })
      if (p === '/settings') return route.fulfill({ json: DEFAULT_SETTINGS })
      if (p === '/conversations') return route.fulfill({ json: [] })
      if (p === '/conversations/trash') return route.fulfill({ json: [] })
      if (p.startsWith('/runs')) return route.fulfill({ json: [] })
      return route.fulfill({ json: {} })
    }

    const tag = `${req.method()} ${p}`
    stray.push(tag)
    allStray.push(tag)
    return route.fulfill({ status: 409, json: { detail: `未预期的写请求：${tag}` } })
  })

  await page.goto(`${WEB}${path}`, { waitUntil: 'domcontentloaded' })
  return { ctx, page, calls, stray }
}

// ================================================================ 1 数据目录：标准业务实体视图

await section('1. 数据目录：标准业务实体视图切换、待确认绑定审阅与新建跨源编码实体', async () => {
  let entities = [
    {
      id: 'ent-line',
      key: 'production_line',
      label: '生产产线',
      description: '制造执行与结算系统统一的产线粒度',
      aliases: ['产线', '机台线'],
      version: 2,
      bindings: [
        {
          id: 'bind-mes', source_id: 'ds-mes', source_name: 'mes_db',
          table_name: 'dwd_workorder_daily', column_name: 'line_id',
          status: 'confirmed', source: 'human', note: '主产线编码',
        },
        {
          id: 'bind-erp', source_id: 'ds-erp', source_name: 'erp_db',
          table_name: 'dwd_settlement_daily', column_name: 'pl_code',
          status: 'proposed', source: 'inferred', note: '表剖析推断',
        },
      ],
      members: [
        { value: 'L1', label: '一号产线', aliases: ['一车间一线'], source_codes: { mes_db: 'LINE_01', erp_db: 'PL01' } },
      ],
      caliber_params: [],
    },
  ]

  const catalogPayload = {
    tables: [
      {
        table_name: 'dwd_workorder_daily', qualified: 'dwd_workorder_daily', is_view: false, in_schema: true,
        label: '工单日汇总', label_status: 'confirmed', kind: 'fact',
        counts: { confirmed: 5, proposed: 0, verified: 0, total: 5 }, relations: 0,
        usage: 1, version: 1, updated_at: NOW, updated_by: '检查员甲', last_draft: null,
      },
      {
        table_name: 'dwd_material_batch', qualified: 'dwd_material_batch', is_view: false, in_schema: true,
        label: '物料批次表', label_status: 'confirmed', kind: 'fact',
        counts: { confirmed: 4, proposed: 0, verified: 0, total: 4 }, relations: 0,
        usage: 0, version: 1, updated_at: NOW, updated_by: '检查员甲', last_draft: null,
      },
    ],
    system_notes: false,
    schema_note: null,
    schema_truncated: false,
    schema_total: 2,
    guidance: null,
    active_job: null,
  }

  const { page, calls } = await openPage('/data/catalog/ds-mes', async (method, p, body) => {
    if (method === 'GET' && p === '/datasources/ds-mes/catalog') return { json: catalogPayload }
    if (method === 'GET' && p === '/datasources/ds-mes/changes') {
      return { json: { items: [], counts: { total: 0, open: 0, by_severity: { action: 0, notice: 0, info: 0 } } } }
    }
    if (method === 'GET' && p === '/datasources/ds-mes/background-jobs') return { json: { items: [] } }
    if (method === 'GET' && p === '/datasources/ds-mes/catalog/draft-jobs/active') return { json: { job: null } }
    if (method === 'GET' && p === '/datasources/ds-mes/catalog/profile-jobs/active') return { json: { job: null } }
    if (method === 'GET' && p === '/ontology/entities') return { json: { items: clone(entities) } }
    if (method === 'POST' && p === '/ontology/entities/ent-line/review') {
      entities = entities.map((ent) => ent.id !== 'ent-line' ? ent : {
        ...ent,
        version: ent.version + 1,
        bindings: ent.bindings.map((b) => (b.id === body.item_id ? { ...b, status: body.status } : b)),
      })
      return { json: clone(entities[0]) }
    }
    if (method === 'POST' && p === '/ontology/entities') {
      const created = {
        id: 'ent-region',
        version: 1,
        key: body.key,
        label: body.label,
        description: body.description ?? '',
        aliases: body.aliases ?? [],
        bindings: (body.bindings ?? []).map((b, i) => ({ id: `b-new-${i}`, ...b })),
        members: body.members ?? [],
        caliber_params: body.caliber_params ?? [],
      }
      entities = [...entities, created]
      return { json: clone(created) }
    }
    return null
  })

  await page.locator('[data-catalog-page="ds-mes"]').waitFor({ state: 'visible' })
  await page.locator('[data-catalog-view-tab="list"]').waitFor({ state: 'visible' })
  const tabsBefore = await page.locator('[data-catalog-view-tab]').evaluateAll(
    (els) => els.map((el) => el.getAttribute('data-catalog-view-tab')),
  )
  check('表目录左栏保持固有三标签 list/review/changes', JSON.stringify(tabsBefore) === JSON.stringify(['list', 'review', 'changes']), JSON.stringify(tabsBefore))

  await page.locator('[data-catalog-entities]').click()
  await page.locator('[data-catalog-entities-view]').waitFor({ state: 'visible' })
  check('地址切换至 ?view=entities', new URL(page.url()).searchParams.get('view') === 'entities', page.url())

  await page.locator('[data-entity-item="production_line"]').waitFor({ state: 'visible' })
  check('列表标出待确认绑定数量', await page.locator('[data-entity-pending="1"]').isVisible())

  await page.locator('[data-entity-confirm="bind-erp"]').click()
  const reviewCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/ontology/entities/ent-line/review'))
  check(
    '单项确认绑定请求携带 target_kind=binding、item_id 与基准 version',
    reviewCall?.body?.target_kind === 'binding'
      && reviewCall?.body?.item_id === 'bind-erp'
      && reviewCall?.body?.status === 'confirmed'
      && reviewCall?.body?.version === 2,
    JSON.stringify(reviewCall?.body),
  )
  await until(async () => (await page.locator('[data-entity-pending]').count()) === 0)
  check('确认后待确认角标清零', (await page.locator('[data-entity-pending]').count()) === 0)

  await page.locator('[data-entity-new]').click()
  await page.locator('[data-entity-detail="__new__"]').waitFor({ state: 'visible' })
  await page.locator('[data-entity-key-input]').fill('sales_region')
  await page.locator('[data-entity-label-input]').fill('销售大区')
  await page.getByLabel('绑定 1 表名').fill('dwd_workorder_daily')
  await page.getByLabel('绑定 1 列名').fill('region_code')

  await page.locator('[data-entity-add-member]').click()
  await page.getByLabel('成员 1 标准值').fill('R1')
  await page.getByLabel('成员 1 标准名称').fill('华东大区')
  await page.getByLabel('成员 1 跨源编码').fill('mes_db=EAST_01，erp_db=RG01')

  await page.locator('[data-entity-save]').click()
  const createCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/ontology/entities'))
  check(
    '新建标准实体提交完整表列绑定与跨源成员编码映射',
    createCall?.body?.key === 'sales_region'
      && createCall?.body?.label === '销售大区'
      && createCall?.body?.bindings?.[0]?.table_name === 'dwd_workorder_daily'
      && createCall?.body?.bindings?.[0]?.column_name === 'region_code'
      && createCall?.body?.members?.[0]?.source_codes?.mes_db === 'EAST_01'
      && createCall?.body?.members?.[0]?.source_codes?.erp_db === 'RG01',
    JSON.stringify(createCall?.body),
  )

  await page.locator('[data-entity-back]').click()
  await page.locator('[data-catalog-view-tab="list"]').waitFor({ state: 'visible' })
  const tabsAfter = await page.locator('[data-catalog-view-tab]').evaluateAll(
    (els) => els.map((el) => el.getAttribute('data-catalog-view-tab')),
  )
  check('返回表目录后左栏仍保持固有三标签', JSON.stringify(tabsAfter) === JSON.stringify(['list', 'review', 'changes']), JSON.stringify(tabsAfter))
})

// ================================================================ 2 口径草稿：业务本体面板（OntologyPanel）

await section('2. 口径草稿：懒加载工作流本体、一跳拓扑与表血缘 SVG、确认建议关系与新增 /links 提交', async () => {
  let currentDraft = {
    id: 'd-mes',
    title: '产线良率口径',
    status: 'ready',
    stage: 'review',
    metric_count: 1,
    question_count: 0,
    workflow_id: 'wf-mes',
    workflow_name: '产线良率口径',
    created_at: NOW,
    error: null,
    card_id: 'mes_card',
    cards: [{ id: 'mes_card', title: '产线良率口径' }],
    revision: 7,
    version: 7,
    metrics: [
      {
        id: 'fpy',
        name: '一次良率',
        unit: '%',
        expression: "cell(nodes.fetch, 0, 'fpy')",
        grain: '产线×日',
        links: [
          {
            id: 'lk-1',
            kind: 'reconciles_with',
            target: { workflow_id: 'wf-erp', card_id: 'erp_card', metric_id: 'settled_yield' },
            entity_key: 'production_line',
            rel_tol: 0.02,
            status: 'proposed',
            source: 'inferred',
            rationale: '同产线完工入库良率镜像对账',
          },
        ],
      },
    ],
    entry_fields: [{ name: 'dt', type: 'string', required: true }],
    trial: { run_id: 'run-t1', status: 'succeeded', version: 7 },
    binding: {
      editable_fields: [],
      current: {},
      status: 'aligned',
      reason: '定义和取数 SQL 同步',
      fix: 'none',
      metrics: ['fpy'],
      checked: true,
      metric_values: { fpy: {} },
      binding_fingerprints: { fetch: 'fp-mes' },
      nodes: [],
      synced: true,
    },
    field_baseline: {
      status: 'known', artifact_id: 'bl-mes', version: 1, content_hash: 'bl-mes',
      card_id: 'mes_card', workflow_id: 'wf-mes', accepted_revision: 1, accepted_at: NOW,
      metric_ids: ['fpy'], field_count: 1,
    },
  }

  const wfOntology = {
    workflow_id: 'wf-mes',
    version: 7,
    issues: [],
    cards: [
      {
        card_id: 'mes_card',
        caliber: '产线良率口径',
        metrics: [
          {
            metric_id: 'fpy',
            name: '一次良率',
            links: currentDraft.metrics[0].links,
            lineage: [{ source: 'mes_db', tables: ['dwd_workorder_daily'] }],
          },
        ],
      },
    ],
  }

  const ontGraph = {
    entities: [{ id: 'ent-line', key: 'production_line', label: '生产产线', version: 2, bindings: [], members: [], caliber_params: [] }],
    cards: [
      {
        workflow_id: 'wf-erp', workflow_name: '财务结算良率', version: 3, card_id: 'erp_card', caliber: '财务结算良率',
        metrics: [{ metric_id: 'settled_yield', name: '入库结算良率' }],
      },
      {
        workflow_id: 'wf-eqp', workflow_name: '设备告警口径', version: 2, card_id: 'eqp_card', caliber: '设备告警口径',
        metrics: [{ metric_id: 'alarm_rate', name: '温控告警率' }],
      },
    ],
    edges: [],
  }

  const { page, calls } = await openPage('/ledger/drafts', async (method, p, body) => {
    if (method === 'GET' && p === '/ledger/drafts') return { json: { drafts: [clone(currentDraft)] } }
    if (method === 'GET' && p === '/ledger/drafts/d-mes') return { json: clone(currentDraft) }
    if (method === 'GET' && p === '/ledger/drafts/d-mes/trial') return { status: 404, json: { detail: '未试运行' } }
    if (method === 'GET' && p === '/ledger/drafts/d-mes/imports') return { json: { runs: [] } }
    if (method === 'GET' && p === '/ledger/drafts/d-mes/validations') {
      return {
        json: {
          draft_id: 'd-mes',
          field_baseline: currentDraft.field_baseline,
          records: [],
          summary: {
            verdict: 'none', status: 'none', covers_current: false, record_id: null, review: null,
            counts: { total: 0, covering: 0, matched: 0, mismatched: 0, observed: 0, unverified: 0, failed: 0 },
            reason: '尚无验证记录', semantic_gaps: [], method: { version: 1, note: '原值比较' },
          },
          method: { version: 1, note: '原值比较' },
        },
      }
    }
    if (method === 'GET' && p === '/ontology/entities') return { json: { items: ontGraph.entities } }
    if (method === 'GET' && p === '/ontology/graph') return { json: ontGraph }
    if (method === 'GET' && p === '/ontology/workflows/wf-mes') return { json: wfOntology }
    if (method === 'PATCH' && p === '/ledger/drafts/d-mes') {
      const linkOp = (body.operations ?? []).find((op) => op.path === '/links')
      const nextRev = currentDraft.revision + 1
      if (linkOp) {
        currentDraft = {
          ...currentDraft,
          revision: nextRev,
          version: nextRev,
          metrics: currentDraft.metrics.map((m) => (m.id === linkOp.metric_id ? { ...m, links: linkOp.value } : m)),
        }
        wfOntology.cards[0].metrics[0].links = linkOp.value
      }
      return {
        json: {
          ...clone(currentDraft),
          receipt: {
            applied: true, classification: 'definition', card: 'mes_card', metrics: ['fpy'],
            operations: body.operations ?? [], revision: nextRev,
            definition_hash: { before: 'dh-1', after: 'dh-2', changed: true },
            validation: { invalidated: true, scope: 'card', metrics: ['fpy'], reason: '定义已更新', next: '重新试运行' },
          },
        },
      }
    }
    return null
  })

  const draftToggle = page.locator('[data-draft="d-mes"] [data-draft-toggle]')
  await draftToggle.waitFor({ state: 'visible' })
  await draftToggle.click()
  const ontPanel = page.locator('[data-ontology-panel]')
  await ontPanel.waitFor({ state: 'visible' })

  const wfCallsBeforeExpand = calls.filter((c) => c.path === '/ontology/workflows/wf-mes').length
  check('展开本体折叠面板前不请求工作流本体接口', wfCallsBeforeExpand === 0, `calls=${wfCallsBeforeExpand}`)

  await ontPanel.locator('summary').click()
  await until(() => calls.some((c) => c.path === '/ontology/workflows/wf-mes'))
  await page.locator('[data-ontology-svg]').waitFor({ state: 'visible' })

  const lineageText = await textOf(page.locator('[data-ontology-lineage="fpy"]'))
  check('展示指标反向物理表血缘', lineageText.includes('mes_db（dwd_workorder_daily）'), lineageText)
  const svgText = await textOf(page.locator('[data-ontology-svg]'))
  check('一跳本体拓扑 SVG 渲染镜像对账边与本卡指标', svgText.includes('跨源镜像对账') && svgText.includes('一次良率'), svgText)

  await page.locator('[data-ontology-link-confirm="lk-1"]').click()
  const confirmPatch = await until(() => calls.find((c) => c.method === 'PATCH' && c.path === '/ledger/drafts/d-mes'))
  check(
    '确认建议态本体关系走 PATCH /links 并携带基准版本 v7',
    confirmPatch?.body?.expected_revision === 7
      && confirmPatch?.body?.operations?.[0]?.path === '/links'
      && confirmPatch?.body?.operations?.[0]?.value?.[0]?.status === 'confirmed',
    JSON.stringify(confirmPatch?.body),
  )

  await page.locator('[data-ontology-kind-select]').selectOption('upstream_driver')
  await page.locator('[data-ontology-target-picker]').selectOption('wf-eqp:eqp_card:alarm_rate')
  await page.locator('[data-ontology-entity-select]').selectOption('production_line')
  await page.locator('[data-ontology-add-submit]').click()

  const addPatch = await until(() => {
    const patches = calls.filter((c) => c.method === 'PATCH' && c.path === '/ledger/drafts/d-mes')
    return patches.length >= 2 ? patches[1] : null
  })
  const addedLink = addPatch?.body?.operations?.[0]?.value?.[1]
  check(
    '新增跨源上游驱动关系携带最新基准版本 v8 与实体对齐键',
    addPatch?.body?.expected_revision === 8
      && addedLink?.kind === 'upstream_driver'
      && addedLink?.target?.workflow_id === 'wf-eqp'
      && addedLink?.target?.card_id === 'eqp_card'
      && addedLink?.target?.metric_id === 'alarm_rate'
      && addedLink?.entity_key === 'production_line'
      && addedLink?.status === 'confirmed',
    JSON.stringify(addedLink),
  )
})

// ================================================================ 3 问数据：[[h:H1]] 源健康证据面板与异常一键排查

await section('3. 问数据：点击 [[h:H1]] 打开源健康核查证据链，点击异常排查按钮发起 mode=escalate', async () => {
  const healthText = 'ERP 结算库滞后 3 天，MES 正常'
  const fullMarkdown = `一号产线一次良率为 82.0%。源健康核查显示：${healthText}。`
  const reportDoc = {
    schema: 'agentlab.report/1',
    run_id: 'run-ont-1',
    node_id: 'write',
    markdown: fullMarkdown,
    source: '一号产线一次良率为 82.0%。源健康核查显示：[[h:H1]]。',
    catalog: {
      'h:H1': {
        alias: 'H1',
        kind: 'source_health',
        eid: 'ev:health:8f12ab34cd56ef78',
        node_id: 'investigate',
        verdict: 'stale',
        phrase: healthText,
        sources: ['erp_db', 'mes_db'],
      },
    },
    blocks: [
      {
        type: 'paragraph',
        units: [
          {
            id: 'u0',
            kind: 'claim',
            span: [0, fullMarkdown.length],
            cites: ['h:H1'],
            see: [],
            segments: [
              { id: 's0', kind: 'text', text: '一号产线一次良率为 82.0%。源健康核查显示：', state: 'neutral', span: [0, 22] },
              {
                id: 's_h1',
                kind: 'source_health',
                ref: 'h:H1',
                eid: 'ev:health:8f12ab34cd56ef78',
                text: healthText,
                state: 'deterministic',
                span: [22, 22 + healthText.length],
              },
              { id: 's2', kind: 'text', text: '。', state: 'neutral', span: [22 + healthText.length, fullMarkdown.length] },
            ],
          },
        ],
      },
    ],
    violations: [],
  }

  const savedTurn = {
    id: 'turn-ont-1',
    seq: 1,
    question: '上周一号产线一次良率为什么跌破 85%？',
    answer: fullMarkdown,
    explanation: '已查询产线良率口径并检查数据源同步状态',
    graph: {
      nodes: [
        { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'question' }] } } },
        { id: 'investigate', type: 'agent', position: { x: 0, y: 100 }, data: { label: '排查', config: { max_steps: 8 } } },
        { id: 'out', type: 'output', position: { x: 0, y: 200 }, data: { label: '成果', config: {} } },
      ],
      edges: [{ source: 'in', target: 'investigate' }, { source: 'investigate', target: 'out' }],
    },
    run_id: 'run-ont-1',
    status: 'done',
    error: '',
    created_at: NOW,
    meta: { evidence: true, runStatus: 'succeeded', runClass: 'exploratory', ms: 1800 },
    review: {
      verdict: 'ok',
      severity: 'ok',
      signals: [{ kind: 'metric_contract_warn', message: '一次良率低于下限 0.85' }],
      anomaly_escalation: {
        available: true,
        auto_escalate: false,
        triggers: ['metric_contract_warn'],
        reason: '指标「一次良率」触发阈值告警（82.0% < 85.0%），可沿已确认业务本体继续核查关联数据源与上下游驱动。',
        button_label: '指标触发异常，沿本体排查关联数据源',
      },
    },
  }

  const { page, calls } = await openPage('/chat/conv-ont', async (method, p) => {
    if (method === 'GET' && p === '/conversations') {
      return {
        json: [{
          id: 'conv-ont', title: savedTurn.question, kind: 'chat', workflow_id: null, archived: false,
          created_at: NOW, last_active_at: NOW, turn_count: 1, last_question: savedTurn.question,
          last_status: 'done', last_run_id: 'run-ont-1',
        }],
      }
    }
    if (method === 'GET' && p === '/conversations/conv-ont') {
      return {
        json: {
          id: 'conv-ont', title: savedTurn.question, kind: 'chat', workflow_id: null, archived: false,
          created_at: NOW, last_active_at: NOW, turns: [clone(savedTurn)],
        },
      }
    }
    if (method === 'GET' && p === '/runs/run-ont-1') {
      return {
        json: {
          id: 'run-ont-1', workflow_id: null, workflow_name: '问数据', status: 'succeeded',
          run_class: 'exploratory', origin_kind: 'chat', created_at: NOW, started_at: NOW, finished_at: NOW,
          input: { question: savedTurn.question },
          output: {
            answer: fullMarkdown,
            _evidence: { report_node: 'write', doc_artifact: 'art-ont-doc-1', fields: ['answer'] },
          },
          usage: {},
        },
      }
    }
    if (method === 'GET' && p === '/artifacts/art-ont-doc-1') {
      return { json: { id: 'art-ont-doc-1', content: reportDoc } }
    }
    if (method === 'GET' && p === '/runs/run-ont-1/ledger') {
      return { json: { status: 'ready', summary: null, actions: [], labels: {}, metrics: [] } }
    }
    if (method === 'GET' && p === '/runs/run-ont-1/evidence') {
      return {
        json: {
          run_id: 'run-ont-1', mode: 'cited',
          seal: { sealed: true, ok: true, hash: 'seal-ont-1' },
          reports: [{ node_id: 'write', doc: reportDoc }],
        },
      }
    }
    if (method === 'GET' && p === '/runs/run-ont-1/evidence/segments/s_h1') {
      return {
        json: {
          run_id: 'run-ont-1',
          segment: reportDoc.blocks[0].units[0].segments[1],
          unit: reportDoc.blocks[0].units[0],
          entry: reportDoc.catalog['h:H1'],
          seal: { sealed: true, ok: true, hash: 'seal-ont-1' },
          chain: [
            {
              step: 'source_health',
              alias: 'H1',
              verdict: 'stale',
              phrase: healthText,
              sources: ['erp_db', 'mes_db'],
              eid_ok: true,
              hash_ok: true,
              render_ok: true,
              tables: [
                {
                  source: 'erp_db', table: 'dwd_settlement_daily', status: 'stale',
                  date_column: 'biz_date', data_range: ['2026-10-01', '2026-10-28'], gaps: 2,
                },
                {
                  source: 'mes_db', table: 'dwd_workorder_daily', status: 'healthy',
                  date_column: 'work_date', data_range: ['2026-10-01', '2026-10-31'], gaps: 0,
                },
              ],
              issues: [
                { code: 'source_lag', message: 'erp_db.dwd_settlement_daily 最新业务日停在 2026-10-28（滞后 3 天）' },
              ],
            },
            { step: 'health_seal', sealed: true, ok: true },
          ],
        },
      }
    }
    if (method === 'POST' && p === '/chat/turns') {
      return {
        json: {
          turn: { ...clone(savedTurn), status: 'running' },
          job: { id: 'job-esc-1', turn_id: 'turn-ont-1', status: 'running', phase: 'planning', attempt_seq: 1 },
        },
      }
    }
    if (method === 'GET' && p === '/chat/turns/turn-ont-1/stream') {
      return { json: [] }
    }
    return null
  })

  const seg = page.locator('[data-seg="s_h1"]')
  await seg.waitFor({ state: 'visible' })
  check('报告正文渲染 [[h:H1]] 可点击片段', (await textOf(seg)) === healthText, await textOf(seg))

  await seg.click()
  const evHealth = page.locator('[data-ev-health="H1"]')
  await evHealth.waitFor({ state: 'visible' })
  check(
    '证据面板展示 H1 滞后判定徽标、核查表断档数与问题说明',
    (await page.locator('[data-ev-health-verdict="stale"]').isVisible())
      && (await textOf(page.locator('[data-ev-health-phrase]'))) === healthText
      && (await textOf(page.locator('[data-ev-health-table="erp_db.dwd_settlement_daily"]'))).includes('断档 2 处')
      && (await textOf(page.locator('[data-ev-health-issues]'))).includes('滞后 3 天'),
    await textOf(evHealth),
  )

  await page.keyboard.press('Escape')
  const escalateBtn = page.locator('[data-remedy-escalate]')
  await escalateBtn.waitFor({ state: 'visible' })
  check(
    '轮次底部展示异常升级原因与一键跨源排查按钮',
    (await textOf(page.locator('[data-remedy-escalation-reason]'))).includes('触发阈值告警')
      && (await textOf(escalateBtn)).includes('沿本体排查关联数据源'),
    await textOf(escalateBtn),
  )

  await escalateBtn.click()
  const turnCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/chat/turns'))
  check(
    '点击异常排查按钮向 POST /api/chat/turns 发送 mode=escalate 与原轮次 retry_of',
    turnCall?.body?.conversation_id === 'conv-ont'
      && turnCall?.body?.retry_of === 'turn-ont-1'
      && turnCall?.body?.mode === 'escalate',
    JSON.stringify(turnCall?.body),
  )
})

// ================================================================ 4 设置页：问数异常自动跨源排查偏好

await section('4. 设置页：切换问数异常自动跨源排查策略并保存至 PUT /api/settings', async () => {
  let currentSettings = clone(DEFAULT_SETTINGS)

  const { page, calls } = await openPage('/settings/prefs', async (method, p, body) => {
    if (method === 'GET' && p === '/settings') return { json: clone(currentSettings) }
    if (method === 'GET' && p === '/judge/spend') return { json: { today_usd: 0, daily_max_usd: 2, unpriced_calls: 0 } }
    if (method === 'GET' && p === '/verify/spend') return { json: { today_usd: 0, daily_max_usd: 2, unpriced_calls: 0 } }
    if (method === 'GET' && p === '/evals/spend') return { json: { today_usd: 0, daily_max_usd: 5, unpriced_calls: 0 } }
    if (method === 'GET' && p === '/settings/concurrency') {
      return { json: { limits: { runs: 4, chat_turns: 4, eval_cases: 2 }, active: { runs: 0, chat_turns: 0, eval_cases: 0 } } }
    }
    if (method === 'PUT' && p === '/settings') {
      const incoming = body?.values ?? body ?? {}
      currentSettings = {
        ...currentSettings,
        ...incoming,
        investigation: { ...currentSettings.investigation, ...(incoming.investigation ?? {}) },
      }
      return { json: clone(currentSettings) }
    }
    return null
  })

  const select = page.locator('[data-setting-auto-escalate]')
  await select.waitFor({ state: 'visible' })
  check('初始策略为 prompt（默认提示一键排查）', (await select.inputValue()) === 'prompt', await select.inputValue())

  await select.selectOption('auto')
  const prefsBar = page.locator('[data-prefs-bar]')
  await prefsBar.waitFor({ state: 'visible' })
  check('未保存提示栏列出「问数异常跨源排查」', (await textOf(prefsBar)).includes('问数异常跨源排查'), await textOf(prefsBar))

  await prefsBar.locator('button.btn-primary').click()
  const saveCall = await until(() => calls.find((c) => c.method === 'PUT' && c.path === '/settings'))
  const savedInv = saveCall?.body?.values?.investigation ?? saveCall?.body?.investigation
  check(
    '保存偏好向 PUT /api/settings 提交 investigation.auto_escalate_on_anomaly=auto',
    savedInv?.auto_escalate_on_anomaly === 'auto',
    JSON.stringify(saveCall?.body),
  )
})

// ================================================================ 5 评测页：本体穿透期望展示与 ontology_tools 贡献对照预设

await section('5. 评测页：题目行渲染本体穿透期望，发起弹窗选择 ontology_tools 预设估算并启动', async () => {
  const budget = { max_parallel: 2, per_case_timeout_s: 900, per_run_max_usd: 2, daily_max_usd: 5 }
  const suiteDetail = {
    id: 'suite-ont',
    name: '三源制造跨源指标穿透评测集',
    description: '验证源健康一票否决、镜像对账与上游驱动穿透',
    timezone: 'Asia/Shanghai',
    default_variant: {},
    budget: {},
    effective_budget: budget,
    archived: false,
    version: 1,
    pending_upgrades: 0,
    created_by: '检查员甲',
    created_at: NOW,
    updated_at: NOW,
    counts: { cases: 1, enabled: 1 },
    schedules: [],
    cases: [
      {
        id: 'case-ont-1',
        suite_id: 'suite-ont',
        seq: 1,
        kind: 'ask',
        question: '为什么 10 月下旬一号产线结算良率与工单一次良率出现背离？',
        datasource_ids: ['ds-mes', 'ds-erp'],
        datasources: [{ id: 'ds-mes', name: 'mes_db' }, { id: 'ds-erp', name: 'erp_db' }],
        workflow_id: null,
        workflow_version: null,
        input_template: {},
        history: null,
        expectations: [
          {
            id: 'e-diag',
            kind: 'investigation',
            required: true,
            require_source_health: true,
            min_ontology_hops: 1,
            diagnosis_stage: 'ontology_penetration',
          },
        ],
        gold: { kind: 'sql', datasource_id: 'ds-mes', sql: 'SELECT 0.94 AS fpy' },
        gold_source: { id: 'ds-mes', name: 'mes_db' },
        tags: ['跨源对账'],
        enabled: true,
        archived: false,
        superseded_by: null,
        version: 1,
        case_hash: 'ch-ont-1',
        trial: { status: 'ok', code: null, reason: null, as_of: NOW, run_ids: ['r-g1'], at: NOW, by: '检查员甲' },
        upgrade: null,
        created_by: '检查员甲',
        created_at: NOW,
        updated_at: NOW,
      },
    ],
  }

  const { page, calls } = await openPage('/evals/suites/suite-ont', async (method, p, body) => {
    if (method === 'GET' && p === '/evals/suites/suite-ont') return { json: suiteDetail }
    if (method === 'GET' && p === '/evals/suites/suite-ont/evaluations') return { json: { evaluations: [] } }
    if (method === 'GET' && p === '/evals/catalog-snapshots') return { json: { snapshots: [] } }
    if (method === 'POST' && p === '/evals/suites/suite-ont/evaluations/estimate') {
      const isPreset = body?.preset === 'ontology_tools'
      const armNames = isPreset ? ['本体穿透开', '本体穿透关'] : [null]
      return {
        json: {
          arms: armNames.map((arm) => ({
            label: arm,
            variant: {
              assistant: { provider: null, model: null }, writer: { provider: null, model: null },
              judge: false, catalog: { mode: 'live', snapshot: null },
              analysis: { tools: 'on' }, ontology: { tools: arm === '本体穿透关' ? 'off' : 'on' },
              repeats: isPreset ? 3 : 1, label: arm,
            },
            models: { assistant: 'mock-gpt', writer: null, judge: false },
            text: arm ? `${arm} · 助手 mock-gpt · 当前目录 · 每题 3 次` : '助手 mock-gpt · 当前目录 · 每题 1 次',
          })),
          cases: 1,
          repeats: isPreset ? 3 : 1,
          estimate: {
            items: isPreset ? 6 : 1,
            per_item_usd: 0.014,
            total_usd: isPreset ? 0.084 : 0.014,
            basis: 'ev-prev',
            note: '按这个题集上一次评测的每题平均花费估算',
          },
          budget,
          parallel: 2,
          spend: { usd: 0.04, calls: 2, unpriced_calls: 0, daily_max_usd: 5 },
        },
      }
    }
    if (method === 'POST' && p === '/evals/suites/suite-ont/evaluations') {
      return {
        json: {
          pair_id: 'pair-ont-1',
          evaluations: [
            { id: 'ev-ont-on', suite_id: 'suite-ont', status: 'queued', arm: '本体穿透开' },
            { id: 'ev-ont-off', suite_id: 'suite-ont', status: 'queued', arm: '本体穿透关' },
          ],
        },
      }
    }
    if (method === 'GET' && p === '/evals/evaluations/ev-ont-on') {
      return {
        json: {
          id: 'ev-ont-on', suite_id: 'suite-ont', status: 'queued', trigger: 'manual', schedule_id: null,
          variant: {
            assistant: { provider: null, model: null }, writer: { provider: null, model: null },
            judge: false, catalog: { mode: 'live', snapshot: null },
            analysis: { tools: 'on' }, ontology: { tools: 'on' }, repeats: 3, label: '本体穿透开',
          },
          resolved_models: { assistant: 'mock-gpt', writer: null, judge: false },
          variant_text: '本体穿透开：助手 mock-gpt · 当前目录 · 分析工具开 · 每题 3 次',
          catalog_snapshot: null, as_of: NOW, repeats: 3, fingerprint: { ontology_tools: 'on' },
          pair_id: 'pair-ont-1', arm: '本体穿透开', baseline_id: null, pinned: false, budget,
          counts: { total: 3, done: 0, passed: 0, failed: 0 }, usage: {}, summary: null,
          stop_reason: null, error: null, created_by: '检查员甲', created_at: NOW,
          started_at: null, finished_at: null, cleaned_at: null, running: true, current: [],
          actions: { stop: true, retry: false, rescore: false, pin: false, unpin: false, open: 0, cleaned: false },
        },
      }
    }
    if (method === 'GET' && p === '/evals/evaluations/ev-ont-on/results') return { json: { evaluation_id: 'ev-ont-on', cleaned: false, results: [] } }
    if (method === 'GET' && p === '/evals/pairs/pair-ont-1') return { json: { pair_id: 'pair-ont-1', evaluations: [], cleaned: false, table: { arms: [], pairs: [] } } }
    return null
  })

  const casesList = page.locator('[data-eval-cases]')
  await casesList.waitFor({ state: 'visible' })
  const caseText = await textOf(casesList)
  check(
    '题目行格式化展示源健康核查、本体穿透跳数与诊断阶段期望',
    caseText.includes('需先核查数据源健康')
      && caseText.includes('至少跨本体 1 跳')
      && caseText.includes('诊断阶段：跨源本体穿透'),
    caseText,
  )

  await page.locator('[data-eval-suite-start]').click()
  await page.locator('[data-eval-start]').waitFor({ state: 'visible' })
  check('单次评测模式下显示单臂本体穿透开关', await page.locator('[data-eval-ontology-tools]').isVisible())

  await page.locator('[data-eval-start-mode="ontology_tools"]').click()
  check('切换至 ontology_tools 贡献对照预设后隐藏单臂本体下拉', (await page.locator('[data-eval-ontology-tools]').count()) === 0)

  const estCall = await until(() =>
    calls.find((c) => c.method === 'POST' && c.path === '/evals/suites/suite-ont/evaluations/estimate' && c.body?.preset === 'ontology_tools'),
  )
  check(
    '估算请求携带 preset=ontology_tools、每题重复默认升至 3 且剥离单臂 ontology 字段',
    estCall?.body?.preset === 'ontology_tools'
      && estCall?.body?.variant?.repeats === 3
      && !('ontology' in (estCall?.body?.variant ?? {})),
    JSON.stringify(estCall?.body),
  )

  await page.locator('[data-eval-start-submit]').click()
  const startCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/evals/suites/suite-ont/evaluations'))
  check(
    '启动评测请求体与估算一致（preset=ontology_tools）',
    startCall?.body?.preset === 'ontology_tools' && JSON.stringify(startCall?.body) === JSON.stringify(estCall?.body),
    JSON.stringify(startCall?.body),
  )
})

// ================================================================ 6 标准业务实体：Agent 发现、文档存入知识库与实体助手保存

await section('6. 标准业务实体：扫描发现候选实体（勾选存入知识库）与实体助手对话提案保存', async () => {
  let entities = [
    {
      id: 'ent-line',
      key: 'production_line',
      label: '生产产线',
      description: '车间与结算统一的产线编码',
      aliases: ['产线'],
      version: 1,
      bindings: [
        {
          id: 'bind-mes', source_id: 'ds-mes', source_name: 'mes_db',
          table_name: 'dwd_workorder_daily', column_name: 'line_id',
          status: 'confirmed', source: 'human', note: '',
        },
      ],
      members: [],
      caliber_params: [],
    },
  ]

  let kbDocs = [
    {
      id: 'kb-sop-1',
      title: '制造车间编码规范.md',
      collection: 'default',
      status: 'ready',
      chunk_count: 3,
      created_at: NOW,
    },
  ]

  let discoveryFinished = false

  const catalogPayload = {
    tables: [
      {
        table_name: 'dwd_workorder_daily', qualified: 'dwd_workorder_daily', is_view: false, in_schema: true,
        label: '工单日汇总', label_status: 'confirmed', kind: 'fact',
        counts: { confirmed: 5, proposed: 0, verified: 0, total: 5 }, relations: 0,
        usage: 1, version: 1, updated_at: NOW, updated_by: '检查员甲', last_draft: null,
      },
      {
        table_name: 'dwd_material_batch', qualified: 'dwd_material_batch', is_view: false, in_schema: true,
        label: '物料批次表', label_status: 'confirmed', kind: 'fact',
        counts: { confirmed: 4, proposed: 0, verified: 0, total: 4 }, relations: 0,
        usage: 0, version: 1, updated_at: NOW, updated_by: '检查员甲', last_draft: null,
      },
    ],
    system_notes: false,
    schema_note: null,
    schema_truncated: false,
    schema_total: 2,
    guidance: null,
    active_job: null,
  }

  const { page, calls } = await openPage('/data/catalog/ds-mes?view=entities', async (method, p, body, _url, rawBody) => {
    if (method === 'GET' && p === '/datasources/ds-mes/catalog') return { json: catalogPayload }
    if (method === 'GET' && p === '/datasources/ds-mes/changes') {
      return { json: { items: [], counts: { total: 0, open: 0, by_severity: { action: 0, notice: 0, info: 0 } } } }
    }
    if (method === 'GET' && p === '/datasources/ds-mes/background-jobs') return { json: { items: [] } }
    if (method === 'GET' && p === '/datasources/ds-mes/catalog/draft-jobs/active') return { json: { job: null } }
    if (method === 'GET' && p === '/datasources/ds-mes/catalog/profile-jobs/active') return { json: { job: null } }
    if (method === 'GET' && p === '/kb/documents') return { json: clone(kbDocs) }
    if (method === 'GET' && p === '/kb/formats') {
      return { json: { extensions: ['.md', '.csv', '.xlsx', '.docx', '.pdf'], tabular: ['.csv', '.xlsx'] } }
    }
    if (method === 'GET' && p === '/ontology/entities') return { json: { items: clone(entities) } }
    if (method === 'GET' && p === '/ontology/discovery-jobs/active') return { json: { job: null } }

    if (method === 'POST' && p === '/ontology/entities/attachments') {
      const savedKb = Boolean(rawBody && /name="save_to_kb"[\s\S]*?\r?\n\r?\ntrue/i.test(rawBody))
      if (savedKb) {
        kbDocs = [
          ...kbDocs,
          {
            id: 'kb-wh-map',
            title: 'warehouse_map.csv',
            collection: 'default',
            status: 'ready',
            chunk_count: 1,
            created_at: NOW,
          },
        ]
      }
      return {
        status: 201,
        json: {
          attachment_id: 'att-wh-1',
          title: 'warehouse_map.csv',
          filename: 'warehouse_map.csv',
          chunk_count: 1,
          char_count: 48,
          saved_to_kb: savedKb,
          document_id: savedKb ? 'kb-wh-map' : null,
          collection: savedKb ? 'default' : null,
          preview: 'canonical,mes_code,erp_code',
        },
      }
    }

    if (method === 'POST' && p === '/ontology/discovery-jobs') {
      discoveryFinished = false
      const recentUtcNoZ = new Date(Date.now() - 12_000).toISOString().replace(/Z$/i, '')
      return {
        status: 201,
        json: {
          id: 'djob-1',
          status: 'running',
          phase: '步骤 3/4：模型正在识别候选实体（已识别 2 项操作，输出 480 字）…',
          source_ids: body?.source_ids ?? [],
          document_ids: body?.document_ids ?? [],
          attachment_ids: body?.attachment_ids ?? [],
          summary: {
            sources_scanned: 2,
            source_names: ['mes_db', 'erp_db'],
            tables_scanned: 3,
            columns_scanned: 12,
            workflows_scanned: 1,
            documents_scanned: 2,
            chunks_scanned: 4,
            step_index: 3,
            step_total: 4,
            progress_pct: 56,
            model_chars: 480,
            ops_detected: 2,
            candidate_keys: ['warehouse'],
          },
          started_at: recentUtcNoZ,
          created_at: recentUtcNoZ,
        },
      }
    }

    if (method === 'GET' && p === '/ontology/discovery-jobs/djob-1') {
      if (!discoveryFinished) {
        discoveryFinished = true
        entities = [
          ...entities,
          {
            id: 'ent-wh',
            key: 'warehouse',
            label: '仓储库位',
            description: '车间物料批次与结算库存共享的标准库位',
            aliases: ['库位', '仓库'],
            version: 1,
            bindings: [
              {
                id: 'bind-wh-mes', source_id: 'ds-mes', source_name: 'mes_db',
                table_name: 'dwd_material_batch', column_name: 'wh_code',
                status: 'proposed', source: 'ai', note: '对照表识别',
              },
              {
                id: 'bind-wh-erp', source_id: 'ds-erp', source_name: 'erp_db',
                table_name: 'dwd_settlement_daily', column_name: 'warehouse_no',
                status: 'proposed', source: 'ai', note: '对照表识别',
              },
            ],
            members: [
              { value: 'WH_01', label: '一号立体库', aliases: [], source_codes: { mes_db: 'W01', erp_db: 'ERP-W01' } },
            ],
            caliber_params: [],
          },
        ]
      }
      return {
        json: {
          id: 'djob-1',
          status: 'succeeded',
          phase: '扫描完成',
          source_ids: ['ds-mes', 'ds-erp'],
          document_ids: ['kb-sop-1'],
          attachment_ids: ['att-wh-1'],
          summary: {
            sources_scanned: 2,
            source_names: ['mes_db', 'erp_db'],
            tables_scanned: 3,
            columns_scanned: 12,
            workflows_scanned: 1,
            documents_scanned: 2,
            chunks_scanned: 4,
            step_index: 4,
            step_total: 4,
            progress_pct: 100,
            model_chars: 620,
            ops_detected: 4,
            created_entities: 1,
            updated_entities: 0,
            proposed_bindings: 2,
            proposed_params: 0,
            added_members: 1,
            touched_entities: [{ id: 'ent-wh', key: 'warehouse', label: '仓储库位' }],
          },
          started_at: NOW,
          created_at: NOW,
          finished_at: NOW,
        },
      }
    }

    if (method === 'POST' && p === '/ontology/entities/assistant') {
      const sse = [
        `data: ${JSON.stringify({
          type: 'query',
          source: 'mes_db',
          sql: 'SELECT DISTINCT wh_code FROM dwd_material_batch LIMIT 5',
          columns: ['wh_code'],
          rows: [['W01'], ['W02']],
          row_count: 2,
          truncated: false,
          error: null,
        })}\n\n`,
        `data: ${JSON.stringify({
          type: 'proposal',
          changes: [
            {
              id: 'chg-wh-2',
              op: 'upsert_member',
              entity_key: 'warehouse',
              summary: '登记成员编码 WH_02（二号立体库）：mes_db=W02, erp_db=ERP-W02',
              reason: '核对两库真实批次表编码前缀一致',
              op_payload: {
                op: 'upsert_member',
                entity_key: 'warehouse',
                value: 'WH_02',
                label: '二号立体库',
                source_codes: { mes_db: 'W02', erp_db: 'ERP-W02' },
              },
            },
          ],
          expected_versions: { warehouse: 1 },
        })}\n\n`,
        `data: ${JSON.stringify({
          type: 'text',
          delta: '已完成 **双库库位** 抽样核对，生成 `WH_02` 成员编码映射提案，请确认后保存。',
        })}\n\n`,
        `data: ${JSON.stringify({ type: 'done', queries: 1 })}\n\n`,
      ].join('')
      return {
        status: 200,
        contentType: 'text/event-stream; charset=utf-8',
        body: sse,
      }
    }

    if (method === 'POST' && p === '/ontology/entities/apply-patch') {
      let updatedWh = null
      entities = entities.map((ent) => {
        if (ent.key !== 'warehouse') return ent
        updatedWh = {
          ...ent,
          version: ent.version + 1,
          members: [
            ...ent.members,
            { value: 'WH_02', label: '二号立体库', aliases: [], source_codes: { mes_db: 'W02', erp_db: 'ERP-W02' } },
          ],
        }
        return updatedWh
      })
      return {
        json: {
          applied_count: 1,
          created_entities: 0,
          updated_entities: 1,
          entities: updatedWh ? [clone(updatedWh)] : [],
        },
      }
    }

    return null
  })

  await page.locator('[data-catalog-entities-view]').waitFor({ state: 'visible' })
  check(
    '标准实体页顶显示「扫描发现候选实体」与「实体助手」入口按钮',
    (await page.locator('[data-entity-discover-open]').isVisible())
      && (await page.locator('[data-entity-assistant-open]').isVisible()),
  )

  // 1) 打开扫描发现候选实体弹窗：验证默认仅勾选当前数据源 ds-mes，再手动勾选 ds-erp 发起跨源扫描
  await page.locator('[data-entity-discover-open]').click()
  await page.locator('[data-entity-discover-dialog]').waitFor({ state: 'visible' })
  check(
    '扫描发现弹窗默认仅勾选当前入口数据源 ds-mes，不混入其他未选数据源',
    (await page.locator('[data-discover-source="ds-mes"]').isChecked())
      && !(await page.locator('[data-discover-source="ds-erp"]').isChecked()),
  )
  await page.locator('[data-discover-source="ds-erp"]').check()

  await page.locator('[data-discover-doc-save-kb]').check()
  check('勾选「同时存入知识库供后续复用」后显示目标集合选择框', await page.locator('[data-discover-doc-collection]').isVisible())

  await page.locator('[data-discover-doc-input]').setInputFiles({
    name: 'warehouse_map.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('canonical,mes_code,erp_code\nWH_01,W01,ERP-W01\n', 'utf8'),
  })

  const attRow = page.locator('[data-discover-attachment="att-wh-1"]')
  await attRow.waitFor({ state: 'visible' })
  const attText = await textOf(attRow)
  const uploadCall = calls.find((c) => c.method === 'POST' && c.path === '/ontology/entities/attachments')
  check(
    '附件上传请求携带 save_to_kb=true 且弹窗列表回显「已存入知识库」标记',
    Boolean(uploadCall?.rawBody && /name="save_to_kb"[\s\S]*?\r?\n\r?\ntrue/i.test(uploadCall.rawBody))
      && attText.includes('warehouse_map.csv')
      && attText.includes('已存入知识库'),
    attText,
  )

  await page.locator('[data-discover-kb-doc="kb-sop-1"]').check()
  await page.locator('[data-discover-submit]').click()

  // 验证扫描运行期的实时进度条、UTC 无后缀时间戳兼容与量化状态展示
  await page.locator('[data-entity-discover-progressbar="56"]').waitFor({ state: 'visible' })
  const runningBannerText = await textOf(page.locator('[data-entity-discover-banner="running"]'))
  check(
    '扫描进行中横幅展示分步阶段、56% 进度条、秒级真实耗时（无 8 小时时差偏移）与候选实体标识',
    runningBannerText.includes('步骤 3/4')
      && runningBannerText.includes('56%')
      && /已用时 1[1-9] 秒/.test(runningBannerText)
      && runningBannerText.includes('warehouse'),
    runningBannerText,
  )

  const discoverCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/ontology/discovery-jobs'))
  check(
    '扫描任务提交携带按需勾选的数据源、上传附件 ID 与所选知识库文档 ID',
    discoverCall?.body?.source_ids?.includes('ds-mes')
      && discoverCall?.body?.source_ids?.includes('ds-erp')
      && JSON.stringify(discoverCall?.body?.attachment_ids) === JSON.stringify(['att-wh-1'])
      && JSON.stringify(discoverCall?.body?.document_ids) === JSON.stringify(['kb-sop-1']),
    JSON.stringify(discoverCall?.body),
  )

  const doneBanner = page.locator('[data-entity-discover-banner="succeeded"]')
  await doneBanner.waitFor({ state: 'visible' })
  const bannerText = await textOf(doneBanner)
  const whItem = page.locator('[data-entity-item="warehouse"]')
  await whItem.waitFor({ state: 'visible' })
  const whItemText = await textOf(whItem)
  check(
    '后台扫描完成后顶部展示汇总横幅，左侧提供「仅看 mes_db」筛选标签，新发现实体刷新至列表',
    bannerText.includes('新建实体 1 个')
      && bannerText.includes('新增待确认绑定 2 处')
      && (await textOf(page.locator('[data-entity-scope="source"]'))).includes('仅看 mes_db（2）')
      && whItemText.includes('仓储库位')
      && whItemText.includes('待确认 2 项'),
    `${bannerText} | ${whItemText}`,
  )

  // 点击「查看明细」打开扫描进度/结果弹窗，并点击本次新增实体跳转选中
  await page.locator('[data-entity-discover-detail-open]').click()
  const detailModal = page.locator('[data-entity-discover-progress-modal="succeeded"]')
  await detailModal.waitFor({ state: 'visible' })
  const modalText = await textOf(detailModal)
  check(
    '扫描明细弹窗展示四阶段完成状态、表列/文档量化统计与可点击跳转的新增实体',
    modalText.includes('3 张表 / 12 列')
      && modalText.includes('2 份 / 4 段')
      && (await page.locator('[data-discover-touched-entity="warehouse"]').isVisible()),
    modalText,
  )
  await page.locator('[data-discover-touched-entity="warehouse"]').click()

  // 2) 通过明细弹窗跳转选中仓储库位后打开「实体助手」，验证默认聚焦当前数据源、按需勾选跨源、流式核查与乐观锁保存
  await page.locator('[data-entity-assistant-open]').click()
  const assistant = page.locator('[data-entity-assistant]')
  await assistant.waitFor({ state: 'visible' })
  check('实体助手顶栏聚焦当前选中的「仓储库位」实体', (await textOf(assistant)).includes('当前：仓储库位'))
  check(
    '实体助手默认数据范围仅锁定当前入口数据源 mes_db',
    (await textOf(page.locator('[data-entity-assistant-scope-summary]'))) === 'mes_db',
  )
  await page.locator('[data-entity-assistant-scope-toggle]').click()
  await page.locator('[data-entity-assistant-source="ds-erp"]').check()
  check(
    '展开跨源选择勾选 erp_db 后数据范围更新为 mes_db、erp_db',
    (await textOf(page.locator('[data-entity-assistant-scope-summary]'))) === 'mes_db、erp_db',
  )

  await page.locator('[data-entity-assistant-input]').fill('请核对 mes_db 与 erp_db 的库位编码并补充跨源映射')
  await page.locator('[data-entity-assistant-send]').click()

  await page.locator('[data-entity-assistant-msg="assistant"][data-turn-state="done"]').waitFor({ state: 'visible' })
  const assistantCall = calls.find((c) => c.method === 'POST' && c.path === '/ontology/entities/assistant')
  check(
    '实体助手对话请求携带选定的 source_ids、focus_entity_key=warehouse 与用户提问',
    JSON.stringify(assistantCall?.body?.source_ids) === JSON.stringify(['ds-mes', 'ds-erp'])
      && assistantCall?.body?.focus_entity_key === 'warehouse'
      && assistantCall?.body?.messages?.[0]?.content?.includes('库位编码'),
    JSON.stringify(assistantCall?.body),
  )

  check(
    '流式响应渲染 Markdown（加粗与行内代码）、只读核查折叠卡片与结构化修改提案及待保存汇总栏',
    (await page.locator('[data-entity-assistant-text] strong').count()) === 1
      && (await page.locator('[data-entity-assistant-text] code').count()) === 1
      && (await page.locator('[data-entity-assistant-query="ok"]').isVisible())
      && (await page.locator('[data-entity-proposal-item="chg-wh-2"]').isVisible())
      && (await page.locator('[data-entity-assistant-pending="1"]').isVisible()),
  )

  // 测试单条移出与恢复保留
  await page.locator('[data-entity-proposal-item="chg-wh-2"] [data-entity-proposal-toggle]').click()
  check('点击「移出」后待保存汇总栏收起', (await page.locator('[data-entity-assistant-pending]').count()) === 0)
  await page.locator('[data-entity-proposal-item="chg-wh-2"] [data-entity-proposal-toggle]').click()
  check('再次点击「保留」后恢复待保存项', await page.locator('[data-entity-assistant-pending="1"]').isVisible())

  await page.locator('[data-entity-assistant-save]').click()
  const patchCall = await until(() => calls.find((c) => c.method === 'POST' && c.path === '/ontology/entities/apply-patch'))
  check(
    '点击「保存修改」提交 operations 与 expected_versions 乐观锁校验',
    patchCall?.body?.operations?.length === 1
      && patchCall?.body?.operations?.[0]?.op === 'upsert_member'
      && patchCall?.body?.operations?.[0]?.value === 'WH_02'
      && patchCall?.body?.expected_versions?.warehouse === 1,
    JSON.stringify(patchCall?.body),
  )

  await page.locator('[data-entity-assistant-saved]').waitFor({ state: 'visible' })
  const updatedWhText = await textOf(page.locator('[data-entity-item="warehouse"]'))
  check(
    '保存成功后显示生效提示并同步刷新实体列表成员计数（2 项）',
    updatedWhText.includes('成员 2 项'),
    updatedWhText,
  )
})

// ================================================================ 全局收尾核对

console.log('\n=== 全局收尾核对 ===')
check('没有未拦截的越界写请求（stray 为空）', allStray.length === 0, allStray.join(', '))
check('没有触发浏览器原生弹窗（alert/confirm/prompt）', allDialogs.length === 0, allDialogs.join('; '))
check('没有未捕获的前端运行时异常（pageerror）', allPageErrors.length === 0, allPageErrors.join('; '))

await browser.close()
process.exit(failed ? 1 : 0)
