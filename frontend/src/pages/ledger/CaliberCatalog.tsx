/**
 * 口径与指标目录（/ledger/calibers）：
 *
 * 1. 独立语义指标卡片（SemanticMetric）：即存即生效，直接绑定数据源、物理表、聚合 SQL 表达式、时间列与标准业务实体维度，
 *    保存后自动接入问数页「快速直查」与 metric_query 工具；支持在线试算与从数据目录 / 业务文档批量提取。
 * 2. 工作流受管口径（CaliberDescriptor）：固定工作流版本@口径卡，用于多步编排与正式出具对账。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, Layers, Play, Plus, RefreshCw, Search, Sparkles, Zap } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { SemanticDecomposeJob, SemanticMetric } from '../../api/semanticMetrics'
import { SchemaForm } from '../../components/SchemaForm'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, toast } from '../../components/ui'
import { LEDGER_TEXT } from '../../lib/terms'
import { LEDGER_UI } from '../../lib/termsLedger'
import { useAtLeast } from '../../store/auth'
import {
  DecomposeJobBanner, MetricEditorModal, MetricSuggestModal, SemanticMetricCardsSection,
} from './SemanticMetricStudio'
import { TrialResult, TrialValidation } from './TrialResult'
import type { FormSchema } from '../../typesApp'
import type { CaliberDescriptor, CaliberTrialOut, CanonicalEntity } from '../../typesLedger'

const keyOf = (d: CaliberDescriptor) => `${d.workflow_id}@${d.version}/${d.card.node_id}`

type ViewFilter = 'all' | 'metrics' | 'calibers'

function defaultsOf(schema: FormSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [name, field] of Object.entries(schema.properties ?? {})) {
    if (field?.default !== undefined && field.default !== null && field.default !== '') out[name] = field.default
  }
  return out
}

export function CaliberCatalog() {
  const builder = useAtLeast('builder')
  const [items, setItems] = useState<CaliberDescriptor[] | null>(null)
  const [metrics, setMetrics] = useState<SemanticMetric[]>([])
  const [entities, setEntities] = useState<CanonicalEntity[]>([])
  const [filter, setFilter] = useState<ViewFilter>('all')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingMetric, setEditingMetric] = useState<SemanticMetric | null>(null)
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [bannerJob, setBannerJob] = useState<SemanticDecomposeJob | null>(null)
  const [modalInitialJob, setModalInitialJob] = useState<SemanticDecomposeJob | null>(null)
  const [dismissedJobId, setDismissedJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, Record<string, unknown>>>({})
  const [trials, setTrials] = useState<Record<string, CaliberTrialOut>>({})
  const [busy, setBusy] = useState<string | null>(null)

  const refreshDecomposeBanner = () => {
    if (!builder) return
    api.semanticMetrics.decomposeJobs.active().then(
      ({ active, latest }) => {
        setBannerJob(active ?? latest ?? null)
      },
      () => {},
    )
  }

  const loadMetrics = () => {
    if (!builder) return
    api.semanticMetrics.list().then(
      (res) => setMetrics(res.items ?? []),
      () => setMetrics([]),
    )
    api.ontology.listEntities().then(
      (res) => setEntities(res.items ?? []),
      () => setEntities([]),
    )
    refreshDecomposeBanner()
  }

  const load = () => {
    setError(null)
    setItems(null)
    api.caliberLedger.catalog().then(
      (body) => setItems(body.calibers),
      (e) => setError(e),
    )
    loadMetrics()
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [builder])

  // 当有后台口径拆解任务正在排队或执行时，自动轮询最新阶段进度
  useEffect(() => {
    if (!builder || !bannerJob || (bannerJob.status !== 'queued' && bannerJob.status !== 'running')) return
    let alive = true
    const timer = window.setInterval(() => {
      api.semanticMetrics.decomposeJobs.active().then(
        ({ active, latest }) => {
          if (!alive) return
          const next = active ?? latest ?? null
          setBannerJob(next)
          if (!active && latest?.status === 'succeeded') {
            setDismissedJobId(null)
            toast.ok('后台口径文档拆解已完成，点击横幅即可审阅并一键应用')
          }
        },
        () => {},
      )
    }, 1800)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [builder, bannerJob?.id, bannerJob?.status])

  const handleCancelBannerJob = async (jobId: string) => {
    try {
      const { job: cancelled } = await api.semanticMetrics.decomposeJobs.cancel(jobId)
      setBannerJob(cancelled)
      toast.info('已取消后台拆解任务')
    } catch (e) {
      toast.error(e)
    }
  }

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!items || !q) return items ?? []
    return items.filter((d) => [d.workflow_name, d.card.caliber, d.card.title,
      ...d.metrics.map((m) => m.name ?? m.id)].some((s) => String(s ?? '').toLowerCase().includes(q)))
  }, [items, query])

  const shownMetricsCount = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return metrics.length
    return metrics.filter((m) => [
      m.name, m.key, m.table_name, m.source_name, m.description, ...(m.aliases ?? []),
    ].some((s) => String(s ?? '').toLowerCase().includes(q))).length
  }, [metrics, query])

  const toggle = (d: CaliberDescriptor) => {
    const key = keyOf(d)
    if (open === key) { setOpen(null); return }
    setOpen(key)
    setValues((v) => (v[key] ? v : { ...v, [key]: defaultsOf(d.entry_schema) }))
  }

  const trial = async (d: CaliberDescriptor) => {
    const key = keyOf(d)
    setBusy(key)
    try {
      const out = await api.caliberLedger.trial(d.workflow_id, d.version, d.card.node_id, values[key] ?? {})
      setTrials((t) => ({ ...t, [key]: out }))
      if (out.status === 'succeeded') toast.ok('试运行完成')
      else if (out.status === 'running') toast.warn('试运行还在进行，打开运行记录看结果')
      else toast.error(out.error || '试运行没有成功')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const showMetrics = filter !== 'calibers'
  const showCalibers = filter !== 'metrics'

  return (
    <div className="flex h-full flex-col" data-caliber-catalog="">
      <PageHeader icon={<Layers size={14} />} title="口径" subtitle="独立语义指标卡片（即存即生效）与工作流受管口径目录"
                  actions={<>
                    {builder && (
                      <>
                        <button type="button" className="btn btn-xs btn-primary" data-metric-create-btn=""
                                onClick={() => { setEditingMetric(null); setEditorOpen(true) }}>
                          <Zap size={12} aria-hidden /> 新建指标卡片
                        </button>
                        <button type="button" className="btn btn-xs" data-metric-suggest-btn=""
                                onClick={() => {
                                  const activeRunning = bannerJob && (bannerJob.status === 'queued' || bannerJob.status === 'running')
                                    ? bannerJob
                                    : null
                                  setModalInitialJob(activeRunning)
                                  setSuggestOpen(true)
                                }}>
                          <Sparkles size={12} aria-hidden /> 文档一键拆解 / 提取指标
                        </button>
                      </>
                    )}
                    <Link to="/ledger/drafts" className={clsx('btn btn-xs', !builder && 'btn-primary')} data-caliber-drafts-link=""
                           title="从业务文档起草工作流受管口径；已经有答案的，在草稿里接一次运行">
                      <Plus size={12} aria-hidden /> {LEDGER_UI.drafts.entry}
                    </Link>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={load} title="重新加载">
                      <RefreshCw size={12} aria-hidden /> 刷新
                    </button>
                  </>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          {builder && bannerJob && bannerJob.id !== dismissedJobId && (
            <DecomposeJobBanner
              job={bannerJob}
              onOpenModal={(j) => {
                setModalInitialJob(j)
                setSuggestOpen(true)
              }}
              onCancel={(jid) => void handleCancelBannerJob(jid)}
              onDismiss={() => setDismissedJobId(bannerJob.id)}
            />
          )}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div role="group" aria-label="口径视图筛选" className="inline-flex items-center rounded-md border bg-panel p-0.5 text-xs">
              <button type="button" aria-pressed={filter === 'all'}
                      className={clsx('rounded px-2.5 py-1 transition-colors',
                        filter === 'all' ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg')}
                      onClick={() => setFilter('all')}>
                全部（{metrics.length + (items?.length ?? 0)}）
              </button>
              <button type="button" aria-pressed={filter === 'metrics'} data-filter-metrics=""
                      className={clsx('rounded px-2.5 py-1 transition-colors',
                        filter === 'metrics' ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg')}
                      onClick={() => setFilter('metrics')}>
                指标卡片 · 即存即用（{metrics.length}）
              </button>
              <button type="button" aria-pressed={filter === 'calibers'} data-filter-calibers=""
                      className={clsx('rounded px-2.5 py-1 transition-colors',
                        filter === 'calibers' ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg')}
                      onClick={() => setFilter('calibers')}>
                工作流受管口径（{items?.length ?? 0}）
              </button>
            </div>
            <label className="relative min-w-[240px] flex-1 sm:max-w-sm">
              <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
              <input className="field w-full pl-8" value={query} onChange={(e) => setQuery(e.target.value)}
                     placeholder="按指标名、标识、物理表或工作流查找" aria-label="查找口径" />
            </label>
          </div>

          {showMetrics && (
            <SemanticMetricCardsSection
              metrics={metrics}
              entities={entities}
              query={query}
              onReload={loadMetrics}
              onEdit={(m) => { setEditingMetric(m); setEditorOpen(true) }}
            />
          )}

          {filter === 'metrics' && shownMetricsCount === 0 && (
            <EmptyState
              offline={false}
              icon={<Zap size={22} />}
              title="还没有独立语义指标卡片"
              body={query ? '换个关键词试试' : '点击右上角「新建指标卡片」或「从目录与文档提取」，保存后无需发布工作流即可在问数快速直查中直接命中'}
            />
          )}

          {showCalibers && (
            error ? <ErrorState error={error} onRetry={load} compact />
              : !items ? <Skeleton rows={5} height={38} />
              : !shown.length ? (
                shownMetricsCount === 0 ? (
                  <EmptyState offline={false} icon={<Layers size={22} />} title="还没有可用的口径"
                              body={query ? '换个关键词试试' : '可新建即存即用的指标卡片，或在工作流里发布带口径卡的版本'} />
                ) : null
              ) : (
                <div className="space-y-2">
                  {showMetrics && shownMetricsCount > 0 && (
                    <div className="px-0.5 pt-2 text-xs font-semibold text-dim">
                      工作流受管口径（共 {shown.length} 项）
                    </div>
                  )}
                  {shown.map((d) => {
                    const key = keyOf(d)
                    const expanded = open === key
                    const result = trials[key]
                    return (
                      <section key={key} className="overflow-hidden rounded-lg border bg-panel" data-caliber={key}>
                        <button type="button" className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover"
                                onClick={() => toggle(d)} aria-expanded={expanded} data-caliber-toggle="">
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                              <span className="text-sm font-medium">{d.workflow_name}</span>
                              <span className="text-2xs text-faint">v{d.version}</span>
                              {d.card.caliber && <span className="text-xs text-dim">· {d.card.caliber}</span>}
                              <span className="rounded border px-1.5 py-0.5 text-[10px] text-dim">
                                {(LEDGER_TEXT.level as Record<string, string>)[d.level] ?? d.level}
                              </span>
                            </div>
                            <div className="mt-0.5 text-2xs text-faint">
                              {d.metrics.map((m) => m.name ?? m.id).join('、')}
                              {d.entry_schema.properties && Object.keys(d.entry_schema.properties).length > 0
                                ? ` · ${Object.keys(d.entry_schema.properties).length} 个入口字段` : ' · 没有入口字段'}
                              {d.dependencies.tables.length > 0 ? ` · 读 ${d.dependencies.tables.join('、')}` : ''}
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-2 text-[10px] text-faint">
                            <span>{d.composable ? '可组合' : '不可组合'}</span>
                            <span>{d.issuance.auto_issue ? '可自动出具' : '只可一键出具'}</span>
                            <ChevronDown size={14} className={clsx('transition-transform', expanded && 'rotate-180')} aria-hidden />
                          </div>
                        </button>
                        {expanded && (
                          <div className="space-y-3 border-t px-3 py-3" data-caliber-detail="">
                            <div className="grid gap-3 md:grid-cols-2">
                              <div>
                                <h3 className="mb-1 text-2xs font-semibold text-dim">指标</h3>
                                <ul className="space-y-1 text-xs">
                                  {d.metrics.map((m) => (
                                    <li key={m.id} className="border-b border-hairline pb-1 last:border-0">
                                      <div className="flex items-baseline justify-between gap-2">
                                        <span>{m.name ?? m.id}</span>
                                        <span className="text-2xs text-faint">{m.unit || '—'}</span>
                                      </div>
                                      {m.definition?.text && (
                                        <p className="mt-0.5 text-2xs leading-relaxed text-faint">{m.definition.text}</p>
                                      )}
                                      {!!m.params?.length && (
                                        <p className="mt-0.5 text-2xs text-faint">
                                          参数：{m.params.map((p) => `${p.name}（${p.type}）`).join('、')}
                                        </p>
                                      )}
                                    </li>
                                  ))}
                                </ul>
                              </div>
                              <div>
                                <h3 className="mb-1 text-2xs font-semibold text-dim">依赖</h3>
                                <p className="text-2xs leading-relaxed text-dim">
                                  数据源：{d.dependencies.sources.join('、') || '—'}<br />
                                  表：{d.dependencies.tables.join('、') || '—'}<br />
                                  {d.compose_reason ? `不能组合：${d.compose_reason}` : '可以按这一版组合'}
                                </p>
                                {d.issuance.reasons.length > 0 && (
                                  <p className="mt-1 text-2xs leading-relaxed text-faint">
                                    出具：{d.issuance.reasons.join('；')}
                                  </p>
                                )}
                              </div>
                            </div>
                            <div>
                              <h3 className="mb-1 text-2xs font-semibold text-dim">入口字段</h3>
                              {d.entry_schema.properties && Object.keys(d.entry_schema.properties).length > 0 ? (
                                <SchemaForm schema={d.entry_schema} value={values[key] ?? {}}
                                            onChange={(v) => {
                                              setValues((all) => ({ ...all, [key]: v }))
                                              setTrials((all) => {
                                                const next = { ...all }
                                                delete next[key]
                                                return next
                                              })
                                            }}
                                            idPrefix={`caliber-${key}`} />
                              ) : <p className="text-2xs text-faint">这份口径不需要参数。</p>}
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                              <button type="button" className="btn btn-primary btn-sm" disabled={busy === key}
                                      onClick={() => void trial(d)} data-caliber-trial="">
                                {busy === key ? <Spinner size={12} /> : <Play size={12} aria-hidden />} 试运行
                              </button>
                              <Link to={`/studio/${d.workflow_id}`} className="btn btn-sm btn-ghost">打开工作流</Link>
                              <span className="text-2xs text-faint">试运行是只读探索运行，不是正式出具</span>
                            </div>
                            {result && (
                              <div className="rounded-md border bg-elev px-3 py-2 text-xs" data-caliber-trial-result={result.status}>
                                {result.status === 'succeeded' ? (
                                  <ul className="space-y-1">
                                    {result.metrics.map((m) => (
                                      <li key={m.id} className="flex items-baseline justify-between gap-2">
                                        <span>{m.name ?? m.id}</span>
                                        <span className="tnum font-medium">{m.rendered ?? String(m.value ?? '—')}{m.unit ? ` ${m.unit}` : ''}</span>
                                      </li>
                                    ))}
                                  </ul>
                                ) : (
                                  <Notice tone={result.status === 'running' ? 'info' : 'err'}>
                                    {result.status === 'running' ? '还在运行：' : '试运行没有成功：'}
                                    {result.error || result.status}
                                  </Notice>
                                )}
                                {result.run_id && (
                                  <div className="mt-1">
                                    <Link className="text-2xs text-accent hover:underline" to={`/runs/${result.run_id}`}
                                          data-caliber-trial-run="">
                                      {LEDGER_UI.drafts.trialRun}
                                    </Link>
                                  </div>
                                )}
                                {result.status === 'succeeded' && <><TrialValidation report={result.validation} /><TrialResult queries={result.queries} /></>}
                              </div>
                            )}
                          </div>
                        )}
                      </section>
                    )
                  })}
                </div>
              )
          )}
        </div>
      </div>

      {builder && (
        <>
          <MetricEditorModal
            open={editorOpen}
            metric={editingMetric}
            entities={entities}
            onClose={() => { setEditorOpen(false); setEditingMetric(null) }}
            onSaved={loadMetrics}
          />
          <MetricSuggestModal
            open={suggestOpen}
            initialJob={modalInitialJob}
            onClose={() => setSuggestOpen(false)}
            onSaved={loadMetrics}
            onJobUpdated={(nextJob) => {
              setBannerJob(nextJob)
              setModalInitialJob(nextJob)
              if (nextJob) setDismissedJobId(null)
            }}
          />
        </>
      )}
    </div>
  )
}
