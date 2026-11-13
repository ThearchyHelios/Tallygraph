import { Link } from 'react-router-dom'
import { AlertTriangle, Check, FileText } from 'lucide-react'
import clsx from 'clsx'
import { ErrorState, Skeleton, Spinner } from '../../../components/ui'
import { formatTime } from '../../../lib/format'
import { LEDGER_UI } from '../../../lib/termsLedger'
import type { DataSourceName, KbDocument, Run } from '../../../types'
import type {
  CaliberDraft, CaliberDraftStage, CaliberDraftStatus, DraftAlignment, DraftBinding, DraftCitation, DraftMetric,
  DraftTrialMetric,
} from '../../../typesLedger'
import { choiceLabel, spanParts } from './helpers'

const D = LEDGER_UI.drafts

/** 阶段标签：起草中 / 待审 / 已丢弃 / 起草失败；细状态放在 title 里，鼠标悬停能看到「待确认问题」这类差别 */
export function StatusPill({ stage, status }: { stage: CaliberDraftStage; status?: CaliberDraftStatus }) {
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

export function QuoteBody({ content, span, quote }: { content: string; span?: [number, number]; quote: string }) {
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
export function AlignmentBlock({ alignment }: { alignment: DraftAlignment }) {
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

export function DraftQuestionList({
  questions, prefix, pickedQuestions, onToggle,
}: {
  questions: string[] | undefined
  prefix: string
  pickedQuestions: string[]
  onToggle: (question: string, checked: boolean) => void
}) {
  const list = (questions ?? []).filter((q) => q.trim())
  if (!list.length) return null
  return (
    <ul className="mt-1 space-y-0.5">
      {list.map((q, i) => (
        <li key={`${prefix}:${i}`}>
          <label className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim">
            <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={pickedQuestions.includes(q)}
                   onChange={(e) => onToggle(q, e.target.checked)} />
            <span className="min-w-0 [overflow-wrap:anywhere]">{q}</span>
          </label>
        </li>
      ))}
    </ul>
  )
}

/**
 * 取数 SQL：草稿图里写好的模板，入口字段在运行时渲染；只读展示，不出现执行值。
 * 默认折叠，SQL 可能为空（旧草稿、没写出取数），那时整块不画。
 */
export function DraftSqlSection({ draft }: { draft: CaliberDraft }) {
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
export function DraftChecksSection({ draft }: { draft: CaliberDraft }) {
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

/**
 * 一条指标：默认收成一行，名字、单位、值和未决问题数直接看得见；展开才是定义、表达式、引用。
 * 指标多到几十条时这样一屏能扫完，找哪条再展开哪条。
 */
export function DraftMetricItem({
  metric, index, values, binding, chunks, paragraphOf, pickedQuestions, onToggleQuestion,
}: {
  metric: DraftMetric
  index: number
  values: Map<string, DraftTrialMetric>
  binding?: DraftBinding
  chunks: Record<string, { ordinal: number; content: string }>
  paragraphOf: (citation: DraftCitation) => number | null
  pickedQuestions: string[]
  onToggleQuestion: (question: string, checked: boolean) => void
}) {
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
  const business = (binding?.editable_fields ?? [])
    .map((field): [string, string] => {
      const raw = (metric as unknown as Record<string, unknown>)[field.field]
      return [field.label, typeof raw === 'string' ? choiceLabel(field, raw) : '']
    })
    .filter(([, text]) => !!text)
  return (
    <li data-draft-metric={metric.name}>
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
              <ul className="space-y-1">
                {citations.map((citation, i) => {
                  const chunk = chunks[citation.chunk]
                  const ordinal = paragraphOf(citation)
                  return (
                    <li key={`${index}:${i}`}>
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
                        <p className="mt-0.5 text-[10px] text-faint">{metric.name}</p>
                      </details>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
          {!!questions.length && (
            <div className="mt-1.5" data-draft-questions="">
              <h4 className="text-2xs font-semibold text-faint">{D.questions}</h4>
              <DraftQuestionList questions={questions} prefix={`${index}`}
                                 pickedQuestions={pickedQuestions} onToggle={onToggleQuestion} />
            </div>
          )}
        </div>
      </details>
    </li>
  )
}

export function DraftAttachSection({
  draft, runId, setRunId, recentRuns, busy, attachOut, onAttach, showForm,
}: {
  draft: CaliberDraft
  runId: string
  setRunId: (value: string) => void
  recentRuns: Run[] | null
  busy: string | null
  attachOut: { workflow_id: string; version: number; warnings: string[] } | null
  onAttach: (draft: CaliberDraft) => void
  showForm: boolean
}) {
  return (
    <>
      {showForm && (
        <details className="rounded-md border bg-elev px-3 py-2" data-draft-attach="">
          <summary className="cursor-pointer text-2xs font-semibold text-dim">{D.attach}</summary>
          <p className="mt-0.5 text-2xs leading-relaxed text-faint">{D.attachHint}</p>
          <div className="mt-1.5 flex flex-wrap items-end gap-2">
            <label className="min-w-40 flex-1 text-2xs text-faint">
              <span className="mb-0.5 block">{D.runId}</span>
              <input className="field w-full" value={runId} placeholder={D.runIdPlaceholder}
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
                    onClick={() => onAttach(draft)} data-draft-attach-submit="">
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
    </>
  )
}

export function DraftStartSection({
  docs, docsError, sources, pickedDocs, setPickedDocs, datasourceId, setDatasourceId,
  guidance, setGuidance, fixedFilters, setFixedFilters, submitting, onSubmit,
}: {
  docs: KbDocument[] | null
  docsError: unknown
  sources: DataSourceName[]
  pickedDocs: string[]
  setPickedDocs: React.Dispatch<React.SetStateAction<string[]>>
  datasourceId: string
  setDatasourceId: (value: string) => void
  guidance: string
  setGuidance: (value: string) => void
  fixedFilters: string
  setFixedFilters: (value: string) => void
  submitting: boolean
  onSubmit: (definitions?: boolean) => void
}) {
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
            <select className="field w-full" value={datasourceId}
                    onChange={(e) => setDatasourceId(e.target.value)}>
              <option value="">导入定义时可暂不选数据源</option>
              {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <span className="mt-0.5 block">先保留文档定义，选择指标后再绑定已探查的数据源。</span>
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
          <button type="button" className="btn btn-primary btn-sm" disabled={submitting || !pickedDocs.length}
                  onClick={() => onSubmit()} data-draft-submit="">
            {submitting ? <Spinner size={12} /> : <FileText size={12} aria-hidden />} {submitting ? '正在导入…' : '导入指标定义表'}
          </button>
          <button type="button" className="btn btn-ghost btn-sm ml-2" disabled={submitting || !datasourceId || !pickedDocs.length}
                  onClick={() => onSubmit(false)}>其他文档：AI 起草</button>
        </div>
      </div>
    </section>
  )
}
